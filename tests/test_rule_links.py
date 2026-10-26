"""Rupee limits follow the discount %: the same relations the rules editor shows live are enforced on save."""

import pytest

from app.config import TENANTS_DIR
from app.rules.policy import load_policy
from app.rules.schema import RulesConfig, check_rules

POLICY = load_policy(TENANTS_DIR / "shopkart")
SEGMENTS = {s.key for s in POLICY.segments.segments}


def problems(change) -> list[str]:
    data = POLICY.rules.model_dump(mode="json")
    change(data)
    return check_rules(RulesConfig(**data), POLICY.catalog.margins, SEGMENTS)


def offer(data, key):
    return next(o for o in data["offers"] if o["key"] == key)


def test_shopkart_rules_satisfy_every_link():
    assert problems(lambda d: None) == []


@pytest.mark.parametrize("change, expected", [
    # 20% of a Rs 999 minimum order is Rs 200: a Rs 150 cap means the % never applies in full.
    (lambda d: offer(d, "win_back_vip").update(max_discount_rs=150), "more than its maximum discount of Rs 150"),
    # Raising the % has the same effect against the existing cap.
    (lambda d: offer(d, "second_order").update(value=30, min_order_value=1999), "more than its maximum discount"),
    # A favourite-category % above every category's cap.
    (lambda d: offer(d, "win_back").update(value=40), "no category allows more than 30%"),
    # The daily budget must fit the most expensive enabled offer.
    (lambda d: d["limits"].update(daily_budget_rs=100), "could never be sent"),
    # The total daily ceiling can't sit below the daily budget.
    (lambda d: d["limits"].update(max_total_spend_per_day_rs=1000), "below the daily budget"),
])
def test_broken_links_are_explained(change, expected):
    found = problems(change)
    assert any(expected in p for p in found), found


def test_disabled_offers_dont_constrain_the_budget():
    def change(d):
        d["limits"]["daily_budget_rs"] = 100
        for o in d["offers"]:
            o["enabled"] = o["type"] == "free_delivery"
    assert problems(change) == []
