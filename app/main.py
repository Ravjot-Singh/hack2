"""FastAPI entry point: health checks, Telegram webhooks and the admin dashboard (/admin).

Bots start here only when PUBLIC_BASE_URL is set (Render). Locally, run scripts/run_bot.py (polling) instead.
"""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from telegram import Update

from app.bot.runner import build_bots, start_bots, stop_bots
from app.config import get_settings
from app.dashboard.routes import STATIC_DIR, router as dashboard_router
from app.db.session import get_engine, session_scope
from app.tenants.registry import get_tenants, sync_tenants

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bots = {}
    try:
        with session_scope() as s:
            sync_tenants(s, get_tenants())
    except Exception as exc:  # app should still boot so /health can report the problem
        log.warning("Could not sync tenants to database: %s", exc)
    if settings.public_base_url:
        app.state.bots = build_bots(webhook=True)
        await start_bots(app.state.bots, settings.public_base_url, settings.webhook_secret)
    yield
    await stop_bots(app.state.bots)


app = FastAPI(title="Personalised Offer Engine", lifespan=lifespan)
app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(dashboard_router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "tenants": sorted(get_tenants()), "bots": sorted(app.state.bots)}


@app.get("/health/db")
def health_db() -> dict:
    with get_engine().connect() as conn:
        conn.execute(text("select 1"))
    return {"database": "ok", "dialect": get_engine().dialect.name}


@app.post("/tg/{slug}/{secret}")
async def telegram_webhook(slug: str, secret: str, request: Request) -> dict:
    expected = get_settings().webhook_secret
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not (hmac.compare_digest(secret, expected) and hmac.compare_digest(header, expected)):
        raise HTTPException(status_code=404)
    bot_app = app.state.bots.get(slug)
    if bot_app is None:
        raise HTTPException(status_code=404)
    await bot_app.process_update(Update.de_json(await request.json(), bot_app.bot))
    return {"ok": True}
