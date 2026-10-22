"""Rule-based customer segments, defined in tenants/<slug>/segments.yaml so marketing can edit them."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

from app.features.customer import FEATURE_LABELS


class SegmentRule(BaseModel):
    key: str
    label: str
    description: str
    when: dict[str, float] = {}

    @field_validator("when")
    @classmethod
    def known_measures(cls, when: dict[str, float]) -> dict[str, float]:
        for cond in when:
            measure, _, bound = cond.rpartition("_")
            if bound not in ("min", "max") or measure not in FEATURE_LABELS:
                raise ValueError(
                    f"'{cond}' is not a valid condition. Use a measure name plus _min or _max, "
                    f"e.g. orders_365_min. Measures: {', '.join(FEATURE_LABELS)}"
                )
        return when


class SegmentConfig(BaseModel):
    segments: list[SegmentRule]

    @model_validator(mode="after")
    def last_is_catch_all(self) -> SegmentConfig:
        if not self.segments or self.segments[-1].when:
            raise ValueError("The last segment must have no conditions (when: {}) so every customer gets a group.")
        keys = [s.key for s in self.segments]
        if len(keys) != len(set(keys)):
            raise ValueError("Segment keys must be unique.")
        return self

    def label(self, key: str) -> str:
        return next((s.label for s in self.segments if s.key == key), key)


def load_segment_config(path: Path) -> SegmentConfig:
    return SegmentConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


def _fmt(measure: str, v: float) -> str:
    if measure in ("discount_share", "return_rate", "churn_risk", "buy_anyway_prob", "deal_affinity"):
        return f"{v:.0%}"
    return f"{v:,.0f}"


def assign_segment(features: dict, cfg: SegmentConfig) -> tuple[str, str]:
    """Return (segment key, plain-language reason)."""
    for rule in cfg.segments:
        reasons = []
        for cond, threshold in rule.when.items():
            measure, _, bound = cond.rpartition("_")
            value = features.get(measure)
            if value is None:  # only recency: never ordered = infinitely long ago
                value = float("inf")
            ok = value >= threshold if bound == "min" else value <= threshold
            if not ok:
                break
            shown = "never" if value == float("inf") else _fmt(measure, value)
            sign = ">=" if bound == "min" else "<="
            reasons.append(f"{FEATURE_LABELS[measure]}: {shown} ({sign} {_fmt(measure, threshold)})")
        else:
            reason = "; ".join(reasons) if reasons else "did not match any other group"
            return rule.key, reason[:200]
    raise AssertionError("unreachable: last segment is a catch-all")
