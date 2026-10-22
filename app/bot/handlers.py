"""Telegram handlers. One Application per tenant; tenant context lives in application.bot_data["ctx"]."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.data.generator import IST
from app.delivery import templates
from app.offers import service
from app.rules.policy import TenantPolicy
from app.tenants.registry import TenantConfig

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("offers", "Get your personalised offer"),
    BotCommand("profile", "See your shopper profile"),
    BotCommand("switch", "Become a different kind of shopper"),
    BotCommand("stop", "Pause offers"),
    BotCommand("resume", "Turn offers back on"),
    BotCommand("help", "What this bot does"),
]


@dataclass(frozen=True)
class BotContext:
    tenant: TenantConfig
    tenant_id: int
    policy: TenantPolicy

    @property
    def coupon_prefix(self) -> str:
        return self.tenant.slug[:2].upper()


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]


def _now() -> datetime:
    return datetime.now(IST)


def _segment_keyboard(bc: BotContext) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(s.label, callback_data=f"seg:{s.key}") for s in bc.policy.segments.segments]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("🎲 Surprise me", callback_data="seg:*")])
    return InlineKeyboardMarkup(rows)


MENU = InlineKeyboardMarkup([[InlineKeyboardButton("🎁 Get my offer", callback_data="offer"),
                              InlineKeyboardButton("🔄 Switch shopper", callback_data="switch")]])


def _offer_keyboard(decision_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Claim", callback_data=f"claim:{decision_id}"),
         InlineKeyboardButton("Not now", callback_data=f"dismiss:{decision_id}")],
        [InlineKeyboardButton("🔕 Stop offers", callback_data="stop")],
    ])


async def _user(update: Update, bc: BotContext) -> dict:
    tg = update.effective_user
    return await asyncio.to_thread(service.touch_user, bc.tenant_id, update.effective_chat.id,
                                   tg.full_name if tg else None, _now())


async def _send_profile(update: Update, bc: BotContext, customer_id: int, edit: bool = False) -> None:
    cust, f = await asyncio.to_thread(service.load_profile, bc.tenant_id, customer_id, bc.policy, _now())
    seg = next(s for s in bc.policy.segments.segments if s.key == f["segment"])
    text = templates.profile_card(cust, f, seg.label, seg.description, bc.policy.catalog.labels)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=MENU)
    else:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    user = await _user(update, bc)
    if user["customer_id"]:
        await update.effective_chat.send_message(f"👋 Welcome back to {bc.tenant.name}!")
        await _send_profile(update, bc, user["customer_id"])
        return
    first = update.effective_user.first_name if update.effective_user else "there"
    await update.effective_chat.send_message(templates.welcome(bc.tenant.name, first), parse_mode=ParseMode.HTML,
                                             reply_markup=_segment_keyboard(bc))


async def switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    await _user(update, bc)
    await update.effective_chat.send_message("Which kind of shopper would you like to be?",
                                             reply_markup=_segment_keyboard(bc))


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    user = await _user(update, bc)
    if not user["customer_id"]:
        await start(update, context)
        return
    await _send_profile(update, bc, user["customer_id"])


async def offers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    user = await _user(update, bc)
    if not user["customer_id"]:
        await start(update, context)
        return
    await update.effective_chat.send_action("typing")
    result = await asyncio.to_thread(service.request_offer, bc.tenant_id, user["customer_id"], bc.policy,
                                     bc.tenant.name, bc.coupon_prefix, "on_demand", _now())
    if result.kind in ("offer", "repeat"):
        if result.kind == "repeat":
            await update.effective_chat.send_message("Your current offer is still live 👇")
        msg = await update.effective_chat.send_message(result.message, parse_mode=ParseMode.HTML,
                                                       reply_markup=_offer_keyboard(result.decision_id))
        await asyncio.to_thread(service.set_message_id, result.decision_id, msg.message_id)
    else:
        await update.effective_chat.send_message(templates.no_offer(result.kind, result.next_time),
                                                 parse_mode=ParseMode.HTML)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    await _user(update, bc)
    await asyncio.to_thread(service.set_opt_out, bc.tenant_id, update.effective_chat.id, True)
    await update.effective_chat.send_message("🔕 Offers paused. You won't hear from us. Send /resume any time.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    await _user(update, bc)
    await asyncio.to_thread(service.set_opt_out, bc.tenant_id, update.effective_chat.id, False)
    await update.effective_chat.send_message("🔔 Offers are back on. Try /offers.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(templates.HELP, parse_mode=ParseMode.HTML)


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message("I work with buttons and commands. Try /offers or /help 🙂")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    q = update.callback_query
    data = q.data or ""
    await _user(update, bc)
    chat_id = update.effective_chat.id

    if data.startswith("seg:"):
        seg = data[4:]
        cid = await asyncio.to_thread(service.assign_persona, bc.tenant_id, chat_id, None if seg == "*" else seg)
        if cid is None:
            await q.answer("All shoppers of that type are taken. Pick another!", show_alert=True)
            return
        await q.answer("You're all set!")
        await _send_profile(update, bc, cid, edit=True)
    elif data == "offer":
        await q.answer()
        await offers(update, context)
    elif data == "switch":
        await q.answer()
        await switch(update, context)
    elif data == "stop":
        await q.answer()
        await q.edit_message_reply_markup(None)
        await stop(update, context)
    elif data.startswith(("claim:", "dismiss:")):
        action, _, raw_id = data.partition(":")
        response = "claimed" if action == "claim" else "dismissed"
        status, info = await asyncio.to_thread(service.respond, bc.tenant_id, chat_id, int(raw_id), response, _now())
        if status == "not_yours":
            await q.answer("This offer isn't linked to your profile.", show_alert=True)
            return
        await q.edit_message_reply_markup(None)
        if status == "expired":
            await q.answer("Sorry, this offer has expired.", show_alert=True)
        elif status == "done":
            await q.answer(f"Already {info['response']}.")
        elif response == "claimed":
            await q.answer("Claimed! 🎉")
            await update.effective_chat.send_message(
                f"🎉 <b>{info['headline']}</b> is yours!\nUse code <code>{info['coupon']}</code> at checkout.\n"
                f"<i>{info['terms']}</i>\n\n<i>(Demo store: nothing is charged.)</i>", parse_mode=ParseMode.HTML)
        else:
            await q.answer("No problem!")
            await update.effective_chat.send_message("👍 No problem. We'll keep the next one relevant.")
    else:
        await q.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Bot error", exc_info=context.error)


def build_application(bc: BotContext, webhook: bool) -> Application:
    builder = Application.builder().token(bc.tenant.bot_token)
    if webhook:
        builder = builder.updater(None)
    app = builder.build()
    app.bot_data["ctx"] = bc
    for name, fn in [("start", start), ("offers", offers), ("offer", offers), ("profile", profile),
                     ("switch", switch), ("stop", stop), ("resume", resume), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    app.add_error_handler(on_error)
    return app
