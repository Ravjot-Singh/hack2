"""Sending offer messages to Telegram, and logging every send (delivered or failed) in `deliveries`.

`deliver()` is the one way a proactive offer reaches a customer's chat. `Mirror` copies offers to the business's
demo phone, so a single Telegram account can watch what every demo customer receives.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from html import escape

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.offers import service

log = logging.getLogger(__name__)
MIRROR_BATCH_LIMIT = 10  # per deal / automation run; the rest are summarised in one message
MIRROR_GAP_SECONDS = 1.0  # stay well inside Telegram's per-chat rate limit


def offer_keyboard(decision_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Claim", callback_data=f"claim:{decision_id}"),
         InlineKeyboardButton("Not now", callback_data=f"dismiss:{decision_id}")],
        [InlineKeyboardButton("🔕 Stop offers", callback_data="stop")],
    ])


def mirror_keyboard(decision_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Claim (as them)", callback_data=f"claim:{decision_id}"),
                                  InlineKeyboardButton("Not now", callback_data=f"dismiss:{decision_id}")]])


async def with_bot(running_bots: dict, slug: str, token: str | None, fn):
    """Run fn(bot) with the business's running bot if this process has one, else a short-lived client."""
    running = running_bots.get(slug)
    if running is not None:
        return await fn(running.bot)
    if not token:
        raise LookupError("no bot connected")
    async with Bot(token) as bot:
        return await fn(bot)


async def push_offer(bot: Bot, chat_id: int, decision_id: int, message: str, lead: str | None = None) -> int:
    """Send to the customer's chat and log it. Raises TelegramError if Telegram refuses (after logging)."""
    try:
        if lead:
            await bot.send_message(chat_id, lead, parse_mode=ParseMode.HTML)
        msg = await bot.send_message(chat_id, message, parse_mode=ParseMode.HTML,
                                     reply_markup=offer_keyboard(decision_id))
    except TelegramError as exc:
        await asyncio.to_thread(service.record_delivery, decision_id, chat_id, "customer", False, error=str(exc))
        raise
    await asyncio.to_thread(service.record_delivery, decision_id, chat_id, "customer", True, msg.message_id)
    return msg.message_id


async def deliver(bot: Bot, chat_id: int, decision_id: int, message: str) -> bool:
    """push_offer that never raises: the failure is in the log (and on the Messages page)."""
    try:
        await push_offer(bot, chat_id, decision_id, message)
        return True
    except TelegramError as exc:
        log.warning("Could not deliver decision %s to %s: %s", decision_id, chat_id, exc)
        return False


class Mirror:
    """Copies offers for demo customers to the demo phone: all of them for single sends (limit None), else the
    first `limit` and then one summary of the rest."""

    def __init__(self, demo_chat_id: int | None, limit: int | None = None, label: str = ""):
        self.chat_id, self.limit, self.label = demo_chat_id, limit, label
        self.sent = 0
        self.skipped: Counter = Counter()

    async def copy(self, bot: Bot | None, decision_id: int) -> None:
        if not self.chat_id or bot is None:
            return
        info = await asyncio.to_thread(service.mirror_info, decision_id, self.chat_id)
        if info is None:
            return
        if self.limit is not None and self.sent >= self.limit:
            self.skipped[info["channel"]] += 1
            return
        lead = (f"📨 <b>Demo copy</b> · for <b>{escape(info['name'])}</b> ({escape(info['ref'])})\n"
                f"{escape(info['trigger'])} · {escape(info['channel'])}")
        try:
            if self.sent:
                await asyncio.sleep(MIRROR_GAP_SECONDS)
            await bot.send_message(self.chat_id, lead, parse_mode=ParseMode.HTML)
            msg = await bot.send_message(self.chat_id, info["message"], parse_mode=ParseMode.HTML,
                                         reply_markup=mirror_keyboard(decision_id))
            await asyncio.to_thread(service.record_delivery, decision_id, self.chat_id, "mirror", True, msg.message_id)
        except TelegramError as exc:
            log.warning("Mirror of decision %s failed: %s", decision_id, exc)
            await asyncio.to_thread(service.record_delivery, decision_id, self.chat_id, "mirror", False,
                                    error=str(exc))
        self.sent += 1

    async def finish(self, bot: Bot | None) -> None:
        """One summary for the copies that were not sent individually."""
        total = sum(self.skipped.values())
        if not total or not self.chat_id or bot is None:
            return
        parts = ", ".join(f"{n} {k}" for k, n in self.skipped.most_common())
        text = (f"📨 <b>Demo copy</b> · {escape(self.label) + ': ' if self.label else ''}"
                f"…and {total} more offers ({escape(parts)}). See <b>Messages</b> in the portal for each one.")
        try:
            await bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            log.warning("Mirror summary failed: %s", exc)
