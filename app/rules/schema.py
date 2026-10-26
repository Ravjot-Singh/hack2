"""rules.yaml: the offer catalogue and the hard limits every offer (and every AI choice) must respect.

Loading validates the file against the catalog and segments, so a rule that breaks a limit is rejected
with a plain-language error before it can ever reach a customer.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from app.features.conditions import validate_conditions

OfferType = Literal["percent_off", "flat_off", "free_delivery", "bonus_points"]
MONETARY_TYPES = {"percent_off", "flat_off", "free_delivery"}  # cost us money on the order
DYNAMIC_CATEGORIES = {"any", "from_cart", "top_category"}


class Offer(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str
    description: str
    enabled: bool = True
    type: OfferType
    value: float = Field(ge=0)  # % for percent_off, Rs for flat_off, points for bonus_points
    category: str = "any"
    fallback_category: str | None = None  # used when from_cart/top_category can't be resolved
    min_order_value: int = Field(default=0, ge=0)
    max_discount_rs: int | None = Field(default=None, gt=0)
    valid_hours: int = Field(default=72, gt=0, le=24 * 30)
    segments: list[str] = ["all"]
    audience: dict[str, float] = {}
    priority: int = Field(default=5, ge=1, le=10)
    expected_response: float = Field(default=0.15, gt=0, le=1)

    _check_audience = field_validator("audience")(validate_conditions)


class QuietHours(BaseModel):
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # window crosses midnight


class Limits(BaseModel):
    max_discount_pct: dict[str, float]
    max_discount_rs_per_offer: int = Field(gt=0)
    max_offers_per_customer_per_week: int = Field(ge=0)
    min_hours_between_offers: int = Field(ge=0)
    same_offer_cooldown_days: int = Field(ge=0)
    daily_budget_rs: int = Field(ge=0)
    max_buy_anyway_prob_for_discounts: float = Field(ge=0, le=1)
    max_return_rate_for_discounts: float = Field(ge=0, le=1)
    min_expected_net_rs: int
    quiet_hours: QuietHours
    delivery_fee_rs: int = Field(default=40, ge=0)
    point_value_rs: float = Field(default=1.0, gt=0)
    # Hard ceilings the guardrail harness enforces on every send, including hand-sent offers and deals.
    max_offers_per_customer_per_day: int = Field(default=2, ge=1, le=20)
    max_total_spend_per_day_rs: int | None = Field(default=None, ge=0)  # None = 2x the daily budget

    @property
    def total_spend_ceiling_rs(self) -> int:
        return self.max_total_spend_per_day_rs if self.max_total_spend_per_day_rs is not None \
            else 2 * self.daily_budget_rs


class Measurement(BaseModel):
    holdout_pct: float = Field(default=10, ge=0, le=50)


class Learning(BaseModel):
    """bandit: the engine learns per customer group which allowed offer makes the most profit and picks it;
    rules: always the rules' own ranking (priority, then expected value)."""
    mode: Literal["rules", "bandit"] = "rules"


class Fairness(BaseModel):
    """Deals and campaigns rebalance their budget so no customer group (city tier, gender, age band, new vs
    long-time) gets offers at less than min_ratio of the overall rate, as far as the rules allow."""
    min_ratio: float = Field(default=0.8, ge=0, le=1)
    min_group_size: int = Field(default=20, ge=1)


class RulesConfig(BaseModel):
    limits: Limits
    offers: list[Offer]
    measurement: Measurement = Measurement()
    learning: Learning = Learning()
    fairness: Fairness = Fairness()

    def offer(self, key: str) -> Offer:
        return next(o for o in self.offers if o.key == key)

    def cap_pct(self, category: str) -> float:
        return self.limits.max_discount_pct.get(category, self.limits.max_discount_pct["any"])


