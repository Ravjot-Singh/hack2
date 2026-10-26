"""FastAPI entry point: health checks, Telegram webhooks, the operator portal (/app) and our admin console (/admin).

Bots start here only when PUBLIC_BASE_URL is set (Render). Locally, run scripts/run_bot.py (polling) instead.
"""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from telegram import Update

from app.bot.runner import BotManager
from app.config import get_settings
from app.dashboard.routes import STATIC_DIR, router as dashboard_router
from app.portal.data_routes import router as portal_data_router
from app.portal.deals_routes import router as portal_deals_router
from app.portal.routes import router as portal_router
from app.portal.routes import templates as portal_templates
from app.portal.settings_routes import router as portal_settings_router
from starlette.middleware.sessions import SessionMiddleware
from app.db.session import get_engine, session_scope
from app.ingest.jobs import mark_interrupted_jobs
from app.offers.deals import mark_interrupted_deals
from app.tenants.registry import get_tenants, sync_tenants

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bots = {}
    app.state.bot_manager = None
    try:
        with session_scope() as s:
            sync_tenants(s)
        mark_interrupted_deals()
        mark_interrupted_jobs()
    except Exception as exc:  # app should still boot so /health can report the problem
        log.warning("Could not sync tenants to database: %s", exc)
    if settings.public_base_url:  # public server: this process runs every business's bot by webhook
        manager = BotManager(settings.public_base_url, settings.webhook_secret)
        app.state.bot_manager, app.state.bots = manager, manager.apps
        await manager.sync()
    yield
    if app.state.bot_manager:
        await app.state.bot_manager.stop_all()


app = FastAPI(title="Personalised Offer Engine", lifespan=lifespan)
_settings = get_settings()
app.add_middleware(SessionMiddleware, secret_key=_settings.session_secret, session_cookie="oe_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=bool(_settings.public_base_url))
SECURITY_HEADERS = {
    # No other site may frame our pages (a framed portal could be clicked into launching a deal).
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(dashboard_router)
app.include_router(portal_router)
app.include_router(portal_deals_router)
app.include_router(portal_data_router)
app.include_router(portal_settings_router)


ERROR_TEXT = {
    400: ("That didn't work", "Something in that request wasn't right. Go back and try again."),
    403: ("Not allowed", "That action was blocked for your security. Reload the page and try again."),
    404: ("Not found", "This page doesn't exist, or it belongs to another business."),
    503: ("Not available yet", "This part of the service isn't set up yet."),
}


def _wants_portal_page(request: Request) -> bool:
    return request.url.path.startswith("/app") and "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def portal_http_errors(request: Request, exc: StarletteHTTPException):
    """Friendly pages for people using the portal in a browser; JSON for everything else (unchanged)."""
    if exc.status_code < 400 or not _wants_portal_page(request):
        return await http_exception_handler(request, exc)
    title, message = ERROR_TEXT.get(exc.status_code, ("Something went wrong", "Please try again."))
    if exc.status_code in (400, 503) and isinstance(exc.detail, str):
        message = exc.detail  # these details are written for people
    return portal_templates.TemplateResponse(request, "error.html", {"title": title, "message": message},
                                             status_code=exc.status_code)


@app.exception_handler(Exception)
async def portal_crash(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    if _wants_portal_page(request):
        return portal_templates.TemplateResponse(
            request, "error.html", {"title": "Something went wrong",
                                    "message": "Sorry, that didn't work. Please try again in a moment."},
            status_code=500)
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/app")


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
