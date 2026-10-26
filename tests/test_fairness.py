"""Fairness across customer groups: measured on every deal/campaign, budget rebalanced toward groups left behind."""

from dataclasses import replace

from app import fairness
from app.config import TENANTS_DIR
from app.data.city_tiers import tier_for
from app.data.generator import IST  # noqa: F401
from app.fairness import Member
from app.rules.engine import CustomerContext, evaluate
from app.rules.policy import load_policy
from tests.test_agent import FEATS
from tests.test_bandit import NOON
from tests.test_hardening import DEAL_FORM, SAME, biz_of, c, seeded  # noqa: F401

POLICY = load_policy(TENANTS_DIR / "shopkart")
CAND = evaluate(POLICY.rules, POLICY.catalog, CustomerContext(1, FEATS, "regular"), 0, NOON).eligible[0]


def test_attributes_and_city_tiers():
    assert tier_for("Ludhiana") == 2 and tier_for(" delhi ") == 1 and tier_for("Moga") == 3
    assert tier_for("Atlantis") is None
    a = fairness.attributes("Moga", None, "f", 22, 30)
    assert a == {"city_tier": "Smaller town", "gender": "Women", "age_band": "Under 25",
                 "tenure": "New (under 3 months)"}
    assert fairness.attributes(None, None, None, None, None) == dict.fromkeys(a)  # unknown: never guessed


def _people(n_metro=60, n_town=60, metro_funded=40, town_funded=4, town_pool=30, cost=100, net=lambda i: 50 - i):
    members, funded, unfunded = [], [], []
    for i in range(n_metro):
        m = Member(i, {"city_tier": "Metro"}, replace(CAND, liability_rs=cost, expected_net_rs=net(i)))
        members.append(m)
        (funded if i < metro_funded else unfunded).append(m)
    for j in range(n_town):
        k = 1000 + j
        cand = replace(CAND, liability_rs=cost, expected_net_rs=10 - j * 0.1) if j < town_funded + town_pool else None
        m = Member(k, {"city_tier": "Smaller town"}, cand)
        members.append(m)
        if j < town_funded:
            funded.append(m)
        elif cand is not None:
            unfunded.append(m)
    return members, funded, unfunded


def test_unfair_plan_is_rebalanced_within_the_same_budget():
    members, funded, unfunded = _people()
    new, rep = fairness.rebalance(members, funded, unfunded, budget_left=0, min_ratio=0.8, min_group_size=20)
    town = next(r for r in rep.rows if r.group == "Smaller town")
    assert town.before_ratio < 0.8 <= town.rate_ratio and town.status == "rebalanced"
    assert len(new) == len(funded)  # same budget: swaps, not extra spend
    assert sum(m.candidate.liability_rs for m in new) <= sum(m.candidate.liability_rs for m in funded)
    assert rep.swaps == rep.added > 0
    metro = next(r for r in rep.rows if r.group == "Metro")
    assert metro.rate_ratio >= 0.8  # the donors' group stays fair too


def test_shortfall_is_reported_as_limited_by_rules():
    members, funded, unfunded = _people(town_pool=0)  # rules allow no more town customers
    _, rep = fairness.rebalance(members, funded, unfunded, budget_left=0, min_ratio=0.8, min_group_size=20)
    town = next(r for r in rep.rows if r.group == "Smaller town")
    assert town.status == "limited" and rep.gaps == [town] and rep.added == 0


def test_small_groups_are_not_judged():
    members, funded, unfunded = _people(n_town=10, town_funded=0, town_pool=10)
    _, rep = fairness.rebalance(members, funded, unfunded, budget_left=0, min_ratio=0.8, min_group_size=20)
    assert next(r for r in rep.rows if r.group == "Smaller town").status == "too small" and rep.added == 0


def test_leftover_budget_is_used_before_swapping():
    members, funded, unfunded = _people()
    _, rep = fairness.rebalance(members, funded, unfunded, budget_left=10**6, min_ratio=0.8, min_group_size=20)
    assert rep.added > 0 and rep.swaps == 0


def test_guardrail_can_refuse_a_newcomer():
    members, funded, unfunded = _people()
    _, rep = fairness.rebalance(members, funded, unfunded, budget_left=10**6, min_ratio=0.8, min_group_size=20,
                                accept=lambda m: False)
    assert rep.added == 0 and next(r for r in rep.rows if r.group == "Smaller town").status == "limited"


def test_deal_preview_shows_fairness(seeded):
    c, tid = seeded
    c.post("/app/deals", headers=SAME, data=DEAL_FORM)
    from sqlalchemy import select
    from app.db.models import Deal
    from app.db.session import session_scope
    with session_scope() as s:
        deal_id = s.scalar(select(Deal.id))
    page = c.get(f"/app/deals/{deal_id}").text
    assert "Fairness across customer groups" in page and ("Metro" in page or "Large city" in page)
    assert "Fairness of recent deals" in c.get("/app/guardrails").text


def test_campaign_reports_fairness(seeded):
    from app.db.session import session_scope
    from app.offers.campaign import run_simulated_campaign
    from app.rules.policy import current_policy
    from tests.test_hardening import NOW
    _, tid = seeded
    with session_scope() as s:
        res = run_simulated_campaign(s, "kesar-fashions", tid, current_policy("kesar-fashions"), "K", "KF", NOW)
    assert res.fairness.rows and all(r.status in ("ok", "rebalanced", "limited", "too small") for r in res.fairness.rows)
    assert biz_of("kesar-fashions")  # fixture sanity
