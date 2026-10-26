"""Every limit in rules.yaml, checked one at a time, plus a randomized 'nothing ever breaks a limit' sweep."""

import random
from datetime import datetime, timedelta

import pytest
import yaml

from app.config import TENANTS_DIR
from app.data.generator import IST
from app.rules.engine import CustomerContext, SentOffer, evaluate
from app.rules.policy import load_policy
from app.rules.schema import RulesConfig, RulesError, check_rules, load_rules

SHOPKART = TENANTS_DIR / "shopkart"
POLICY = load_policy(SHOPKART)
NOON = datetime(2026, 10, 22, 12, 0, tzinfo=IST)


def feats(**kw):
    f = dict(tenure_days=400, recency_days=20, orders_30=1, orders_90=2, orders_365=6, monetary_365=9000,
             aov=1500, discount_share=0.3, return_rate=0.05, sessions_30=4, sessions_90=12,
             abandoned_carts_7d=0, abandoned_carts_30d=0, last_abandoned_cart_value=None,
             last_abandoned_cart_category=None, top_category="fashion", points_balance=0, points_expiring_30d=0,
             days_to_birthday=200, churn_risk=0.35, buy_anyway_prob=0.2, deal_affinity=0.35, expected_value_90d=2000)
    # churn_risk 0.35: a regular starting to slow down, so the loyalty bonus applies by default
    f.update(kw)
    return f


def run(segment="regular", recent=(), opted_out=False, budget_used=0, now=NOON, rules=None, **kw):
    ctx = CustomerContext(1, feats(**kw), segment, opted_out, list(recent))
    return evaluate(rules or POLICY.rules, POLICY.catalog, ctx, budget_used, now)


def keys(d):
    return [c.offer_key for c in d.eligible]


def reason(d, key):
    return next(b.reason for b in d.blocked if b.offer_key == key)


def with_limits(**changes):
    data = POLICY.rules.model_dump()
    data["limits"].update(changes)
    return RulesConfig(**data)


def test_shopkart_rules_load():
    assert {o.key for o in POLICY.rules.offers} >= {"cart_recovery", "win_back", "loyalty_bonus"}


# ---- customer-level limits ----

def test_opted_out_gets_nothing():
    d = run(opted_out=True)
    assert d.eligible == [] and "opted out" in d.customer_block


def test_weekly_cap():
    sent = [SentOffer("birthday", NOON - timedelta(days=5)), SentOffer("free_delivery", NOON - timedelta(days=3))]
    d = run(recent=sent)
    assert d.eligible == [] and "this week" in d.customer_block
    assert run(recent=[SentOffer("birthday", NOON - timedelta(days=9))] * 2).eligible  # older than a week


def test_min_gap_between_offers():
    d = run(recent=[SentOffer("birthday", NOON - timedelta(hours=10))])
    assert d.eligible == [] and "48h" in d.customer_block


def test_operator_sends_relax_frequency_but_not_price_rules():
    sent = [SentOffer("loyalty_bonus", NOON - timedelta(hours=2)), SentOffer("birthday", NOON - timedelta(days=1))]

    def relaxed(**kw):
        ctx = CustomerContext(1, feats(**kw), kw.pop("segment", "regular"), kw.pop("opted_out", False), sent)
        return evaluate(POLICY.rules, POLICY.catalog, ctx, kw.pop("budget_used", 0), NOON, relax_frequency=True)

    assert run(recent=sent).customer_block  # normally: weekly cap / 48h gap
    d = relaxed(days_to_birthday=3)
    assert d.customer_block is None and {"loyalty_bonus", "birthday"} <= set(keys(d))  # gap + cooldown skipped
    assert all(c.percent is None or c.percent <= POLICY.rules.cap_pct(c.category) for c in d.eligible)
    ctx = CustomerContext(1, feats(), "regular", True, sent)
    assert evaluate(POLICY.rules, POLICY.catalog, ctx, 0, NOON, relax_frequency=True).customer_block  # opt-out
    ctx = CustomerContext(1, feats(buy_anyway_prob=0.9, days_to_birthday=3), "regular", False, sent)
    assert "birthday" not in keys(evaluate(POLICY.rules, POLICY.catalog, ctx, 0, NOON, relax_frequency=True))
    ctx = CustomerContext(1, feats(days_to_birthday=3), "regular", False, sent)
    full = evaluate(POLICY.rules, POLICY.catalog, ctx, POLICY.rules.limits.daily_budget_rs - 10, NOON,
                    relax_frequency=True)
    assert not full.eligible  # daily budget still applies


