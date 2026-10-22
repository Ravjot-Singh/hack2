"""Rule-based customer segments, defined in tenants/<slug>/segments.yaml so marketing can edit them."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

from app.features.conditions import check_conditions, validate_conditions


class SegmentRule(BaseModel):
    key: str
    label: str
    description: str
    when: dict[str, float] = {}

    _check_when = field_validator("when")(validate_conditions)


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


def assign_segment(features: dict, cfg: SegmentConfig) -> tuple[str, str]:
    """Return (segment key, plain-language reason)."""
    for rule in cfg.segments:
        ok, reasons = check_conditions(features, rule.when)
        if ok:
            reason = "; ".join(reasons) if reasons else "did not match any other group"
            return rule.key, reason[:200]
    raise AssertionError("unreachable: last segment is a catch-all")
