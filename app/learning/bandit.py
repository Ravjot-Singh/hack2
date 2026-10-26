"""Learning which offer pays off: Thompson sampling per customer group, inside the rules.

The rules engine still decides which offers are *allowed* (the shortlist). Among those, the bandit picks the one
with the highest sampled profit for this customer:

    score(offer) = engine's purchase estimate for THIS customer  x  learned correction  x  profit if they buy

* The engine's estimate (buy-anyway chance + the offer's expected lift) already knows the customer: someone
  with a cart waiting is likelier to buy whatever we send. Pooling raw purchase rates per offer would confuse
  "this offer works" with "this offer goes to keen customers", so the bandit learns only a *correction* per
  customer group and offer: purchases seen ÷ purchases the engine expected for those same sends. It has a
  Gamma posterior (prior = 1.0, worth PRIOR_WEIGHT expected purchases) and is sampled, not averaged.
  Simulated outcomes (demo data) and real Telegram claims both count.
* profit if they buy = estimated order x category margin - the discount (computed for this customer), so a
  cheaper offer that converts as well wins.

Sampling (instead of always taking the current best) keeps trying the other allowed offers now and then, which is
how the engine finds out it was wrong. Each customer's draw is seeded per day, so the page preview and the send
agree.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.db.models import CustomerFeature, OfferDecision, OfferOutcome
from app.db.session import session_scope
from app.rules.engine import Candidate
from app.rules.policy import TenantPolicy

PRIOR_WEIGHT = 5.0  # the engine's estimate is trusted like this many expected purchases
CACHE_SECONDS = 30


@dataclass
class ArmStats:
    trials: int = 0
    successes: int = 0
    expected: float = 0.0  # purchases the engine's own estimate predicted for these sends

    @property
    def correction(self) -> float:
        """Posterior mean of (actual ÷ expected purchases)."""
        return (PRIOR_WEIGHT + self.successes) / (PRIOR_WEIGHT + self.expected)


Stats = dict[tuple[str, str], ArmStats]  # (segment, offer_key) -> results

_lock = threading.Lock()
_cache: dict[int, tuple[float, Stats]] = {}


def load_stats(tenant_id: int, policy: TenantPolicy, fresh: bool = False) -> Stats:
    """Results of every sent offer so far, per (customer group, offer). Cached for a few seconds."""
    with _lock:
        hit = _cache.get(tenant_id)
        if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    stats: Stats = {}
    offers = {o.key: o for o in policy.rules.offers}
    with session_scope() as s:
        # A purchase (simulated or real outcome) is a success; for real Telegram customers without a recorded
        # order yet, a claim counts once they answer. The engine's expectation uses the customer's profile.
        rows = s.execute(
            select(OfferDecision.segment, OfferDecision.offer_key, OfferOutcome.purchased, OfferDecision.response,
                   CustomerFeature.buy_anyway_prob, CustomerFeature.deal_affinity)
            .outerjoin(OfferOutcome, OfferOutcome.decision_id == OfferDecision.id)
            .join(CustomerFeature, CustomerFeature.customer_id == OfferDecision.customer_id)
            .where(OfferDecision.tenant_id == tenant_id, OfferDecision.outcome == "sent",
                   (OfferOutcome.decision_id.is_not(None)) | (OfferDecision.response.is_not(None))))
        for seg, key, purchased, response, buy_anyway, affinity in rows:
            offer = offers.get(key)
            if offer is None:  # deals and one-off custom offers are not arms
                continue
            arm = stats.setdefault((seg, key), ArmStats())
            arm.trials += 1
            arm.successes += int(bool(purchased) if purchased is not None else response == "claimed")
            arm.expected += _engine_prob(offer.valid_hours, offer.expected_response, buy_anyway, affinity)
    with _lock:
        _cache[tenant_id] = (time.monotonic(), stats)
    return stats


def _engine_prob(valid_hours: int, response: float, buy_anyway: float | None, affinity: float | None) -> float:
    base = 1 - (1 - (buy_anyway or 0)) ** (valid_hours / (14 * 24))
    lift = min(0.6, max(0.01, response * (0.5 + (affinity if affinity is not None else 0.33))))
    return min(0.95, max(0.01, base + lift * (1 - base)))


def clear_cache(tenant_id: int | None = None) -> None:
    with _lock:
        if tenant_id is None:
            _cache.clear()
        else:
            _cache.pop(tenant_id, None)


def prior_buy_prob(policy: TenantPolicy, c: Candidate, f: dict) -> float:
    """The engine's own estimate: chance they'd buy anyway while it's valid + the offer's expected lift."""
    offer = next((o for o in policy.rules.offers if o.key == c.offer_key), None)
    return _engine_prob(offer.valid_hours if offer else 72, offer.expected_response if offer else 0.1,
                        f.get("buy_anyway_prob"), f.get("deal_affinity"))


def profit_if_bought(policy: TenantPolicy, c: Candidate, f: dict) -> float:
    lim = policy.rules.limits
    margins = policy.catalog.margins
    est_order = f.get("last_abandoned_cart_value") if c.offer_key == "cart_recovery" else None
    est_order = max(est_order or f.get("aov") or 600, c.min_order_value or 0)
    margin = margins.get(c.category) or margins.get(f.get("top_category") or "", policy.catalog.avg_margin)
    if c.type == "percent_off":
        discount = min(est_order * (c.percent or 0) / 100, c.max_discount_rs or lim.max_discount_rs_per_offer)
    elif c.type == "flat_off":
        discount = c.amount_rs or 0
    elif c.type == "free_delivery":
        discount = lim.delivery_fee_rs
    else:
        discount = (c.points or 0) * lim.point_value_rs
    return est_order * margin / 100 - discount


@dataclass
class Scored:
    candidate: Candidate
    p_mean: float  # purchase chance with the posterior-mean correction
    p_sample: float  # with a sampled correction (and any preference boost)
    profit: float
    trials: int

    @property
    def score(self) -> float:
        return self.p_sample * self.profit


def score(policy: TenantPolicy, shortlist: list[Candidate], f: dict, stats: Stats, rng: random.Random,
          boosts: dict[str, float] | None = None) -> list[Scored]:
    """Shortlist re-ordered by sampled profit (best first). boosts: offer_key -> multiplier (preferences)."""
    out = []
    segment = f.get("segment") or ""
    for c in shortlist:
        arm = stats.get((segment, c.offer_key), ArmStats())
        p0 = prior_buy_prob(policy, c, f)
        # Correction ~ Gamma(shape = prior + purchases, rate = prior + expected purchases); mean 1 with no data.
        theta = rng.gammavariate(PRIOR_WEIGHT + arm.successes, 1 / (PRIOR_WEIGHT + arm.expected))
        boost = (boosts or {}).get(c.offer_key, 1.0)
        out.append(Scored(c, min(0.95, p0 * arm.correction), min(0.95, p0 * theta) * boost,
                          profit_if_bought(policy, c, f), arm.trials))
    out.sort(key=lambda x: -x.score)
    return out


def customer_rng(tenant_id: int, customer_id: int, now: datetime) -> random.Random:
    return random.Random(f"bandit:{tenant_id}:{customer_id}:{now.date().isoformat()}")


def rank(policy: TenantPolicy, tenant_id: int, shortlist: list[Candidate], f: dict, now: datetime,
         boosts: dict[str, float] | None = None, stats: Stats | None = None) -> list[Candidate]:
    """The shortlist in the order to offer it: the learned order in bandit mode, else the rules' own (where a
    customer preference counts as +1 priority)."""
    if policy.rules.learning.mode != "bandit" or len(shortlist) < 2:
        if not boosts:
            return list(shortlist)
        return sorted(shortlist, key=lambda c: (-(c.priority + (c.offer_key in boosts)), -c.expected_net_rs))
    stats = stats if stats is not None else load_stats(tenant_id, policy)
    return [x.candidate for x in score(policy, shortlist, f, stats, customer_rng(tenant_id, f["customer_id"], now),
                                       boosts)]


def summary(policy: TenantPolicy, tenant_id: int) -> list[dict]:
    """For the Results page: per group and offer, purchases vs what the engine expected, and the data behind it."""
    stats = load_stats(tenant_id, policy)
    labels = {o.key: o.label for o in policy.rules.offers}
    groups = {s.key: s.label for s in policy.segments.segments}
    rows = []
    for (seg, key), arm in stats.items():
        if key not in labels or arm.trials == 0:
            continue
        rows.append({"segment": seg, "segment_label": groups.get(seg, seg), "offer_key": key,
                     "offer_label": labels[key], "trials": arm.trials, "rate": arm.successes / arm.trials,
                     "correction": arm.correction})
    rows.sort(key=lambda r: (r["segment_label"], -r["correction"]))
    return rows