def test_same_offer_cooldown_blocks_only_that_offer():
    d = run(recent=[SentOffer("loyalty_bonus", NOON - timedelta(days=5))])
    assert "loyalty_bonus" not in keys(d) and "14 days" in reason(d, "loyalty_bonus")


# ---- per-offer rules ----

def test_segment_and_audience_rules():
    assert "win_back" not in keys(run(segment="regular"))
    assert "win_back" in keys(run(segment="at_risk", churn_risk=0.7, buy_anyway_prob=0.02))
    assert "cart_recovery" not in keys(run())
    d = run(abandoned_carts_7d=1, last_abandoned_cart_category="beauty", last_abandoned_cart_value=900)
    cart = next(c for c in d.eligible if c.offer_key == "cart_recovery")
    assert cart.category == "beauty" and cart.percent == 10


def test_buy_anyway_guard_blocks_money_offers_but_not_points():
    d = run(buy_anyway_prob=0.8, abandoned_carts_7d=1, last_abandoned_cart_category="fashion",
            last_abandoned_cart_value=1200)
    assert "cart_recovery" not in keys(d) and "buy anyway" in reason(d, "cart_recovery")
    # Points aren't stopped by the guard, but the value estimate still skips them for near-certain buyers:
    # the points would mostly reward a purchase that was happening anyway.
    assert "Not worth it" in reason(d, "loyalty_bonus")
    assert "loyalty_bonus" in keys(run(buy_anyway_prob=0.3))


def test_serial_returners_get_no_discounts():
    d = run(segment="occasional", return_rate=0.6)
    assert "free_delivery" not in keys(d) and "Returns" in reason(d, "free_delivery")


def test_category_cap_clamps_percent():
    d = run(segment="at_risk", top_category="electronics", churn_risk=0.7, buy_anyway_prob=0.02, aov=6000,
            rules=with_limits(min_expected_net_rs=-500))
    wb = next(c for c in d.eligible if c.offer_key == "win_back")
    assert wb.percent == 5 and any("capped from 15% to 5%" in r for r in wb.reasons)


def test_zero_cap_blocks_category():
    rules = with_limits(max_discount_pct={**POLICY.rules.limits.max_discount_pct, "books": 0})
    d = run(segment="at_risk", top_category="books", churn_risk=0.7, rules=rules)
    assert "not allowed" in reason(d, "win_back")


def test_store_wide_offer_excludes_low_cap_categories():
    d = run(days_to_birthday=3)
    bday = next(c for c in d.eligible if c.offer_key == "birthday")
    assert set(bday.excluded_categories) == {"electronics", "grocery"}
    assert "not valid on Electronics, Grocery" in bday.terms


def test_daily_budget():
    d = run(days_to_birthday=3, budget_used=POLICY.rules.limits.daily_budget_rs - 200)
    assert "birthday" not in keys(d) and "budget" in reason(d, "birthday")  # up to Rs 250 doesn't fit
    assert "loyalty_bonus" in keys(d)  # 150 points = Rs 150 still fits


def test_loyalty_bonus_only_for_customers_slowing_down():
    assert "loyalty_bonus" not in keys(run(churn_risk=0.1))  # buying as usual: they'd buy anyway
    assert "loyalty_bonus" in keys(run(churn_risk=0.4))


def test_vip_win_back_for_former_big_spenders():
    drifting = dict(segment="at_risk", churn_risk=0.7, buy_anyway_prob=0.02, top_category="fashion")
    assert "win_back_vip" in keys(run(**drifting, monetary_365=30000, aov=3000))
    assert "win_back_vip" not in keys(run(**drifting, monetary_365=4000))
    vip = next(c for c in run(**drifting, monetary_365=30000, aov=3000).eligible if c.offer_key == "win_back_vip")
    assert vip.percent == 20 and vip.max_discount_rs == 500


def test_expected_value_floor():
    at_risk = dict(segment="at_risk", top_category="electronics", churn_risk=0.7, buy_anyway_prob=0.02, aov=6000)
    assert "win_back" in keys(run(**at_risk))  # 5% off an 8%-margin category still pays
    d = run(**at_risk, rules=with_limits(min_expected_net_rs=50))
    assert "win_back" not in keys(d) and "Not worth it" in reason(d, "win_back")


