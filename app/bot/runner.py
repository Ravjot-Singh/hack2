"""Builds one bot per tenant that has a token, and runs them by webhook (Render) or polling (local)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from telegram import Update
from telegram.ext import Application

from app.bot.handlers import COMMANDS, BotContext, build_application
from app.data.generator import IST
from app.db.models import Tenant
from app.db.session import session_scope
from app.rules.policy import load_policy
from app.tenants.registry import get_tenants, sync_tenants
from app.triggers.events import dispatch_deferred

log = logging.getLogger(__name__)


def build_bots(webhook: bool) -> dict[str, Application]:
    tenants = get_tenants()
    with session_scope() as s:
        sync_tenants(s, tenants)
        s.flush()
        ids = dict(s.execute(select(Tenant.slug, Tenant.id)).all())
    bots = {}
    for slug, cfg in tenants.items():
        if not cfg.bot_token:
            log.warning("Tenant %s has no bot token in %s; its bot is not started.", slug, cfg.bot_token_env)
            continue
        bots[slug] = build_application(BotContext(cfg, ids[slug], load_policy(cfg.path)), webhook)
    return bots


async def start_bots(bots: dict[str, Application], webhook_base: str | None, secret: str) -> None:
    for slug, app in bots.items():
        await app.initialize()
        await app.bot.set_my_commands(COMMANDS)
        if webhook_base:
            await app.bot.set_webhook(f"{webhook_base.rstrip('/')}/tg/{slug}/{secret}", secret_token=secret,
                                      allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        else:
            await app.bot.delete_webhook(drop_pending_updates=True)
        await app.start()
        if not webhook_base:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Bot @%s for tenant %s is running (%s).", app.bot.username, slug,
                 "webhook" if webhook_base else "polling")
        _tasks.append(asyncio.create_task(_dispatch_loop(app)))


_tasks: list[asyncio.Task] = []


async def _dispatch_loop(app: Application) -> None:
    """Every minute, send offers that quiet hours held back."""
    bc = app.bot_data["ctx"]
    while True:
        try:
            sent = await dispatch_deferred(bc, app.bot, datetime.now(IST))
            if sent:
                log.info("Sent %d deferred offer(s) for %s", sent, bc.tenant.slug)
        except Exception:
            log.exception("Deferred dispatch failed")
        await asyncio.sleep(60)


async def stop_bots(bots: dict[str, Application]) -> None:
    for t in _tasks:
        t.cancel()
    _tasks.clear()
    for app in bots.values():
        if app.updater and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def run_polling_forever() -> None:
    bots = build_bots(webhook=False)
    if not bots:
        raise SystemExit("No tenant has a bot token. Set it in .env (see tenants/<slug>/tenant.yaml bot_token_env).")
    await start_bots(bots, None, "")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_bots(bots)
