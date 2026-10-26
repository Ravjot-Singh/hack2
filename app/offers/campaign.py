"""Batch campaign over synthetic customers, with simulated outcomes (no Telegram, never the LLM).

1. Evaluate every customer with the rules engine (using their simulated offer history).
2. The daily budget goes to the most valuable offers first: customers are sorted by the expected profit of their
   best offer; if that no longer fits the remaining budget, their next-best offer that fits is used.
3. The holdout is matched to that same selection: a holdout customer is in today's experiment only if their best
   offer's expected profit is at or above today's funding cutoff (the lowest one that got funded), so
   "sent" and "holdout" are like-for-like. They are recorded with the offer they *would* have got.
4. Sent and holdout decisions get a simulated outcome. Customers the budget didn't reach are not in today's
   experiment and can be picked tomorrow; they are counted but not stored, to keep the database small.

Customers linked to a real Telegram chat are excluded; they get live campaigns via app/triggers.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import fairness, guardrails
from app.db.models import Customer, CustomerFeature, OfferDecision, OfferOutcome, TelegramUser
from app.delivery.templates import offer_message, template_intro
from app.features.customer import _aware
from app.learning import bandit
from app.measurement.holdout import in_holdout
from app.measurement.outcomes import simulate
from app.offers.service import intro_context, new_coupon
from app.rules.engine import Candidate, CustomerContext, SentOffer, evaluate
from app.rules.policy import TenantPolicy


@dataclass
class CampaignResult:
    considered: int = 0
    eligible: int = 0
    sent: int = 0
    holdout: int = 0
    budget_stopped: int = 0
    guard_blocked: int = 0
    liability: int = 0
    by_offer: Counter = field(default_factory=Counter)
    fairness: object = None  # fairness.FairnessReport


def _margin(policy: TenantPolicy, c: Candidate, f: dict) -> float:
    m = policy.catalog.margins
    return m.get(c.category) or m.get(f.get("top_category") or "", policy.catalog.avg_margin)


def _offer_dict(c: Candidate) -> dict:
    return {"key": c.offer_key, "type": c.type, "percent": c.percent, "amount_rs": c.amount_rs, "points": c.points,
            "max_discount_rs": c.max_discount_rs, "min_order_value": c.min_order_value}


def run_simulated_campaign(s: Session, tenant_slug: str, tenant_id: int, policy: TenantPolicy, store: str,
                           coupon_prefix: str, now: datetime, segment: str | None = None,
                           seed: int = 0, daily_budget: int | None = None) -> CampaignResult:
    """daily_budget overrides the rules' budget for this simulation only (e.g. to measure every offer)."""
    rng = random.Random(f"{tenant_slug}:{now.isoformat()}:{seed}")
    lim = policy.rules.limits
    res = CampaignResult()

    linked = select(TelegramUser.customer_id).where(TelegramUser.customer_id.is_not(None))
    q = (select(CustomerFeature, Customer.name, Customer.true_segment, Customer.price_sensitivity,
                Customer.city, Customer.city_tier, Customer.gender, Customer.age)
         .join(Customer, Customer.id == CustomerFeature.customer_id)
         .where(CustomerFeature.tenant_id == tenant_id, CustomerFeature.customer_id.not_in(linked)))
    if segment:
        q = q.where(CustomerFeature.segment == segment)
    rows = s.execute(q).all()

    # Holdout decisions count as "virtual sends" on later days, so holdout and treated customers wait the same
    # gaps/caps and neither group is re-counted more often (all experiment members have an outcome row).
    history: dict[int, list[SentOffer]] = defaultdict(list)
    lookback = now - timedelta(days=max(7, lim.same_offer_cooldown_days))
    for cid, key, shortlist, ts in s.execute(
            select(OfferDecision.customer_id, OfferDecision.offer_key, OfferDecision.shortlist,
                   OfferDecision.created_at)
            .join(OfferOutcome, OfferOutcome.decision_id == OfferDecision.id)
            .where(OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(True),
                   OfferDecision.created_at > lookback, OfferDecision.created_at <= now)):
        history[cid].append(SentOffer(key or (shortlist or ["?"])[0], _aware(ts)))

    cols = [c.name for c in CustomerFeature.__table__.columns]
    treated, held = [], []
    stats = bandit.load_stats(tenant_id, policy, fresh=True) if policy.rules.learning.mode == "bandit" else None
    members: dict[int, fairness.Member] = {}  # everyone outside the control group, for the fairness check
    for feat, name, true_seg, sens, city, city_tier, gender, age in rows:
        f = {c: getattr(feat, c) for c in cols}
        res.considered += 1
        if not in_holdout(tenant_slug, f["customer_id"], policy.rules.measurement.holdout_pct):
            members[f["customer_id"]] = fairness.Member(
                f["customer_id"], fairness.attributes(city, city_tier, gender, age, f.get("tenure_days")))
        # Budget is allocated below, so evaluate as if none were used yet.
        d = evaluate(policy.rules, policy.catalog, CustomerContext(f["customer_id"], f, f["segment"], False,
                                                                   history[f["customer_id"]]),
                     min(0, lim.daily_budget_rs - (daily_budget or lim.daily_budget_rs)), now)
        if not d.eligible:
            continue
        d.eligible = bandit.rank(policy, tenant_id, d.eligible, f, now, stats=stats)  # learned order (bandit mode)
        res.eligible += 1
        item = (f, name, true_seg, sens, d)
        (held if in_holdout(tenant_slug, f["customer_id"], policy.rules.measurement.holdout_pct) else treated).append(item)

    budget_left = lim.daily_budget_rs if daily_budget is None else daily_budget
    records: list[tuple[OfferDecision, dict, str, float, Candidate | None, Candidate]] = []
    # Guardrail harness on simulated history (simulations never touch live money, so only caps/contact/pause).
    biz_state = guardrails.business_state(s, tenant_id, now, simulated=True)
    states = guardrails.customer_states(s, tenant_id, [x[0]["customer_id"] for x in treated], now, simulated=True)
    funded: list[fairness.Member] = []
    unfunded: list[fairness.Member] = []
    for item in sorted(treated, key=lambda x: -x[4].best.expected_net_rs):
        f, d = item[0], item[4]
        m = members[f["customer_id"]]
        m.payload = item
        chosen = next((c for c in d.eligible if c.liability_rs <= budget_left), None)
        if chosen is None:
            m.candidate, m.share = d.best, fairness.discount_share(policy, d.best, f)
            unfunded.append(m)
            continue
        violations = guardrails.check(policy, chosen, "campaign", states[f["customer_id"]], biz_state, now,
                                      pushes_now=False, simulated=True)
        if violations:
            res.guard_blocked += 1
            continue
        guardrails.record_issue(biz_state, states[f["customer_id"]], chosen)
        budget_left -= chosen.liability_rs
        m.candidate, m.share = chosen, fairness.discount_share(policy, chosen, f)
        funded.append(m)

    def accept(m: fairness.Member) -> bool:
        ok = not guardrails.check(policy, m.candidate, "campaign", states[m.key], biz_state, now,
                                  pushes_now=False, simulated=True)
        res.guard_blocked += not ok
        return ok

    funded, res.fairness = fairness.rebalance(
        list(members.values()), funded, unfunded, budget_left, policy.rules.fairness.min_ratio,
        policy.rules.fairness.min_group_size, accept=accept,
        on_add=lambda m: guardrails.record_issue(biz_state, states[m.key], m.candidate))
    res.budget_stopped = len(treated) - len(funded) - res.guard_blocked

    cutoff = float("inf")
    for m in sorted(funded, key=lambda m: -m.payload[4].best.expected_net_rs):
        f, name, true_seg, sens, d = m.payload
        chosen = m.candidate
        cutoff = min(cutoff, d.best.expected_net_rs)
        rec = OfferDecision(tenant_id=tenant_id, customer_id=f["customer_id"], created_at=now, trigger="campaign",
                            simulated=True, segment=f["segment"], shortlist=[c.offer_key for c in d.eligible],
                            outcome="no_offer", liability_rs=0)
        coupon = new_coupon(coupon_prefix)
        intro = template_intro(chosen, intro_context(name, store, chosen, f, policy))
        rec.outcome, rec.offer_key = "sent", chosen.offer_key
        rec.chosen_by = "bandit" if stats is not None and len(d.eligible) > 1 else "rules"
        rec.headline, rec.terms, rec.coupon_code = chosen.headline, chosen.terms, coupon
        rec.category, rec.percent, rec.amount_rs, rec.points = chosen.category, chosen.percent, chosen.amount_rs, chosen.points
        rec.min_order_value, rec.max_discount_rs = chosen.min_order_value, chosen.max_discount_rs
        rec.liability_rs, rec.expected_net_rs, rec.valid_until = chosen.liability_rs, chosen.expected_net_rs, chosen.valid_until
        rec.message = offer_message(intro, chosen, coupon)
        records.append((rec, f, true_seg, sens, chosen, chosen))
        res.sent += 1
        res.liability += chosen.liability_rs
        res.by_offer[chosen.label] += 1

    for f, name, true_seg, sens, d in held:
        c = d.best
        if c.expected_net_rs < cutoff:  # the budget would not have reached them: not in today's experiment
            continue
        rec = OfferDecision(tenant_id=tenant_id, customer_id=f["customer_id"], created_at=now, trigger="campaign",
                            simulated=True, segment=f["segment"], shortlist=[x.offer_key for x in d.eligible],
                            outcome="holdout", offer_key=c.offer_key, headline=c.headline, category=c.category,
                            liability_rs=0, expected_net_rs=c.expected_net_rs, valid_until=c.valid_until,
                            block_reason="Holdout group: no offer, used to measure what offers really add.")
        records.append((rec, f, true_seg, sens, None, c))
        res.holdout += 1

    s.add_all(r[0] for r in records)
    s.flush()
    for rec, f, true_seg, sens, sent, would in records:
        window = max(1.0, (would.valid_until - now).total_seconds() / 3600)
        out = simulate(rng, features=f, true_segment=true_seg, price_sensitivity=sens, window_hours=window,
                       offer=_offer_dict(sent) if sent else None, margin_pct=_margin(policy, would, f),
                       delivery_fee=lim.delivery_fee_rs, point_value=lim.point_value_rs)
        s.add(OfferOutcome(decision_id=rec.id, tenant_id=tenant_id, purchased=out.purchased, redeemed=out.redeemed,
                           order_value=out.order_value, discount_cost=out.discount_cost, margin_rs=out.margin_rs,
                           resolved_at=would.valid_until))
    return res