def check_rules(rules: RulesConfig, margins: dict[str, float], segment_keys: set[str]) -> list[str]:
    """Business-level checks that need the catalog and segments. Returns plain-language problems."""
    problems = []
    lim = rules.limits
    caps = lim.max_discount_pct
    if "any" not in caps:
        problems.append("limits.max_discount_pct needs an 'any' entry (used for offers on the whole store).")
    for cat, cap in caps.items():
        if cat != "any" and cat not in margins:
            problems.append(f"limits.max_discount_pct has unknown category '{cat}'. Known: {', '.join(margins)}.")
        elif cat != "any" and cap > margins[cat]:
            problems.append(f"Discount cap for {cat} ({cap:g}%) is above its margin ({margins[cat]:g}%): "
                            "every sale would lose money.")
    if caps.get("any", 0) > max(margins.values(), default=0):
        problems.append("The 'any' discount cap is above every category's margin.")

    keys = [o.key for o in rules.offers]
    for dup in {k for k in keys if keys.count(k) > 1}:
        problems.append(f"Offer key '{dup}' is used more than once.")
    for o in rules.offers:
        name = f"Offer '{o.label}' ({o.key})"
        if o.category not in DYNAMIC_CATEGORIES and o.category not in margins:
            problems.append(f"{name}: unknown category '{o.category}'. Use any, from_cart, top_category "
                            f"or one of: {', '.join(margins)}.")
            continue
        if o.fallback_category is not None and o.fallback_category != "any" and o.fallback_category not in margins:
            problems.append(f"{name}: fallback_category must be any or one of: {', '.join(margins)}.")
        for seg in o.segments:
            if seg != "all" and seg not in segment_keys:
                problems.append(f"{name}: unknown segment '{seg}'. Known: all, {', '.join(sorted(segment_keys))}.")
        if o.type == "percent_off":
            if o.value > 90:
                problems.append(f"{name} gives {o.value:g}%: a percentage discount can't be more than 90%.")
            fixed_cat = o.category not in ("from_cart", "top_category")
            if fixed_cat and caps.get("any") is not None and o.value > rules.cap_pct(o.category):
                problems.append(f"{name} gives {o.value:g}% but the cap for {o.category} is "
                                f"{rules.cap_pct(o.category):g}%.")
            if (o.max_discount_rs or 0) > lim.max_discount_rs_per_offer:
                problems.append(f"{name}: max_discount_rs {o.max_discount_rs} is above the store-wide "
                                f"limit of Rs {lim.max_discount_rs_per_offer}.")
        if o.type == "flat_off":
            if o.value > lim.max_discount_rs_per_offer:
                problems.append(f"{name} gives Rs {o.value:g} off, above the Rs {lim.max_discount_rs_per_offer} limit.")
            if o.min_order_value <= 0:
                problems.append(f"{name}: a flat discount needs a min_order_value.")
            elif o.category not in ("from_cart", "top_category") and caps.get("any") is not None:
                pct = 100 * o.value / o.min_order_value
                if pct > rules.cap_pct(o.category):
                    problems.append(f"{name}: Rs {o.value:g} off a Rs {o.min_order_value} order is {pct:.0f}%, "
                                    f"above the {rules.cap_pct(o.category):g}% cap for {o.category}.")
        if o.type == "bonus_points" and o.value * lim.point_value_rs > lim.max_discount_rs_per_offer:
            problems.append(f"{name}: {o.value:g} points are worth more than the Rs "
                            f"{lim.max_discount_rs_per_offer} per-offer limit.")
    return problems


class RulesError(ValueError):
    pass


def load_rules(path: Path, margins: dict[str, float], segment_keys: set[str]) -> RulesConfig:
    return parse_rules(yaml.safe_load(path.read_text(encoding="utf-8")), margins, segment_keys)


def parse_rules(data: dict, margins: dict[str, float], segment_keys: set[str]) -> RulesConfig:
    rules = RulesConfig(**data)
    problems = check_rules(rules, margins, segment_keys)
    if problems:
        raise RulesError("rules.yaml has problems:\n- " + "\n- ".join(problems))
    return rules