def test_quiet_hours_delay_sending_not_block():
    late = NOON.replace(hour=22, minute=30)
    d = run(days_to_birthday=3, now=late)
    assert d.send_after == (late + timedelta(days=1)).replace(hour=9, minute=0)
    assert d.eligible[0].valid_until > d.send_after
    early = run(now=NOON.replace(hour=7))
    assert early.send_after == NOON.replace(hour=9)


def test_fallback_category():
    d = run(segment="dormant", top_category=None, churn_risk=0.9, buy_anyway_prob=0.0, orders_365=0)
    wb = next(c for c in d.eligible if c.offer_key == "win_back")
    assert wb.category == "any" and wb.percent == 10 and any("store-wide" in r for r in wb.reasons)


def test_offers_ranked_by_priority():
    d = run(days_to_birthday=3, abandoned_carts_7d=1, last_abandoned_cart_category="fashion",
            last_abandoned_cart_value=1500)
    assert keys(d)[0] == "cart_recovery"  # priority 9 beats birthday 8


# ---- load-time validation ----

def _raw():
    return yaml.safe_load((SHOPKART / "rules.yaml").read_text(encoding="utf-8"))


def _problems(raw):
    return check_rules(RulesConfig(**raw), POLICY.catalog.margins, {s.key for s in POLICY.segments.segments})


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r["limits"]["max_discount_pct"].update(electronics=12), "above its margin"),
    (lambda r: r["limits"]["max_discount_pct"].update(toys=5), "unknown category 'toys'"),
    (lambda r: r["offers"][0].update(category="toys"), "unknown category 'toys'"),
    (lambda r: r["offers"][0].update(segments=["vip"]), "unknown segment 'vip'"),
    (lambda r: r["offers"][0].update(max_discount_rs=900), "store-wide"),
    (lambda r: r["offers"][2].update(category="fashion", value=40), "cap for fashion"),
    (lambda r: r["offers"][3].update(type="flat_off", category="any", fallback_category=None, max_discount_rs=None,
                                     value=200), "above the 10% cap"),
    (lambda r: r["offers"][3].update(value=120), "can't be more than 90%"),
    (lambda r: r["offers"][6].update(value=800), "worth more than"),
    (lambda r: r["offers"].append(dict(r["offers"][0])), "more than once"),
])
def test_rule_file_mistakes_are_caught(mutate, expected):
    raw = _raw()
    mutate(raw)
    assert any(expected in p for p in _problems(raw)), _problems(raw)


def test_load_rules_raises_readable_error(tmp_path):
    raw = _raw()
    raw["limits"]["max_discount_pct"]["electronics"] = 12
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RulesError, match="Discount cap for electronics"):
        load_rules(path, POLICY.catalog.margins, {s.key for s in POLICY.segments.segments})


# ---- the core promise ----

def test_no_candidate_ever_breaks_a_limit():
    rng = random.Random(0)
    lim = POLICY.rules.limits
    cats = list(POLICY.catalog.margins) + [None]
    segs = [s.key for s in POLICY.segments.segments]
    for _ in range(3000):
        budget_used = rng.uniform(0, lim.daily_budget_rs)
        f = dict(
            segment=rng.choice(segs), budget_used=budget_used, now=NOON.replace(hour=rng.randrange(24)),
            buy_anyway_prob=rng.random(), return_rate=rng.random() * 0.6, deal_affinity=rng.random(),
            churn_risk=rng.random(), top_category=rng.choice(cats), aov=rng.randint(100, 20000),
            abandoned_carts_7d=rng.randint(0, 2), last_abandoned_cart_category=rng.choice(cats),
            last_abandoned_cart_value=rng.randint(100, 30000), days_to_birthday=rng.randint(0, 30),
            points_expiring_30d=rng.randint(0, 200), orders_365=rng.randint(0, 20),
        )
        d = run(**f)
        for c in d.eligible:
            o = POLICY.rules.offer(c.offer_key)
            assert c.liability_rs <= lim.max_discount_rs_per_offer
            assert c.liability_rs <= lim.daily_budget_rs - budget_used
            assert c.expected_net_rs >= lim.min_expected_net_rs
            if c.percent is not None:
                assert c.percent <= POLICY.rules.cap_pct(c.category)
                assert all(c.percent > POLICY.rules.cap_pct(x) for x in c.excluded_categories)
            if o.type in ("percent_off", "flat_off", "free_delivery"):
                assert f["buy_anyway_prob"] <= lim.max_buy_anyway_prob_for_discounts
                assert f["return_rate"] <= lim.max_return_rate_for_discounts
            assert "all" in o.segments or f["segment"] in o.segments
