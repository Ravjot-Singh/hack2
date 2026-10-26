"""Rules engine: which offers may this customer get right now, and why (or why not)?

Pure function of (rules, customer features, offer history, budget used today, now). The AI later picks only
from the shortlist this returns, and the validator re-checks its pick against the same Candidate data.

Order of checks
  customer level: opted out -> weekly cap -> gap since last offer      (fail = no offer at all)
  per offer:      enabled -> segment -> audience -> same-offer cooldown -> category resolvable
  (operator sends relax only the weekly cap, the gap and the same-offer cooldown)
                  -> (money offers) buy-anyway guard, return-rate guard -> category discount cap
                  -> daily budget -> expected value
  quiet hours never block; they set send_after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.features.conditions import check_conditions
from app.rules.schema import MONETARY_TYPES, Offer, RulesConfig


REDEEM_SHARE = 0.85  # share of buyers holding an offer who actually use it


@dataclass(frozen=True)
class SentOffer:
    offer_key: str
    sent_at: datetime


@dataclass
class CustomerContext:
    customer_id: int
    features: dict
    segment: str
    opted_out: bool = False
    recent_offers: list[SentOffer] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogInfo:
    margins: dict[str, float]  # category -> margin %
    labels: dict[str, str]  # category -> display name

    @property
    def avg_margin(self) -> float:
        return sum(self.margins.values()) / len(self.margins)


@dataclass
class Candidate:
    offer_key: str
    label: str
    type: str
    category: str  # resolved: a catalog category or "any"
    percent: float | None  # effective % for percent_off (after caps)
    amount_rs: int | None  # Rs off for flat_off
    points: int | None
    min_order_value: int
    max_discount_rs: int | None
    excluded_categories: list[str]
    liability_rs: int  # most this offer can ever cost us; counted against the daily budget
    expected_cost_rs: float
    expected_net_rs: float
    priority: int
    valid_until: datetime
    headline: str
    terms: str
    reasons: list[str]


@dataclass
class Blocked:
    offer_key: str
    label: str
    reason: str


@dataclass
class Decision:
    customer_id: int
    segment: str
    eligible: list[Candidate]
    blocked: list[Blocked]
    customer_block: str | None = None
    send_after: datetime | None = None

    @property
    def best(self) -> Candidate | None:
        return self.eligible[0] if self.eligible else None


def _rs(v: float) -> str:
    return f"₹{v:,.0f}"


def _describe(o: Offer, cat: str, cat_label: str, pct, amount, points, max_rs, excluded, valid_until, catalog,
              delivery_fee) -> tuple[str, str]:
    where = "everything" if cat == "any" else cat_label
    if o.type == "percent_off":
        headline = f"{pct:g}% off {where}"
    elif o.type == "flat_off":
        headline = f"{_rs(amount)} off {where}"
    elif o.type == "free_delivery":
        headline = "Free delivery"
    else:
        headline = f"{points} bonus points"
    terms = []
    if o.min_order_value:
        terms.append(f"min order {_rs(o.min_order_value)}")
    if o.type == "percent_off" and max_rs:
        terms.append(f"max discount {_rs(max_rs)}")
    if o.type == "free_delivery":
        terms.append(f"saves {_rs(delivery_fee)}")
    if o.type == "bonus_points" and cat != "any":
        terms.append(f"on {cat_label} orders")
    if excluded:
        terms.append("not valid on " + ", ".join(catalog.labels.get(c, c) for c in excluded))
    terms.append(f"valid till {valid_until:%d %b, %I:%M %p}")
    return headline, "; ".join(terms)


def evaluate(rules: RulesConfig, catalog: CatalogInfo, ctx: CustomerContext, budget_used_today_rs: float,
             now: datetime, relax_frequency: bool = False) -> Decision:
    """relax_frequency=True (an operator deliberately sending one offer) skips only the *frequency* limits: the
    weekly cap, the gap between offers and the same-offer cooldown. Opt-outs, discount caps, the per-offer max,
    the daily budget, the margin guards, the value floor and quiet hours all still apply."""
    lim = rules.limits
    f = ctx.features
    decision = Decision(ctx.customer_id, ctx.segment, [], [])

    # ---- customer-level limits ----
    week = [s for s in ctx.recent_offers if s.sent_at > now - timedelta(days=7)]
    last = max((s.sent_at for s in ctx.recent_offers), default=None)
    if ctx.opted_out:
        decision.customer_block = "Customer has opted out of offers."
    elif not relax_frequency and len(week) >= lim.max_offers_per_customer_per_week:
        decision.customer_block = (f"Already received {len(week)} offers this week "
                                   f"(limit {lim.max_offers_per_customer_per_week}).")
    elif not relax_frequency and last and now - last < timedelta(hours=lim.min_hours_between_offers):
        hours = (now - last).total_seconds() / 3600
        decision.customer_block = (f"Last offer was {hours:.0f}h ago "
                                   f"(must wait {lim.min_hours_between_offers}h between offers).")
    if decision.customer_block:
        return decision

    if lim.quiet_hours.contains(now.timetz().replace(tzinfo=None)):
        end = now.replace(hour=lim.quiet_hours.end.hour, minute=lim.quiet_hours.end.minute, second=0, microsecond=0)
        decision.send_after = end if end > now else end + timedelta(days=1)

    budget_left = lim.daily_budget_rs - budget_used_today_rs
    for o in rules.offers:
        def block(reason: str) -> None:
            decision.blocked.append(Blocked(o.key, o.label, reason))

        if not o.enabled:
            block("Offer is switched off.")
            continue
        if "all" not in o.segments and ctx.segment not in o.segments:
            block(f"Only for {', '.join(o.segments)}; customer is {ctx.segment}.")
            continue
        ok, why = check_conditions(f, o.audience)
        if not ok:
            block(f"Audience rule not met - {why[0]}.")
            continue
        recent_same = [s for s in ctx.recent_offers if s.offer_key == o.key
                       and s.sent_at > now - timedelta(days=lim.same_offer_cooldown_days)]
        if recent_same and not relax_frequency:
            block(f"Same offer already sent in the last {lim.same_offer_cooldown_days} days.")
            continue

        notes: list[str] = []
        cat = {"from_cart": f.get("last_abandoned_cart_category"),
               "top_category": f.get("top_category")}.get(o.category, o.category)
        if cat is None:
            missing = "abandoned cart" if o.category == "from_cart" else "favourite category"
            if o.fallback_category is None:
                block(f"No {missing} to base it on.")
                continue
            cat = o.fallback_category
            notes.append(f"no {missing}, so {'store-wide' if cat == 'any' else cat}")

        monetary = o.type in MONETARY_TYPES
        if monetary and f.get("buy_anyway_prob", 0) > lim.max_buy_anyway_prob_for_discounts:
            block(f"Likely to buy anyway ({f['buy_anyway_prob']:.0%} > "
                  f"{lim.max_buy_anyway_prob_for_discounts:.0%}); a discount would waste margin.")
            continue
        if monetary and f.get("return_rate", 0) > lim.max_return_rate_for_discounts:
            block(f"Returns {f['return_rate']:.0%} of items (limit {lim.max_return_rate_for_discounts:.0%} "
                  "for discounts).")
            continue

        pct = amount = points = max_rs = None
        excluded: list[str] = []
        est_order = f.get("last_abandoned_cart_value") if o.category == "from_cart" else None
        est_order = max(est_order or f.get("aov") or 600, o.min_order_value)
        if o.type == "percent_off":
            cap = rules.cap_pct(cat)
            pct = min(o.value, cap)
            if pct <= 0:
                block(f"Discounts are not allowed on {catalog.labels.get(cat, cat)}.")
                continue
            if pct < o.value:
                notes.append(f"capped from {o.value:g}% to {pct:g}% ({catalog.labels.get(cat, cat)} limit)")
            if cat == "any":
                excluded = [c for c in catalog.margins if rules.cap_pct(c) < pct]
            max_rs = min(o.max_discount_rs or lim.max_discount_rs_per_offer, lim.max_discount_rs_per_offer)
            liability = max_rs
            est_discount = min(est_order * pct / 100, max_rs)
        elif o.type == "flat_off":
            amount = int(o.value)
            if amount <= 0 or o.min_order_value <= 0:
                block("A ₹ discount needs an amount and a minimum order.")
                continue
            # The worst case is the amount off the smallest allowed order: it must fit the category's cap, and a
            # store-wide coupon may not be spent on categories whose cap is lower than that share.
            share = 100 * amount / o.min_order_value
            if share > rules.cap_pct(cat):
                block(f"{_rs(amount)} off a {_rs(o.min_order_value)} order is {share:.0f}%, above the "
                      f"{rules.cap_pct(cat):g}% limit for {catalog.labels.get(cat, cat) if cat != 'any' else 'the whole store'}.")
                continue
            if cat == "any":
                excluded = [c for c in catalog.margins if rules.cap_pct(c) < share]
            liability = est_discount = amount
        elif o.type == "free_delivery":
            liability = est_discount = lim.delivery_fee_rs
        else:
            points = int(o.value)
            if points <= 0:
                block("Bonus points need at least 1 point.")
                continue
            liability = est_discount = points * lim.point_value_rs

        if liability > budget_left:
            block(f"Daily offer budget nearly used ({_rs(budget_left)} left, this offer can cost "
                  f"up to {_rs(liability)}).")
            continue

        # Expected value: margin from the extra purchases the offer causes, minus the discount paid to everyone
        # who uses it, including customers who would have bought anyway while it is valid (the real cost).
        margin = catalog.margins.get(cat) or catalog.margins.get(f.get("top_category") or "", catalog.avg_margin)
        base = 1 - (1 - f.get("buy_anyway_prob", 0)) ** (o.valid_hours / (14 * 24))
        lift = min(0.6, max(0.01, o.expected_response * (0.5 + f.get("deal_affinity", 0.33))))
        incremental = lift * (1 - base)
        exp_cost = (base + incremental) * REDEEM_SHARE * est_discount
        exp_net = incremental * est_order * margin / 100 - exp_cost
        if exp_net < lim.min_expected_net_rs:
            block(f"Not worth it: expected value {_rs(exp_net)} is below the {_rs(lim.min_expected_net_rs)} floor.")
            continue

        valid_until = (decision.send_after or now) + timedelta(hours=o.valid_hours)
        headline, terms = _describe(o, cat, catalog.labels.get(cat, cat), pct, amount, points, max_rs, excluded,
                                    valid_until, catalog, lim.delivery_fee_rs)
        reasons = ([f"segment: {ctx.segment}"] if "all" not in o.segments else []) + why + notes
        decision.eligible.append(Candidate(
            offer_key=o.key, label=o.label, type=o.type, category=cat, percent=pct, amount_rs=amount,
            points=points, min_order_value=o.min_order_value, max_discount_rs=max_rs,
            excluded_categories=excluded, liability_rs=int(liability), expected_cost_rs=round(exp_cost, 1),
            expected_net_rs=round(exp_net, 1), priority=o.priority, valid_until=valid_until,
            headline=headline, terms=terms, reasons=reasons,
        ))

    decision.eligible.sort(key=lambda c: (-c.priority, -c.expected_net_rs))
    return decision
