"""Tenant registry.

Each folder under tenants/ (except ones starting with "_") is one store with its own bot,
rules and catalog. Adding a bot = copy tenants/_template, edit tenant.yaml, set its token env var.
"""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR, TENANTS_DIR
from app.db.models import Tenant


class TenantConfig(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    enabled: bool = True
    bot_token_env: str
    brand_voice: str = "friendly, warm, short sentences, a few emoji"
    currency: str = "INR"
    timezone: str = "Asia/Kolkata"
    path: Path | None = None

    @property
    def bot_token(self) -> str | None:
        return _secret(self.bot_token_env)


def _secret(name: str) -> str | None:
    # Real env vars win (Render); fall back to the local .env file.
    return os.environ.get(name) or dotenv_values(ROOT_DIR / ".env").get(name) or None


def load_tenants(tenants_dir: Path = TENANTS_DIR) -> dict[str, TenantConfig]:
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


@lru_cache
def get_tenants() -> dict[str, TenantConfig]:
    return load_tenants()


def sync_tenants(session: Session, tenants: dict[str, TenantConfig]) -> None:
    """Make sure every configured tenant has a row in the database."""
    existing = {t.slug: t for t in session.scalars(select(Tenant))}
    for slug, cfg in tenants.items():
        if slug in existing:
            existing[slug].name = cfg.name
        else:
            session.add(Tenant(slug=slug, name=cfg.name))
