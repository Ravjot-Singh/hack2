"""Tenant (business) registry.

Businesses live in the `tenants` table: self-service sign-ups are created there by the operator portal.
Folders under tenants/ (except ones starting with "_") are *file tenants* such as the ShopKart demo: on startup
`sync_tenants` imports their name, catalog and customer groups into the database, and their bot token can still
come from an env var. Everything else reads `get_tenants()`, which returns the same `TenantConfig` shape for both.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR, TENANTS_DIR
from app.db.models import Tenant
from app.db.session import session_scope
from app.security import decrypt_secret

log = logging.getLogger(__name__)

# New businesses start from a copy of this demo store's catalog, customer groups and rules.
TEMPLATE_DIR = TENANTS_DIR / "shopkart"
CACHE_SECONDS = 5


class TenantConfig(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    enabled: bool = True
    bot_token_env: str | None = None  # file tenants: env var holding the token
    brand_voice: str = "friendly, warm, short sentences, a few emoji"
    currency: str = "INR"
    timezone: str = "Asia/Kolkata"
    path: Path | None = None  # file tenants only
    id: int | None = None
    coupon_prefix: str | None = None
    bot_token_enc: str | None = None
    bot_username: str | None = None
    demo_chat_id: int | None = None  # demo phone: gets a copy of every message to a demo customer

    @property
    def bot_token(self) -> str | None:
        return decrypt_secret(self.bot_token_enc) or (_secret(self.bot_token_env) if self.bot_token_env else None)

    @property
    def prefix(self) -> str:
        return self.coupon_prefix or self.slug[:2].upper()


def _secret(name: str) -> str | None:
    # Real env vars win (Render); fall back to the local .env file.
    return os.environ.get(name) or dotenv_values(ROOT_DIR / ".env").get(name) or None


def load_tenants(tenants_dir: Path = TENANTS_DIR) -> dict[str, TenantConfig]:
    """File tenants only (tenants/<slug>/tenant.yaml)."""
    tenants: dict[str, TenantConfig] = {}
    for folder in sorted(p for p in tenants_dir.iterdir() if p.is_dir()):
        if folder.name.startswith("_"):
            continue
        data = yaml.safe_load((folder / "tenant.yaml").read_text(encoding="utf-8"))
        cfg = TenantConfig(**data, path=folder)
        if cfg.slug != folder.name:
            raise ValueError(f"tenants/{folder.name}/tenant.yaml has slug '{cfg.slug}'; it must match the folder name")
        if cfg.enabled:
            tenants[cfg.slug] = cfg
    return tenants


_lock = threading.Lock()
_cache: tuple[float, dict[str, TenantConfig]] | None = None


def clear_tenant_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def get_tenants() -> dict[str, TenantConfig]:
    """All active businesses (database), merged with file-tenant extras. Falls back to files if the DB is down."""
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    files = load_tenants()
    try:
        with session_scope() as s:
            rows = s.scalars(select(Tenant).where(Tenant.status == "active").order_by(Tenant.id)).all()
            tenants = {}
            for t in rows:
                f = files.get(t.slug)
                tenants[t.slug] = TenantConfig(
                    slug=t.slug, name=t.name, brand_voice=t.brand_voice, currency=t.currency, id=t.id,
                    coupon_prefix=t.coupon_prefix, bot_token_enc=t.bot_token_enc, bot_username=t.bot_username,
                    demo_chat_id=t.demo_chat_id,
                    bot_token_env=f.bot_token_env if f else None, path=f.path if f else None)
    except Exception as exc:
        log.warning("Could not read businesses from the database, using tenant files: %s", exc)
        return files
    tenants = tenants or files  # before the first import
    with _lock:
        _cache = (time.monotonic(), tenants)
    return tenants


def catalog_from_file(path: Path) -> dict:
    cats = yaml.safe_load(path.read_text(encoding="utf-8"))["categories"]
    return {k: {"label": v.get("label", k), "margin_pct": float(v["margin_pct"])} for k, v in cats.items()}


def sync_tenants(session: Session, tenants: dict[str, TenantConfig] | None = None) -> None:
    """Make sure every file tenant has a database row with its catalog and customer groups."""
    tenants = tenants if tenants is not None else load_tenants()
    existing = {t.slug: t for t in session.scalars(select(Tenant))}
    for slug, cfg in tenants.items():
        row = existing.get(slug)
        if row is None:
            row = Tenant(slug=slug, name=cfg.name)
            session.add(row)
        row.name = cfg.name
        row.brand_voice = cfg.brand_voice
        row.currency = cfg.currency
        row.coupon_prefix = row.coupon_prefix or slug[:2].upper()
        if cfg.path is not None:
            if row.catalog is None and (cfg.path / "catalog.yaml").exists():
                row.catalog = catalog_from_file(cfg.path / "catalog.yaml")
            if row.segments_yaml is None and (cfg.path / "segments.yaml").exists():
                row.segments_yaml = (cfg.path / "segments.yaml").read_text(encoding="utf-8")
    clear_tenant_cache()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return slug or "store"


def _prefix(name: str, taken: set[str]) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name.upper()) or ["ST"]
    base = ("".join(w[0] for w in words) if len(words) > 1 else words[0])[:3]
    base = base if len(base) >= 2 else (base + "X")[:2]
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base[:2]}{n}", n + 1
    return candidate


def create_tenant(session: Session, name: str) -> Tenant:
    """A new business, starting from the demo store's catalog and customer groups (rules default likewise)."""
    rows = session.scalars(select(Tenant)).all()
    slugs = {t.slug for t in rows}
    slug, n = _slugify(name), 2
    base = slug
    while slug in slugs or (TENANTS_DIR / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    tenant = Tenant(slug=slug, name=name.strip()[:120], coupon_prefix=_prefix(name, {t.coupon_prefix for t in rows}),
                    catalog=catalog_from_file(TEMPLATE_DIR / "catalog.yaml"),
                    segments_yaml=(TEMPLATE_DIR / "segments.yaml").read_text(encoding="utf-8"))
    session.add(tenant)
    session.flush()
    clear_tenant_cache()
    return tenant
