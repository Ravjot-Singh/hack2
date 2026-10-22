"""Runs one Telegram bot per business that has connected one, by webhook (Render) or polling (local).

`BotManager.sync()` compares the running bots with the businesses' current tokens and starts, restarts or
stops bots to match, so a token pasted in the portal's Settings takes effect without a restart. The web app
syncs right after a token change (webhook mode); the local polling runner re-syncs every 30 seconds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application

from app.bot.handlers import COMMANDS, BotContext, build_application
from app.data.generator import IST
from app.db.session import session_scope
from app.tenants.registry import clear_tenant_cache, get_tenants, sync_tenants
from app.triggers.events import dispatch_deferred

log = logging.getLogger(__name__)
POLL_RESYNC_SECONDS = 30


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


class BotManager:
    def __init__(self, webhook_base: str | None, secret: str):
        self.webhook_base = webhook_base.rstrip("/") if webhook_base else None
        self.secret = secret
        self.apps: dict[str, Application] = {}  # also used by the webhook route, so keep the same dict
        self._tokens: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        async with self._lock:
            clear_tenant_cache()
            tenants = await asyncio.to_thread(get_tenants)
            wanted = {slug: cfg for slug, cfg in tenants.items() if cfg.bot_token}
            for slug in list(self.apps):
                if slug not in wanted or wanted[slug].bot_token != self._tokens[slug]:
                    await self._stop(slug)
            for slug, cfg in wanted.items():
                if slug not in self.apps:
                    try:
                        await self._start(slug, cfg)
                    except Exception:  # one bad token must not stop the other businesses' bots
                        log.exception("Could not start the bot for %s", slug)

    async def _start(self, slug: str, cfg) -> None:
        app = build_application(BotContext(cfg, cfg.id), webhook=bool(self.webhook_base))
        await app.initialize()
        await app.bot.set_my_commands(COMMANDS)
        if self.webhook_base:
            await app.bot.set_webhook(f"{self.webhook_base}/tg/{slug}/{self.secret}", secret_token=self.secret,
                                      allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        else:
            await app.bot.delete_webhook(drop_pending_updates=True)
        await app.start()
        if not self.webhook_base:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        self.apps[slug], self._tokens[slug] = app, cfg.bot_token
        self._tasks[slug] = asyncio.create_task(_dispatch_loop(app))
        log.info("Bot @%s for %s is running (%s).", app.bot.username, slug,
                 "webhook" if self.webhook_base else "polling")

    async def _stop(self, slug: str) -> None:
        app = self.apps.pop(slug)
        self._tokens.pop(slug, None)
        task = self._tasks.pop(slug, None)
        if task:
            task.cancel()
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("Error while stopping the bot for %s", slug)
        log.info("Bot for %s stopped.", slug)

    async def stop_all(self) -> None:
        async with self._lock:
            for slug in list(self.apps):
                await self._stop(slug)


def import_file_tenants() -> None:
    with session_scope() as s:
        sync_tenants(s)


async def run_polling_forever() -> None:
    """Local mode: poll Telegram for every connected bot and pick up token changes every 30 seconds."""
    await asyncio.to_thread(import_file_tenants)
    manager = BotManager(None, "")
    await manager.sync()
    if not manager.apps:
        log.warning("No business has a bot connected yet. Connect one in the portal (Settings); "
                    "this runner checks again every %ss.", POLL_RESYNC_SECONDS)
    try:
        while True:
            await asyncio.sleep(POLL_RESYNC_SECONDS)
            await manager.sync()
    finally:
        await manager.stop_all()
