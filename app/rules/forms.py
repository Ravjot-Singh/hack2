"""Turning the rules-editor form (portal and /admin share the same field names) into rules data."""

from __future__ import annotations

import copy

from pydantic import ValidationError

from app.rules.schema import RulesError

FIELD_NAMES = {"value": "amount", "min_order_value": "minimum order", "max_discount_rs": "maximum discount",
               "valid_hours": "valid for", "priority": "priority", "daily_budget_rs": "daily budget",
               "max_discount_rs_per_offer": "most one offer can be worth", "holdout_pct": "control group"}


def rules_form_data(form, base: dict) -> dict:
    """form: submitted FormData; base: current rules as JSON-able dict. Percent limits are entered as %."""
    data = copy.deepcopy(base)
    lim = data["limits"]
    for key in ("max_discount_rs_per_offer", "max_offers_per_customer_per_week", "min_hours_between_offers",
                "same_offer_cooldown_days", "daily_budget_rs", "min_expected_net_rs", "delivery_fee_rs",
                "max_offers_per_customer_per_day"):
        if form.get(f"limits.{key}") not in (None, ""):
            lim[key] = int(float(form.get(f"limits.{key}")))
    if "limits.max_total_spend_per_day_rs" in form:  # empty = automatic (twice the daily budget)
        raw = form.get("limits.max_total_spend_per_day_rs")
        lim["max_total_spend_per_day_rs"] = int(float(raw)) if raw not in (None, "") else None
    for key in ("max_buy_anyway_prob_for_discounts", "max_return_rate_for_discounts"):
        if form.get(f"limits.{key}") not in (None, ""):
            lim[key] = float(form.get(f"limits.{key}")) / 100
    for cat in list(lim["max_discount_pct"]):
        if form.get(f"cap.{cat}") not in (None, ""):
            lim["max_discount_pct"][cat] = float(form.get(f"cap.{cat}"))
    lim["quiet_hours"] = {"start": form.get("quiet.start") or lim["quiet_hours"]["start"],
                          "end": form.get("quiet.end") or lim["quiet_hours"]["end"]}
    if form.get("holdout_pct") not in (None, ""):
        data["measurement"]["holdout_pct"] = float(form.get("holdout_pct"))
    if form.get("learning.mode") in ("rules", "bandit"):
        data.setdefault("learning", {})["mode"] = form.get("learning.mode")
    if form.get("fairness.min_ratio") not in (None, ""):
        data.setdefault("fairness", {})["min_ratio"] = float(form.get("fairness.min_ratio")) / 100
    for o in data["offers"]:
        k = o["key"]
        o["enabled"] = form.get(f"offer.{k}.enabled") == "on"
        for field, cast in (("value", float), ("min_order_value", int), ("max_discount_rs", int),
                            ("valid_hours", int), ("priority", int)):
            raw = form.get(f"offer.{k}.{field}")
            if raw not in (None, ""):
                o[field] = cast(float(raw))
        o["segments"] = form.getlist(f"offer.{k}.segments") or o["segments"]
    return data


def friendly_errors(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        out = []
        for e in exc.errors():
            loc = [str(p) for p in e["loc"] if not str(p).isdigit()]
            name = FIELD_NAMES.get(loc[-1], loc[-1].replace("_", " ")) if loc else "value"
            out.append(f"{name.capitalize()}: {e['msg']}.")
        return out
    if isinstance(exc, RulesError):
        return [line[2:] for line in str(exc).splitlines()[1:]]
    if isinstance(exc, ValueError):
        return ["Please enter numbers only."]
    return [str(exc)]
