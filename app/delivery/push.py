"""Sending offer messages to Telegram (used for on-demand replies and proactive pushes)."""

from __future__ import annotations

import asyncio

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from app.offers import service


def offer_keyboard(decision_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Claim", callback_data=f"claim:{decision_id}"),
         InlineKeyboardButton("Not now", callback_data=f"dismiss:{decision_id}")],
        [InlineKeyboardButton("🔕 Stop offers", callback_data="stop")],
    ])


async def push_offer(bot: Bot, chat_id: int, decision_id: int, message: str, lead: str | None = None) -> int:
    if lead:
        await bot.send_message(chat_id, lead, parse_mode=ParseMode.HTML)
    msg = await bot.send_message(chat_id, message, parse_mode=ParseMode.HTML, reply_markup=offer_keyboard(decision_id))
    await asyncio.to_thread(service.set_message_id, decision_id, msg.message_id)
    return msg.message_id
