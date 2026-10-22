"""Everything that defines one tenant's offer policy: rules, catalog economics and segments.

Rules come from tenants/<slug>/rules.yaml unless a newer version was saved from the dashboard (rules_versions
table). `current_policy` re-checks the database every few seconds, so a saved edit reaches every process
(bot, web app) without a restart.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select

from app.data.generator import load_catalog
from app.db.models import RulesVersion, Tenant
from app.db.session import session_scope
from app.features.segments import SegmentConfig, load_segment_config
from app.rules.engine import CatalogInfo
from app.rules.schema import RulesConfig, load_rules, parse_rules

log = logging.getLogger(__name__)
RECHECK_SECONDS = 10


@dataclass(frozen=True)
class TenantPolicy:
    rules: RulesConfig
    catalog: CatalogInfo
    segments: SegmentConfig
    version_id: int | None = None  # None = the rules.yaml file


def load_policy(tenant_dir: Path, rules_data: dict | None = None, version_id: int | None = None) -> TenantPolicy:
    cats = load_catalog(tenant_dir / "catalog.yaml")["categories"]
    catalog = CatalogInfo(
        margins={k: float(v["margin_pct"]) for k, v in cats.items()},
        labels={k: v.get("label", k) for k, v in cats.items()},
    )
    segments = load_segment_config(tenant_dir / "segments.yaml")
    keys = {s.key for s in segments.segments}
    rules = (parse_rules(rules_data, catalog.margins, keys) if rules_data is not None
             else load_rules(tenant_dir / "rules.yaml", catalog.margins, keys))
    return TenantPolicy(rules, catalog, segments, version_id)


_lock = threading.Lock()
_cache: dict[str, tuple[float, TenantPolicy]] = {}


def _latest_version(slug: str) -> tuple[int, str] | None:
    with session_scope() as s:
        row = s.execute(select(RulesVersion.id, RulesVersion.content).join(Tenant, Tenant.id == RulesVersion.tenant_id)
                        .where(Tenant.slug == slug).order_by(RulesVersion.id.desc()).limit(1)).first()
        return tuple(row) if row else None


def current_policy(slug: str, tenant_dir: Path) -> TenantPolicy:
    with _lock:
        cached = _cache.get(slug)
        if cached and time.monotonic() - cached[0] < RECHECK_SECONDS:
            return cached[1]
    try:
        latest = _latest_version(slug)
    except Exception as exc:  # database down: keep serving what we have
        log.warning("Could not check saved rules for %s: %s", slug, exc)
        latest = (cached[1].version_id, None) if cached and cached[1].version_id else None
    if cached and (latest[0] if latest else None) == cached[1].version_id:
        policy = cached[1]
    elif latest and latest[1] is not None:
        try:
            policy = load_policy(tenant_dir, yaml.safe_load(latest[1]), latest[0])
        except Exception as exc:  # a stored version that no longer validates: fall back to the file
            log.error("Saved rules version %s for %s is invalid, using rules.yaml: %s", latest[0], slug, exc)
            policy = load_policy(tenant_dir)
    else:
        policy = load_policy(tenant_dir)
    with _lock:
        _cache[slug] = (time.monotonic(), policy)
    return policy


def save_rules(slug: str, tenant_id: int, tenant_dir: Path, data: dict, user: str, note: str | None,
               now: datetime) -> TenantPolicy:
    """Validate and store a new rules version. Raises (pydantic ValidationError / RulesError) if invalid."""
    policy = load_policy(tenant_dir, data)
    with session_scope() as s:
        s.add(RulesVersion(tenant_id=tenant_id, created_at=now, created_by=user[:80], note=(note or "")[:200] or None,
                           content=yaml.safe_dump(data, sort_keys=False, allow_unicode=True)))
    with _lock:
        _cache.pop(slug, None)
    return policy
