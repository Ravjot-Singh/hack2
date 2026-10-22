"""Everything that defines one tenant's offer policy: rules, catalog economics and segments."""

from dataclasses import dataclass
from pathlib import Path

from app.data.generator import load_catalog
from app.features.segments import SegmentConfig, load_segment_config
from app.rules.engine import CatalogInfo
from app.rules.schema import RulesConfig, load_rules


@dataclass(frozen=True)
class TenantPolicy:
    rules: RulesConfig
    catalog: CatalogInfo
    segments: SegmentConfig


def load_policy(tenant_dir: Path) -> TenantPolicy:
    cats = load_catalog(tenant_dir / "catalog.yaml")["categories"]
    catalog = CatalogInfo(
        margins={k: float(v["margin_pct"]) for k, v in cats.items()},
        labels={k: v.get("label", k) for k, v in cats.items()},
    )
    segments = load_segment_config(tenant_dir / "segments.yaml")
    rules = load_rules(tenant_dir / "rules.yaml", catalog.margins, {s.key for s in segments.segments})
    return TenantPolicy(rules, catalog, segments)
