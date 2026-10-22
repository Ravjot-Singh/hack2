"""The one condition grammar used by segments.yaml and rules.yaml.

A condition is a measure name plus _min (at least) or _max (at most): {"orders_365_min": 4}.
"""

from __future__ import annotations

from app.features.customer import FEATURE_LABELS

PERCENT_MEASURES = {"discount_share", "return_rate", "churn_risk", "buy_anyway_prob", "deal_affinity"}


def validate_conditions(when: dict[str, float]) -> dict[str, float]:
    for cond in when:
        measure, _, bound = cond.rpartition("_")
        if bound not in ("min", "max") or measure not in FEATURE_LABELS:
            raise ValueError(
                f"'{cond}' is not a valid condition. Use a measure name plus _min or _max, "
                f"e.g. orders_365_min. Measures: {', '.join(FEATURE_LABELS)}"
            )
    return when


def fmt(measure: str, v: float) -> str:
    return f"{v:.0%}" if measure in PERCENT_MEASURES else f"{v:,.0f}"


def check_conditions(features: dict, when: dict[str, float]) -> tuple[bool, list[str]]:
    """Return (all hold, reasons). On failure the reasons list holds only the failed condition."""
    reasons = []
    for cond, threshold in when.items():
        measure, _, bound = cond.rpartition("_")
        value = features.get(measure)
        if value is None:  # only recency: never ordered = infinitely long ago
            value = float("inf")
        shown = "never" if value == float("inf") else fmt(measure, value)
        sign = ">=" if bound == "min" else "<="
        text = f"{FEATURE_LABELS[measure]}: {shown} (needs {sign} {fmt(measure, threshold)})"
        if not (value >= threshold if bound == "min" else value <= threshold):
            return False, [text]
        reasons.append(text.replace("needs ", ""))
    return True, reasons
