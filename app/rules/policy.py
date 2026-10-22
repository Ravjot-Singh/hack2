"""Everything that defines one business's offer policy: rules, catalog economics and customer groups.

Sources, newest first:
  catalog + customer groups: the business's row in `tenants` (edited in the portal), else its tenant folder;
  rules: the newest `rules_versions` row (saved from a dashboard), else tenants/<slug>/rules.yaml, else the
  demo store's rules adapted to the business's categories.
`current_policy` re-checks the database every few seconds, so an edit reaches every process (bot, web app)
without a restart.
"""

from __future__ import annotations

import copy
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
from app.rules.schema import DYNAMIC_CATEGORIES, RulesConfig, load_rules, parse_rules
from app.tenants.registry import TEMPLATE_DIR, catalog_from_file

log = logging.getLogger(__name__)
RECHECK_SECONDS = 10
DEFAULT_CAP_SHARE_OF_MARGIN = 0.6


@dataclass(frozen=True)
class TenantPolicy:
    rules: RulesConfig
    catalog: CatalogInfo
    segments: SegmentConfig
    version_id: int | None = None  # None = rules from a file / the default template


def load_policy(tenant_dir: Path, rules_data: dict | None = None, version_id: int | None = None) -> TenantPolicy:
    """Policy purely from a tenant folder (used by tests and file-based tools)."""
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


def catalog_info(catalog: dict) -> CatalogInfo:
    return CatalogInfo(margins={k: float(v["margin_pct"]) for k, v in catalog.items()},
                       labels={k: v.get("label", k) for k, v in catalog.items()})


def default_rules(catalog: dict) -> dict:
    """The demo store's rules, adapted to another catalog: caps for its categories (kept from the template when
    the category exists there, else ~60% of margin) and fixed-category offers moved to the whole store."""
    data = yaml.safe_load((TEMPLATE_DIR / "rules.yaml").read_text(encoding="utf-8"))
    template_caps = data["limits"]["max_discount_pct"]
    caps = {}
    for cat, info in catalog.items():
        margin = float(info["margin_pct"])
        caps[cat] = min(template_caps.get(cat, round(margin * DEFAULT_CAP_SHARE_OF_MARGIN)), margin)
    max_margin = max((float(v["margin_pct"]) for v in catalog.values()), default=0)
    caps["any"] = min(template_caps["any"], max_margin)
    data["limits"]["max_discount_pct"] = caps
    for offer in data["offers"]:
        for key in ("category", "fallback_category"):
            cat = offer.get(key)
            if cat and cat not in DYNAMIC_CATEGORIES and cat not in catalog:
                offer[key] = "any"
    return data


def build_policy(catalog: dict, segments_yaml: str, rules_data: dict, version_id: int | None = None) -> TenantPolicy:
    info = catalog_info(catalog)
    segments = SegmentConfig(**yaml.safe_load(segments_yaml))
    rules = parse_rules(rules_data, info.margins, {s.key for s in segments.segments})
    return TenantPolicy(rules, info, segments, version_id)


_lock = threading.Lock()
_cache: dict[str, tuple[float, tuple, TenantPolicy]] = {}


def _db_state(slug: str) -> tuple[dict | None, str | None, int, int | None, str | None]:
    """(catalog, segments_yaml, config_version, rules_version_id, rules_yaml) from the database."""
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == slug))
        if t is None:
            return None, None, 0, None, None
        row = s.execute(select(RulesVersion.id, RulesVersion.content).where(RulesVersion.tenant_id == t.id)
                        .order_by(RulesVersion.id.desc()).limit(1)).first()
        return t.catalog, t.segments_yaml, t.config_version, (row[0] if row else None), (row[1] if row else None)


def current_policy(slug: str, tenant_dir: Path | None = None) -> TenantPolicy:
    with _lock:
        cached = _cache.get(slug)
        if cached and time.monotonic() - cached[0] < RECHECK_SECONDS:
            return cached[2]
    try:
        catalog, seg_yaml, config_version, version_id, rules_yaml = _db_state(slug)
    except Exception as exc:  # database down: keep serving what we have, else files
        log.warning("Could not read policy for %s from the database: %s", slug, exc)
        if cached:
            return cached[2]
        catalog, seg_yaml, config_version, version_id, rules_yaml = None, None, 0, None, None
    key = (config_version, version_id)
    if cached and cached[1] == key:
        policy = cached[2]
    else:
        folder = tenant_dir if tenant_dir is not None and tenant_dir.exists() else None
        catalog = catalog or catalog_from_file((folder or TEMPLATE_DIR) / "catalog.yaml")
        seg_yaml = seg_yaml or ((folder or TEMPLATE_DIR) / "segments.yaml").read_text(encoding="utf-8")
        file_rules = (yaml.safe_load((folder / "rules.yaml").read_text(encoding="utf-8"))
                      if folder and (folder / "rules.yaml").exists() else default_rules(catalog))
        policy = None
        if rules_yaml is not None:
            try:
                policy = build_policy(catalog, seg_yaml, yaml.safe_load(rules_yaml), version_id)
            except Exception as exc:  # a stored version that no longer validates: fall back
                log.error("Saved rules version %s for %s is invalid, using defaults: %s", version_id, slug, exc)
        policy = policy or build_policy(catalog, seg_yaml, copy.deepcopy(file_rules))
    with _lock:
        _cache[slug] = (time.monotonic(), key, policy)
    return policy


def clear_policy_cache(slug: str | None = None) -> None:
    with _lock:
        if slug is None:
            _cache.clear()
        else:
            _cache.pop(slug, None)


def save_rules(slug: str, tenant_id: int, tenant_dir: Path | None, data: dict, user: str, note: str | None,
               now: datetime) -> TenantPolicy:
    """Validate and store a new rules version. Raises (pydantic ValidationError / RulesError) if invalid."""
    current = current_policy(slug, tenant_dir)
    rules = parse_rules(data, current.catalog.margins, {s.key for s in current.segments.segments})
    with session_scope() as s:
        s.add(RulesVersion(tenant_id=tenant_id, created_at=now, created_by=user[:80], note=(note or "")[:200] or None,
                           content=yaml.safe_dump(data, sort_keys=False, allow_unicode=True)))
    clear_policy_cache(slug)
    return TenantPolicy(rules, current.catalog, current.segments)
