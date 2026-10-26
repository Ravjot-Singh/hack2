"""Deals: one offer idea for many customers, personalised per customer and sent within the rules.

A deal ("Diwali Deal: up to 15% off") becomes a separate one-off offer for each customer:
  category  = their favourite (or the whole store, or the closest of the chosen categories)
  amount    = the deal's amount, capped at that category's discount limit
Each personalised offer goes through the same `evaluate()` as every other offer, so frequency limits, margin
guards and the value floor apply. The deal's own budget replaces the daily budget and is spent on the most
valuable offers first; a matched control group (holdout) receives nothing, to measure what the deal added.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.error import TelegramError

from app import fairness, guardrails, preferences
from app.agent.chooser import choose
from app.agent.groq_client import JsonLLM, groq_json
from app.db.models import Customer, CustomerFeature, Deal, OfferDecision, OfferOutcome, TelegramUser
from app.db.session import session_scope
from app.delivery.push import MIRROR_BATCH_LIMIT, Mirror, push_offer, with_bot
from app.delivery.templates import deal_intro, offer_message
from app.features.customer import _aware
from app.measurement.holdout import in_holdout
from app.measurement.outcomes import simulate
from app.offers import service
from app.offers.service import intro_context, new_coupon
from app.rules.engine import Candidate, CustomerContext, SentOffer, evaluate
from app.rules.policy import TenantPolicy, current_policy
from app.rules.schema import Offer, RulesConfig, check_rules

log = logging.getLogger(__name__)

TYPES = {"percent_off": "% off", "flat_off": "₹ off", "free_delivery": "Free delivery", "bonus_points": "Bonus points"}
APPLIES = {"favourite": "Each customer's favourite category", "any": "Whole store",
           "categories": "Selected categories"}

# Plain-language groups for "why these customers won't get it".
REASON_GROUPS = [
    ("Customer has opted out", "Turned offers off"),
    ("Already received", "Already had the weekly maximum of offers"),
    ("Last offer was", "Got an offer too recently"),
    ("Same offer already", "Already got this deal"),
    ("Likely to buy anyway", "Would probably buy anyway, so a discount would only cost margin"),
    ("Returns ", "Returns a lot of what they buy"),
    ("Daily offer budget", "Not enough deal budget left"),
    ("Not worth it", "The discount would cost more than it earns"),
    ("Discounts are not allowed", "No discount allowed on their category"),
]


def simplify(reason: str) -> str:
    for prefix, label in REASON_GROUPS:
        if reason.startswith(prefix):
            return label
    return re.sub(r"\s*\(.*\)$", "", reason).rstrip(".")


@dataclass
class DealSpec:
    name: str
    type: str
    value: float
    applies_to: str
    categories: list[str]
    segments: list[str]
    min_order_value: int
    max_discount_rs: int | None
    valid_hours: int
    budget_rs: int

    @classmethod
    def from_deal(cls, d: Deal) -> DealSpec:
        return cls(d.name, d.type, d.value, d.applies_to, list(d.categories or []), list(d.segments or ["all"]),
                   d.min_order_value, d.max_discount_rs, d.valid_hours, d.budget_rs)

    @classmethod
    def from_form(cls, form) -> tuple[DealSpec | None, list[str]]:
        """form: the submitted FormData (multi-valued for categories and segments)."""
        try:
            otype = form.get("type") or "percent_off"
            spec = cls(
                name=(form.get("name") or "").strip()[:80], type=otype,
                value=0.0 if otype == "free_delivery" else float(form.get("value") or 0),
                applies_to=form.get("applies_to") or "favourite",
                categories=list(form.getlist("categories")),
                segments=list(form.getlist("segments")) or ["all"],
                min_order_value=int(float(form.get("min_order_value") or 0)),
                max_discount_rs=int(float(form["max_discount_rs"])) if otype == "percent_off" and form.get(
                    "max_discount_rs") else None,
                valid_hours=int(float(form.get("valid_hours") or 72)),
                budget_rs=int(float(form.get("budget_rs") or 0)),
            )
        except (ValueError, OverflowError):
            return None, ["Please enter numbers only in the amount, minimum order, maximum discount and budget."]
        return spec, []


def validate_spec(spec: DealSpec, policy: TenantPolicy) -> list[str]:
    problems = []
    if len(spec.name) < 2:
        problems.append("Give the deal a name customers will see, e.g. \"Diwali Deal\".")
    if spec.type not in TYPES:
        problems.append("Choose a deal type.")
    if spec.type != "free_delivery" and spec.value <= 0:
        problems.append("Enter an amount above zero.")
    if spec.type == "percent_off" and spec.value > 90:
        problems.append("A percentage discount can't be more than 90%.")
    if spec.applies_to not in APPLIES:
        problems.append("Choose what the deal applies to.")
    if spec.applies_to == "categories":
        if not spec.categories:
            problems.append("Pick at least one category.")
        elif any(c not in policy.catalog.margins for c in spec.categories):
            problems.append("One of the chosen categories doesn't exist.")
    keys = {s.key for s in policy.segments.segments}
    if any(s != "all" and s not in keys for s in spec.segments):
        problems.append("One of the chosen customer groups doesn't exist.")
    if not 12 <= spec.valid_hours <= 24 * 30:
        problems.append("Each customer's offer should be valid for between 12 hours and 30 days.")
    if spec.budget_rs < 1:
        problems.append("Set a budget for the deal (the most it may cost you in total).")
    if spec.budget_rs > 1_000_000_000 or spec.min_order_value > 10_000_000:
        problems.append("That budget or minimum order is too large.")
    if problems:
        return problems
    # The amount itself must fit the per-offer limits (percentages are capped per category later).
    sample_cat = spec.categories[0] if spec.applies_to == "categories" else "any"
    value = min(spec.value, policy.rules.cap_pct(sample_cat)) if spec.type == "percent_off" else spec.value
    offer = _offer_dict(spec, 0, sample_cat, value)
    data = policy.rules.model_dump(mode="json")
    data["offers"] = [offer]
    try:
        rules = RulesConfig(**data)
    except ValidationError as exc:
        return [f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors()]
    label = f"Offer '{offer['label']}' ({offer['key']})"
    return [p.replace(label, "This deal") for p in
            check_rules(rules, policy.catalog.margins, keys)]


def _offer_dict(spec: DealSpec, deal_id: int, category: str, value: float) -> dict:
    return {"key": f"deal_{deal_id}", "label": spec.name, "description": "Deal", "type": spec.type, "value": value,
            "category": category, "min_order_value": spec.min_order_value,
            "max_discount_rs": spec.max_discount_rs if spec.type == "percent_off" else None,
            "valid_hours": spec.valid_hours, "segments": ["all"], "priority": 10, "expected_response": 0.15}


def personalise(spec: DealSpec, deal_id: int, f: dict, policy: TenantPolicy) -> tuple[dict | None, str | None]:
    """This customer's version of the deal, or why there isn't one."""
    top = f.get("top_category")
    if spec.applies_to == "any":
        cat = "any"
    elif spec.applies_to == "favourite":
        if not top:
            return None, "No favourite category yet (no recent orders)"
        if spec.categories and top not in spec.categories:
            return None, "Their favourite category isn't part of this deal"
        cat = top
    else:
        cat = top if top in spec.categories else spec.categories[0]
    value = spec.value
    if spec.type == "percent_off":
        value = min(spec.value, policy.rules.cap_pct(cat))
        if value <= 0:
            return None, "No discount allowed on their category"
    elif spec.type == "flat_off":
        cap = policy.rules.cap_pct(cat)
        if not spec.min_order_value or 100 * spec.value / spec.min_order_value > cap:
            return None, "The ₹ amount is above their category's discount limit"
    return _offer_dict(spec, deal_id, cat, value), None


@dataclass
class PlanItem:
    customer_id: int
    name: str
    ref: str
    segment: str
    chat_id: int | None
    opted_out: bool
    true_segment: str | None
    price_sensitivity: float | None
    f: dict
    candidate: Candidate


@dataclass
class DealPlan:
    audience: int = 0
    send: list[PlanItem] = field(default_factory=list)
    holdout: list[PlanItem] = field(default_factory=list)
    blocked: Counter = field(default_factory=Counter)
    budget_stopped: int = 0
    send_after: datetime | None = None
    guard_blocked: list[tuple[PlanItem, list]] = field(default_factory=list)
    ceiling_limited: bool = False
    fairness: fairness.FairnessReport | None = None

    @property
    def liability(self) -> int:
        return sum(i.candidate.liability_rs for i in self.send)

    @property
    def expected_net(self) -> float:
        return sum(i.candidate.expected_net_rs for i in self.send)

    @property
    def on_telegram(self) -> int:
        return sum(1 for i in self.send if i.chat_id and not i.opted_out)

    @property
    def headlines(self) -> Counter:
        return Counter(i.candidate.headline for i in self.send)

    def summary(self) -> dict:
        return {"audience": self.audience, "eligible": len(self.send) + len(self.holdout) + self.budget_stopped,
                "to_send": len(self.send), "on_telegram": self.on_telegram,
                "coupons_only": len(self.send) - self.on_telegram, "holdout": len(self.holdout),
                "budget_stopped": self.budget_stopped, "liability": self.liability,
                "expected_net": round(self.expected_net), "blocked": dict(self.blocked.most_common()),
                "guard_blocked": len(self.guard_blocked), "ceiling_limited": self.ceiling_limited,
                "fairness": self.fairness.as_dict() if self.fairness else None}


def plan_deal(s: Session, tenant_id: int, slug: str, policy: TenantPolicy, spec: DealSpec, deal_id: int,
              now: datetime) -> DealPlan:
    lim = policy.rules.limits
    plan = DealPlan()
    q = (select(CustomerFeature, Customer.name, Customer.external_ref, Customer.true_segment,
                Customer.price_sensitivity, TelegramUser.chat_id, TelegramUser.opted_out,
                Customer.city, Customer.city_tier, Customer.gender, Customer.age)
         .join(Customer, Customer.id == CustomerFeature.customer_id)
         .outerjoin(TelegramUser, TelegramUser.customer_id == CustomerFeature.customer_id)
         .where(CustomerFeature.tenant_id == tenant_id))
    if "all" not in spec.segments:
        q = q.where(CustomerFeature.segment.in_(spec.segments))
    rows = s.execute(q).all()

    history: dict[int, list[SentOffer]] = {}
    lookback = now - timedelta(days=max(7, lim.same_offer_cooldown_days))
    for cid, key, ts in s.execute(select(OfferDecision.customer_id, OfferDecision.offer_key, OfferDecision.created_at)
                                  .where(OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(False),
                                         OfferDecision.outcome.in_(("sent", "deferred", "holdout")),
                                         OfferDecision.created_at > lookback)):
        history.setdefault(cid, []).append(SentOffer(key, _aware(ts)))

    base = policy.rules.model_copy(update={"limits": lim.model_copy(update={"daily_budget_rs": spec.budget_rs})})
    cols = [c.name for c in CustomerFeature.__table__.columns]
    treated: list[PlanItem] = []
    held: list[PlanItem] = []
    members: dict[int, fairness.Member] = {}  # everyone outside the control group, for the fairness check
    for feat, name, ref, true_seg, sens, chat_id, opted_out, city, city_tier, gender, age in rows:
        plan.audience += 1
        f = {c: getattr(feat, c) for c in cols}
        if not in_holdout(slug, f["customer_id"], policy.rules.measurement.holdout_pct):
            members[f["customer_id"]] = fairness.Member(
                f["customer_id"], fairness.attributes(city, city_tier, gender, age, f.get("tenure_days")))
        offer, reason = personalise(spec, deal_id, f, policy)
        if offer is None:
            plan.blocked[reason] += 1
            continue
        rules = base.model_copy(update={"offers": [Offer(**offer)]})
        ctx = CustomerContext(f["customer_id"], f, f["segment"], bool(opted_out), history.get(f["customer_id"], []))
        d = evaluate(rules, policy.catalog, ctx, 0, now)
        if d.customer_block or not d.eligible:
            plan.blocked[simplify(d.customer_block or d.blocked[0].reason)] += 1
            continue
        plan.send_after = d.send_after
        item = PlanItem(f["customer_id"], name, ref, f["segment"], chat_id, bool(opted_out), true_seg, sens, f, d.best)
        (held if in_holdout(slug, f["customer_id"], policy.rules.measurement.holdout_pct) else treated).append(item)

    # The guardrail harness re-checks every offer the budget would fund (money, contact, pause); the deal
    # budget may also never push today's total spend past the business's daily ceiling.
    biz_state = guardrails.business_state(s, tenant_id, now)
    states = guardrails.customer_states(s, tenant_id, [i.customer_id for i in treated], now)
    budget_left = min(spec.budget_rs, max(0, lim.total_spend_ceiling_rs - biz_state.spend_today))
    plan.ceiling_limited = budget_left < spec.budget_rs

    prefs = preferences.load([i.customer_id for i in treated])

    def guard(item: PlanItem, budget: float) -> list:
        cust = states[item.customer_id]
        p = prefs.get(item.customer_id)
        cust.opted_out, cust.quiet = item.opted_out, p.quiet_hours if p else None
        waits = plan.send_after is not None or preferences.quiet_until(p, now) is not None  # queued, not pushed
        return guardrails.check(policy, item.candidate, "deal", cust, biz_state, now,
                                pushes_now=bool(item.chat_id) and not item.opted_out and not waits,
                                budget_left=budget)

    unfunded: list[PlanItem] = []
    for item in sorted(treated, key=lambda i: -i.candidate.expected_net_rs):
        if item.candidate.liability_rs > budget_left:
            unfunded.append(item)
            continue
        violations = guard(item, budget_left)
        if violations:
            plan.guard_blocked.append((item, violations))
            plan.blocked["Stopped by a safety guardrail: " + violations[0].text.rstrip(".")] += 1
            continue
        guardrails.record_issue(biz_state, states[item.customer_id], item.candidate)
        budget_left -= item.candidate.liability_rs
        plan.send.append(item)

    # Fairness: rebalance the same budget so no customer group is left far behind (as far as the rules allow).
    for item in treated:
        m = members[item.customer_id]
        m.candidate, m.share, m.payload = item.candidate, fairness.discount_share(policy, item.candidate, item.f), item

    def accept(m: fairness.Member) -> bool:
        violations = guard(m.payload, float("inf"))  # the deal budget is enforced by the rebalancer itself
        if violations:
            plan.guard_blocked.append((m.payload, violations))
        return not violations

    def dropped(m: fairness.Member) -> None:
        cust = states[m.key]
        biz_state.spend_today -= m.candidate.liability_rs
        cust.offers_today -= 1
        cust.live_offer_keys.discard(m.candidate.offer_key)

    funded, plan.fairness = fairness.rebalance(
        list(members.values()), [members[i.customer_id] for i in plan.send],
        [members[i.customer_id] for i in unfunded], budget_left, policy.rules.fairness.min_ratio,
        policy.rules.fairness.min_group_size, accept=accept, on_drop=dropped,
        on_add=lambda m: guardrails.record_issue(biz_state, states[m.key], m.candidate))
    plan.send = sorted((m.payload for m in funded), key=lambda i: -i.candidate.expected_net_rs)
    sent_ids = {i.customer_id for i in plan.send}
    blocked_ids = {i.customer_id for i, _ in plan.guard_blocked}
    plan.budget_stopped = sum(1 for i in treated if i.customer_id not in sent_ids | blocked_ids)
    # Control group matched to the customers the budget actually reached.
    cutoff = min((i.candidate.expected_net_rs for i in plan.send), default=float("inf"))
    plan.holdout = [i for i in held if i.candidate.expected_net_rs >= cutoff]
    return plan


def execute_deal(deal_id: int, tenant_id: int, slug: str, store: str, prefix: str, voice: str, now: datetime,
                 llm: JsonLLM | None = groq_json) -> list[tuple[int, int, str]]:
    """Record every decision of the deal; return (decision_id, chat_id, message) for Telegram pushes."""
    rng = random.Random(f"deal:{deal_id}")
    with session_scope() as s:
        deal = s.get(Deal, deal_id)
        spec = DealSpec.from_deal(deal)
        policy = current_policy(slug)
        plan = plan_deal(s, tenant_id, slug, policy, spec, deal_id, now)
        lim = policy.rules.limits
        pushes: list[tuple[OfferDecision, int]] = []
        records: list[tuple[OfferDecision, PlanItem, bool]] = []
        prefs = preferences.load([i.customer_id for i in plan.send])
        for item in plan.send:
            c = item.candidate
            reachable = bool(item.chat_id) and not item.opted_out
            # Store quiet hours, or the customer's own quiet times: the message waits, the coupon exists now.
            send_after = max(filter(None, (plan.send_after, preferences.quiet_until(prefs.get(item.customer_id), now))),
                             default=None)
            deferred = send_after is not None and reachable
            first = item.name.split()[0]
            intro, chosen_by, ai_reason, ai_rejected = deal_intro(spec.name, first), "rules", None, None
            if reachable:  # the AI writes intros only for real people on Telegram (free-tier friendly)
                ictx = intro_context(item.name, store, c, item.f, policy)
                choice = choose(policy, tenant_id, item.customer_id, store, voice, item.name, item.f, [c], ictx, now,
                                llm)
                if choice.chosen_by == "ai":
                    intro, chosen_by, ai_reason = choice.intro_html, "ai", choice.ai_reason
                ai_rejected = choice.ai_rejected_reason
            coupon = new_coupon(prefix)
            valid_until = (send_after if deferred else now) + timedelta(hours=spec.valid_hours)
            rec = OfferDecision(
                tenant_id=tenant_id, customer_id=item.customer_id, created_at=now, trigger="deal", deal_id=deal_id,
                segment=item.segment, outcome="deferred" if deferred else "sent", shortlist=[c.offer_key],
                offer_key=c.offer_key, chosen_by=chosen_by, ai_reason=ai_reason, ai_rejected_reason=ai_rejected,
                headline=c.headline, terms=c.terms.rsplit("valid till", 1)[0] + f"valid till {valid_until:%d %b, %I:%M %p}",
                coupon_code=coupon, category=c.category, percent=c.percent, amount_rs=c.amount_rs, points=c.points,
                min_order_value=c.min_order_value, max_discount_rs=c.max_discount_rs, liability_rs=c.liability_rs,
                expected_net_rs=c.expected_net_rs, send_after=send_after if deferred else None,
                valid_until=valid_until)
            c.terms = rec.terms
            rec.message = offer_message(intro, c, coupon)
            s.add(rec)
            records.append((rec, item, True))
            if reachable and not deferred:
                pushes.append((rec, item.chat_id))
        for item in plan.holdout:
            c = item.candidate
            rec = OfferDecision(
                tenant_id=tenant_id, customer_id=item.customer_id, created_at=now, trigger="deal", deal_id=deal_id,
                segment=item.segment, outcome="holdout", shortlist=[c.offer_key], offer_key=c.offer_key,
                headline=c.headline, category=c.category, liability_rs=0, expected_net_rs=c.expected_net_rs,
                valid_until=now + timedelta(hours=spec.valid_hours),
                block_reason="Control group: held back on purpose to measure what the deal adds.")
            s.add(rec)
            records.append((rec, item, False))
        for item, violations in plan.guard_blocked:  # kept for the operator's Guardrails page; nothing issued
            c = item.candidate
            s.add(OfferDecision(
                tenant_id=tenant_id, customer_id=item.customer_id, created_at=now, trigger="deal", deal_id=deal_id,
                segment=item.segment, outcome="blocked", shortlist=[c.offer_key], offer_key=c.offer_key,
                headline=c.headline, category=c.category, liability_rs=0, expected_net_rs=c.expected_net_rs,
                block_reason=guardrails.reason(violations), guardrail=[v.as_dict() for v in violations]))
        s.flush()
        # Demo-data customers (not real Telegram users) get a simulated response, so results can be shown.
        for rec, item, sent in records:
            if item.true_segment is None or item.chat_id:
                continue
            c = item.candidate
            out = simulate(rng, features=item.f, true_segment=item.true_segment,
                           price_sensitivity=item.price_sensitivity or 0.5, window_hours=spec.valid_hours,
                           offer={"key": c.offer_key, "type": c.type, "percent": c.percent, "amount_rs": c.amount_rs,
                                  "points": c.points, "max_discount_rs": c.max_discount_rs,
                                  "min_order_value": c.min_order_value} if sent else None,
                           margin_pct=policy.catalog.margins.get(c.category) or policy.catalog.avg_margin,
                           delivery_fee=lim.delivery_fee_rs, point_value=lim.point_value_rs)
            s.add(OfferOutcome(decision_id=rec.id, tenant_id=tenant_id, purchased=out.purchased,
                               redeemed=out.redeemed, order_value=out.order_value, discount_cost=out.discount_cost,
                               margin_rs=out.margin_rs, resolved_at=rec.valid_until))
        summary = plan.summary()
        summary["queued"] = sum(1 for r, _, sent in records if sent and r.outcome == "deferred")
        summary["simulated"] = any(i.true_segment is not None for _, i, _ in records)
        deal.progress = {**summary, "stage": "delivering", "delivered": 0, "failed": 0}
        return [(rec.id, chat_id, rec.message) for rec, chat_id in pushes]


def _set_progress(deal_id: int, **changes) -> None:
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        progress = dict(d.progress or {})
        for k, v in changes.items():
            if k in ("status", "finished_at", "error"):
                setattr(d, k, v)
            else:
                progress[k] = v
        d.progress = progress


_running: set[asyncio.Task] = set()


async def run_deal(deal_id: int, tenant, tenant_id: int, running_bots: dict, now_fn,
                   llm: JsonLLM | None = groq_json) -> None:
    """Background job: record decisions, then push to Telegram users, updating progress as it goes."""
    try:
        pushes = await asyncio.to_thread(execute_deal, deal_id, tenant_id, tenant.slug, tenant.name, tenant.prefix,
                                         tenant.brand_voice, now_fn(), llm)
        delivered = failed = 0
        for decision_id, chat_id, message in pushes:
            try:
                await with_bot(running_bots, tenant.slug, tenant.bot_token,
                               lambda bot: push_offer(bot, chat_id, decision_id, message))
                delivered += 1
            except (TelegramError, LookupError) as exc:  # push_offer has logged the failure on the offer
                failed += 1
                log.warning("Deal %s: could not deliver to %s: %s", deal_id, chat_id, exc)
            if (delivered + failed) % 20 == 0:
                await asyncio.to_thread(_set_progress, deal_id, delivered=delivered, failed=failed)
        # Demo phone: a copy of the first offers to demo customers, then a summary of the rest.
        if tenant.demo_chat_id:
            mirror = Mirror(tenant.demo_chat_id, MIRROR_BATCH_LIMIT, f"Deal #{deal_id}")
            try:
                async def copy_all(bot):
                    for did in await asyncio.to_thread(service.sent_decision_ids, tenant_id, deal_id):
                        await mirror.copy(bot, did)
                    await mirror.finish(bot)
                await with_bot(running_bots, tenant.slug, tenant.bot_token, copy_all)
            except LookupError:
                pass
        await asyncio.to_thread(_set_progress, deal_id, delivered=delivered, failed=failed, stage="done",
                                status="done", finished_at=now_fn())
    except Exception as exc:  # never leave a deal stuck in "sending"
        log.exception("Deal %s failed", deal_id)
        await asyncio.to_thread(_set_progress, deal_id, status="failed", error=str(exc)[:300], finished_at=now_fn())


def start_deal(deal_id: int, tenant, tenant_id: int, running_bots: dict, now_fn) -> None:
    task = asyncio.get_running_loop().create_task(run_deal(deal_id, tenant, tenant_id, running_bots, now_fn))
    _running.add(task)
    task.add_done_callback(_running.discard)


def mark_interrupted_deals() -> None:
    """On startup: a deal still 'sending' was cut off by a restart."""
    with session_scope() as s:
        for d in s.scalars(select(Deal).where(Deal.status == "sending")):
            d.status, d.error = "failed", "Interrupted by a server restart. Offers recorded so far are kept."
