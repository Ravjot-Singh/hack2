"""FastAPI entry point: health checks now; Telegram webhooks and the admin dashboard are added in later stages."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.db.session import get_engine, session_scope
from app.tenants.registry import get_tenants, sync_tenants

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with session_scope() as s:
            sync_tenants(s, get_tenants())
    except Exception as exc:  # app should still boot so /health can report the problem
        log.warning("Could not sync tenants to database: %s", exc)
    yield


app = FastAPI(title="Personalised Offer Engine", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "tenants": sorted(get_tenants())}


@app.get("/health/db")
def health_db() -> dict:
    with get_engine().connect() as conn:
        conn.execute(text("select 1"))
    return {"database": "ok", "dialect": get_engine().dialect.name}
