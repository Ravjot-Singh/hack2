"""Learning engine (Thompson sampling inside the rules' shortlist)."""

import random
from datetime import datetime

from app.config import TENANTS_DIR
from app.data.generator import IST
from app.db.session import session_scope
from app.learning import bandit
from app.learning.bandit import ArmStats
from app.measurement.outcomes import expected_gain
from app.offers.campaign import run_simulated_campaign
from app.rules.engine import CustomerContext, evaluate
from app.rules.policy import current_policy, load_policy
from tests.test_agent import FEATS
from tests.test_hardening import c, seeded  # noqa: F401

POLICY = load_policy(TENANTS_DIR / "shopkart")
NOON = datetime(2026, 10, 22, 12, tzinfo=IST)
F = {**FEATS, "customer_id": 1}
SHORTLIST = evaluate(POLICY.rules, POLICY.catalog, CustomerContext(1, F, "regular"), 0, NOON).eligible
KEYS = [x.offer_key for x in SHORTLIST]


def _with_mode(mode):
    return type(POLICY)(POLICY.rules.model_copy(update={"learning": POLICY.rules.learning.model_copy(
        update={"mode": mode})}), POLICY.catalog, POLICY.segments)


def test_shopkart_learns_by_default_and_rules_mode_keeps_the_rules_order():
    assert POLICY.rules.learning.mode == "bandit"
    assert [x.offer_key for x in bandit.rank(_with_mode("rules"), 1, SHORTLIST, F, NOON, stats={})] == KEYS


def test_ranking_only_reorders_the_shortlist_and_is_stable_per_day():
    ranked = bandit.rank(POLICY, 1, SHORTLIST, F, NOON, stats={})
    assert sorted(x.offer_key for x in ranked) == sorted(KEYS)
    assert ranked == bandit.rank(POLICY, 1, SHORTLIST, F, NOON, stats={})  # preview == send


def test_no_data_means_the_engines_own_estimate():
    assert ArmStats().correction == 1.0
    scored = bandit.score(POLICY, SHORTLIST, F, {}, random.Random(1))
    for x in scored:
        assert abs(x.p_mean - bandit.prior_buy_prob(POLICY, x.candidate, F)) < 1e-9


def test_results_move_the_choice():
    """An offer that keeps beating the engine's estimate gets picked; one that keeps missing it gets dropped."""
    winner, loser = KEYS[-1], KEYS[0]
    stats = {("regular", winner): ArmStats(400, 120, 40.0), ("regular", loser): ArmStats(400, 4, 40.0)}
    picks = [bandit.score(POLICY, SHORTLIST, F, stats, random.Random(i))[0].candidate.offer_key for i in range(200)]
    assert picks.count(winner) > 150 and picks.count(loser) == 0


def test_preferences_boost_is_a_multiplier_on_the_sampled_chance():
    base = bandit.score(POLICY, SHORTLIST, F, {}, random.Random(3))
    boosted = bandit.score(POLICY, SHORTLIST, F, {}, random.Random(3), boosts={KEYS[-1]: 100.0})
    assert boosted[0].candidate.offer_key == KEYS[-1] or base[0].candidate.offer_key == KEYS[-1]


def test_stats_come_from_simulated_outcomes(seeded):
    from tests.test_hardening import NOW
    _, tid = seeded
    pol = current_policy("kesar-fashions")
    with session_scope() as s:
        run_simulated_campaign(s, "kesar-fashions", tid, pol, "Kesar", "KF", NOW)
    stats = bandit.load_stats(tid, pol, fresh=True)
    assert stats and all(a.trials >= a.successes and a.expected > 0 for a in stats.values())
    assert sum(a.trials for a in stats.values()) > 0
    rows = bandit.summary(pol, tid)
    assert rows and {"segment_label", "offer_label", "correction"} <= rows[0].keys()


def test_true_expected_gain_rewards_relevant_offers():
    common = dict(features={**F, "buy_anyway_prob": 0.1, "aov": 1500}, true_segment="bargain_hunter",
                  price_sensitivity=0.9, window_hours=72, margin_pct=40, delivery_fee=40, point_value=1)
    strong = expected_gain(offer={"key": "x", "type": "percent_off", "percent": 15, "max_discount_rs": 300,
                                  "min_order_value": 0}, **common)
    useless = expected_gain(offer={"key": "x", "type": "percent_off", "percent": 1, "max_discount_rs": 300,
                                   "min_order_value": 0}, **common)
    assert strong > useless


def test_results_page_shows_what_was_learned(seeded):
    from tests.test_hardening import NOW
    c, tid = seeded
    pol = current_policy("kesar-fashions")
    with session_scope() as s:
        run_simulated_campaign(s, "kesar-fashions", tid, pol, "Kesar", "KF", NOW, daily_budget=10**7)
    bandit.clear_cache()
    assert "What the engine has learned" in c.get("/app/results").text
