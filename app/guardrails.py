"""Guardrail harness: the last gate before any coupon is issued.

The rules engine decides what is *allowed*; this module independently re-checks the final offer about to go
out — whoever picked it (rules, bandit, AI, an operator, a deal) — against hard invariants, so a bug or a bad
edit upstream can never turn into money lost or a spammed customer:

  money    discount within the category cap and margin (store-wide offers must exclude lower-cap categories),
           within the per-offer maximum, within today's budget (or the deal's) and the total daily ceiling
  contact  not opted out, no push during quiet hours, at most N offers per customer per day (even hand-sent),
           no second live coupon of the same offer
  pause    the business pressed "Pause all automatic offers": campaigns, deals and events stop

A violation blocks the send; callers record the decision with outcome "blocked" and the reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import OfferDecision, Tenant
from app.rules.engine import Candidate
from app.rules.policy import TenantPolicy
from app.rules.schema import QuietHours

DIRECT_TRIGGERS = {"on_demand", "operator"}  # someone asked for this offer: not stopped by the pause switch
ISSUED = ("sent", "deferred")


@dataclass(frozen=True)
class Violation:
    code: str  # money | contact | pause
    text: str

    def as_dict(self) -> dict:
        return {"code": self.code, "text": self.text}


@dataclass
class CustomerState:
    opted_out: bool = False
    offers_today: int = 0
    live_offer_keys: set[str] = field(default_factory=set)
    quiet: QuietHours | None = None  # the customer's own quiet times (/preferences)


@dataclass
class BusinessState:
    paused: bool = False
    spend_today: float = 0.0  # worst-case cost of everything issued today, every trigger


def _midnight(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(0), now.tzinfo)


def business_state(s: Session, tenant_id: int, now: datetime, simulated: bool = False) -> BusinessState:
    paused = bool(s.scalar(select(Tenant.paused).where(Tenant.id == tenant_id)))
    spend = s.scalar(select(func.coalesce(func.sum(OfferDecision.liability_rs), 0)).where(
        OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(simulated),
        OfferDecision.outcome.in_(ISSUED), OfferDecision.created_at >= _midnight(now)))
    return BusinessState(paused, float(spend or 0))


def customer_states(s: Session, tenant_id: int, customer_ids: list[int], now: datetime,
                    simulated: bool = False) -> dict[int, CustomerState]:
    """One query for a whole batch. Opt-outs are passed in by callers (they already load the chat)."""
    states = {cid: CustomerState() for cid in customer_ids}
    if not customer_ids:
        return states
    rows = s.execute(select(OfferDecision.customer_id, OfferDecision.offer_key, OfferDecision.created_at,
                            OfferDecision.valid_until, OfferDecision.response).where(
        OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(simulated),
        OfferDecision.outcome.in_(ISSUED), OfferDecision.customer_id.in_(customer_ids),
        (OfferDecision.created_at >= _midnight(now)) | (OfferDecision.valid_until > now)))
    midnight = _midnight(now)
    for cid, key, created, until, response in rows:
        st = states[cid]
        if _aware(created, now) >= midnight:
            st.offers_today += 1
        if until is not None and _aware(until, now) > now and response is None and key:
            st.live_offer_keys.add(key)
    return states


def _aware(ts: datetime, now: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=now.tzinfo)


def check(policy: TenantPolicy, c: Candidate, trigger: str, cust: CustomerState, biz: BusinessState,
          now: datetime, pushes_now: bool, budget_left: float | None = None, simulated: bool = False) -> list[Violation]:
    """budget_left: the deal's remaining budget (deals); None = today's daily budget applies (everything else).
    pushes_now: a Telegram message goes out immediately (not queued for after quiet hours)."""
    rules, lim, margins = policy.rules, policy.rules.limits, policy.catalog.margins
    out: list[Violation] = []

    def money(text: str) -> None:
        out.append(Violation("money", text))

    def contact(text: str) -> None:
        out.append(Violation("contact", text))

    # ---- pause ----
    if biz.paused and trigger not in DIRECT_TRIGGERS:
        out.append(Violation("pause", "Automatic offers are paused for this business."))

    # ---- money ----
    cat = c.category
    if c.type == "percent_off" and c.percent is not None:
        share = c.percent
    elif c.type == "flat_off" and c.amount_rs:
        share = 100 * c.amount_rs / c.min_order_value if c.min_order_value else float("inf")
    else:
        share = None
    if share is not None:
        if share > rules.cap_pct(cat):
            money(f"{share:.0f}% off is above the {rules.cap_pct(cat):g}% limit for "
                  f"{'the whole store' if cat == 'any' else policy.catalog.labels.get(cat, cat)}.")
        if cat != "any" and share > margins.get(cat, 0):
            money(f"{share:.0f}% off is above the {margins.get(cat, 0):g}% margin of "
                  f"{policy.catalog.labels.get(cat, cat)}: every sale would lose money.")
        if cat == "any":
            leaks = [k for k in margins if rules.cap_pct(k) < share and k not in c.excluded_categories]
            if leaks:
                money(f"A store-wide {share:.0f}% offer must exclude "
                      f"{', '.join(policy.catalog.labels.get(k, k) for k in leaks)} (lower limits).")
    if c.liability_rs > lim.max_discount_rs_per_offer:
        money(f"Can cost up to ₹{c.liability_rs:,}, above the ₹{lim.max_discount_rs_per_offer:,} per-offer limit.")
    if not simulated:  # simulations allocate their own budget and never touch live money
        if budget_left is not None:
            if c.liability_rs > budget_left:
                money(f"Deal budget used up (₹{budget_left:,.0f} left, this offer can cost ₹{c.liability_rs:,}).")
        elif biz.spend_today + c.liability_rs > lim.daily_budget_rs:
            money(f"Today's offer budget of ₹{lim.daily_budget_rs:,} would be exceeded.")
        ceiling = lim.total_spend_ceiling_rs
        if biz.spend_today + c.liability_rs > ceiling:
            money(f"Today's total spend ceiling of ₹{ceiling:,} would be exceeded.")

    # ---- contact ----
    if cust.opted_out:
        contact("The customer turned offers off.")
    if trigger != "on_demand":
        clock = now.timetz().replace(tzinfo=None)
        if pushes_now and lim.quiet_hours.contains(clock):
            contact("Would message the customer during quiet hours.")
        elif pushes_now and cust.quiet is not None and cust.quiet.contains(clock):
            contact("Would message the customer during the quiet times they chose.")
        if cust.offers_today >= lim.max_offers_per_customer_per_day:
            contact(f"Already {cust.offers_today} offer(s) today (most {lim.max_offers_per_customer_per_day} "
                    "per customer per day).")
        if c.offer_key in cust.live_offer_keys:
            contact(f"They already have a live, unused '{c.label}' coupon.")
    return out


def record_issue(biz: BusinessState, cust: CustomerState, c: Candidate) -> None:
    """Keep the batch state current after an offer is issued (deals and campaigns check many in a row)."""
    biz.spend_today += c.liability_rs
    cust.offers_today += 1
    cust.live_offer_keys.add(c.offer_key)


def reason(violations: list[Violation]) -> str:
    return ("Guardrail: " + " ".join(v.text for v in violations))[:300]
