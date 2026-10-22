"""Offer service: links chats to personas, runs the decision pipeline and records every decision.

Synchronous (SQLAlchemy); the async bot calls it via asyncio.to_thread.
Pipeline for one request: snapshot features -> rules engine (with real offer history + today's budget)
-> pick candidate -> write message -> record decision.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.chooser import choose
from app.agent.groq_client import JsonLLM, groq_json
from app.db.models import Customer, CustomerFeature, OfferDecision, TelegramUser
from app.db.session import session_scope
from app.delivery.templates import offer_message, rs
from app.features.customer import _aware
from app.features.store import refresh_features
from app.measurement.holdout import in_holdout
from app.rules.engine import Candidate, CustomerContext, SentOffer, evaluate
from app.rules.policy import TenantPolicy

COUPON_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
# Someone deliberately asked for this offer (the customer, or the business's operator): never held back as a
# control-group member. Operator sends also skip the frequency limits (gap, weekly cap, same-offer cooldown);
# every price/margin rule still applies.
DIRECT_TRIGGERS = {"on_demand", "operator"}


@dataclass
class OfferResult:
    kind: str  # offer | deferred | repeat | opted_out | too_soon | none | holdout
    decision_id: int | None = None
    message: str | None = None
    next_time: datetime | None = None
    chat_id: int | None = None  # set when the customer can be messaged on Telegram
    coupon: str | None = None
    headline: str | None = None


# ---- chats & personas ----

def touch_user(tenant_id: int, chat_id: int, tg_name: str | None, now: datetime) -> dict:
    with session_scope() as s:
        u = s.scalar(select(TelegramUser).where(TelegramUser.tenant_id == tenant_id, TelegramUser.chat_id == chat_id))
        if u is None:
            u = TelegramUser(tenant_id=tenant_id, chat_id=chat_id, opted_out=False)
            s.add(u)
        u.tg_name = (tg_name or "")[:80] or None
        u.last_seen_at = now
        s.flush()
        return {"id": u.id, "customer_id": u.customer_id, "opted_out": u.opted_out}


def assign_persona(tenant_id: int, chat_id: int, segment: str | None) -> int | None:
    """Link the chat to a random unlinked synthetic customer (optionally from one segment)."""
    with session_scope() as s:
        u = s.scalar(select(TelegramUser).where(TelegramUser.tenant_id == tenant_id, TelegramUser.chat_id == chat_id))
        if u is None:
            return None
        u.customer_id = None
        s.flush()
        taken = select(TelegramUser.customer_id).where(TelegramUser.customer_id.is_not(None))
        q = select(CustomerFeature.customer_id).where(
            CustomerFeature.tenant_id == tenant_id, CustomerFeature.customer_id.not_in(taken))
        if segment:
            q = q.where(CustomerFeature.segment == segment)
        cid = s.scalar(q.order_by(func.random()).limit(1))
        u.customer_id = cid
        return cid


def set_opt_out(tenant_id: int, chat_id: int, value: bool) -> None:
    with session_scope() as s:
        u = s.scalar(select(TelegramUser).where(TelegramUser.tenant_id == tenant_id, TelegramUser.chat_id == chat_id))
        if u:
            u.opted_out = value


def _features(s: Session, tenant_id: int, customer_id: int, policy: TenantPolicy, now: datetime) -> dict:
    row = s.get(CustomerFeature, customer_id)
    if row is None:  # no snapshot yet: compute this one customer now
        refresh_features(s.connection(), tenant_id, now, policy.segments, [customer_id])
        row = s.get(CustomerFeature, customer_id)
    f = {c.name: getattr(row, c.name) for c in CustomerFeature.__table__.columns}
    for k in ("computed_at", "last_abandoned_cart_at", "next_points_expiry"):
        f[k] = _aware(f[k])
    return f


def chat_opted_out(s: Session, customer_id: int) -> bool:
    return bool(s.scalar(select(TelegramUser.opted_out).where(TelegramUser.customer_id == customer_id)))


def load_profile(tenant_id: int, customer_id: int, policy: TenantPolicy, now: datetime) -> tuple[dict, dict]:
    with session_scope() as s:
        c = s.get(Customer, customer_id)
        cust = {k: getattr(c, k) for k in ("id", "external_ref", "name", "city", "age", "loyalty_tier")}
        return cust, _features(s, tenant_id, customer_id, policy, now)


# ---- decisions ----

def _recent_offers(s: Session, customer_id: int, now: datetime, days: int) -> list[SentOffer]:
    rows = s.execute(select(OfferDecision.offer_key, OfferDecision.created_at).where(
        OfferDecision.customer_id == customer_id, OfferDecision.simulated.is_(False),
        OfferDecision.outcome.in_(("sent", "deferred")),
        OfferDecision.created_at > now - timedelta(days=days)))
    return [SentOffer(k, _aware(t)) for k, t in rows]


def budget_used_today(s: Session, tenant_id: int, now: datetime) -> int:
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    return int(s.scalar(select(func.coalesce(func.sum(OfferDecision.liability_rs), 0)).where(
        OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(False),
        OfferDecision.outcome.in_(("sent", "deferred")), OfferDecision.created_at >= midnight)) or 0)


def _next_allowed(recent: list[SentOffer], policy: TenantPolicy, now: datetime) -> datetime | None:
    lim = policy.rules.limits
    times = []
    if recent:
        times.append(max(r.sent_at for r in recent) + timedelta(hours=lim.min_hours_between_offers))
    week = sorted(r.sent_at for r in recent if r.sent_at > now - timedelta(days=7))
    cap = lim.max_offers_per_customer_per_week
    if cap and len(week) >= cap:
        times.append(week[len(week) - cap] + timedelta(days=7))
    return max(times) if times else None


def new_coupon(prefix: str) -> str:
    return f"{prefix}-" + "".join(secrets.choice(COUPON_ALPHABET) for _ in range(6))


def intro_context(cust_name: str, store: str, c: Candidate, f: dict, policy: TenantPolicy) -> dict:
    labels = policy.catalog.labels
    return {
        "first": cust_name.split()[0],
        "store": store,
        "cat": "everything" if c.category == "any" else labels.get(c.category, c.category),
        "cart_value": rs(f["last_abandoned_cart_value"]) if f.get("last_abandoned_cart_value") else None,
        "expiring": f"{f['points_expiring_30d']:,}" if f.get("points_expiring_30d") else None,
        "expiry_date": f["next_points_expiry"].strftime("%d %b") if f.get("next_points_expiry") else None,
    }


def request_offer(tenant_id: int, customer_id: int, policy: TenantPolicy, store: str, coupon_prefix: str,
                  trigger: str, now: datetime, voice: str = "friendly", llm: JsonLLM | None = groq_json,
                  tenant_slug: str = "") -> OfferResult:
    """llm=None forces the rules + template path. Proactive triggers (anything but on_demand) respect the
    holdout group; a person asking for an offer is always answered."""
    lim = policy.rules.limits
    with session_scope() as s:
        if trigger == "on_demand":  # re-show a live offer instead of making a new one
            live = s.scalar(select(OfferDecision).where(
                OfferDecision.customer_id == customer_id, OfferDecision.simulated.is_(False),
                OfferDecision.outcome == "sent",
                OfferDecision.response.is_(None), OfferDecision.valid_until > now,
            ).order_by(OfferDecision.created_at.desc()).limit(1))
            if live is not None:
                return OfferResult("repeat", live.id, live.message)

        user = s.scalar(select(TelegramUser).where(TelegramUser.customer_id == customer_id))
        f = _features(s, tenant_id, customer_id, policy, now)
        recent = _recent_offers(s, customer_id, now, max(7, lim.same_offer_cooldown_days))
        ctx = CustomerContext(customer_id, f, f["segment"], bool(user and user.opted_out), recent)
        decision = evaluate(policy.rules, policy.catalog, ctx, budget_used_today(s, tenant_id, now), now,
                            relax_frequency=trigger == "operator")

        rec = OfferDecision(tenant_id=tenant_id, customer_id=customer_id, created_at=now, trigger=trigger,
                            segment=f["segment"], outcome="no_offer", liability_rs=0,
                            shortlist=[c.offer_key for c in decision.eligible])
        if decision.customer_block or not decision.eligible:
            s.add(rec)
            rec.block_reason = (decision.customer_block or "No offer passed the rules: "
                                + "; ".join(f"{b.label}: {b.reason}" for b in decision.blocked))[:300]
            s.flush()
            if ctx.opted_out:
                return OfferResult("opted_out", rec.id)
            if decision.customer_block:
                return OfferResult("too_soon", rec.id, next_time=_next_allowed(recent, policy, now))
            return OfferResult("none", rec.id)

        if trigger not in DIRECT_TRIGGERS and in_holdout(tenant_slug, customer_id,
                                                         policy.rules.measurement.holdout_pct):
            b = decision.best
            rec.outcome, rec.offer_key, rec.headline, rec.category = "holdout", b.offer_key, b.headline, b.category
            rec.expected_net_rs, rec.valid_until = b.expected_net_rs, b.valid_until
            rec.block_reason = "Holdout group: no offer, used to measure what offers really add."
            s.add(rec)
            s.flush()
            return OfferResult("holdout", rec.id)

        # AI picks from the rule-approved shortlist and writes the intro; the validator re-checks it, and the
        # rules' own top pick + template is used if the AI is off, over budget, failing or rejected.
        cust_name = s.get(Customer, customer_id).name
        ictx = intro_context(cust_name, store, decision.best, f, policy)
        choice = choose(policy, tenant_id, customer_id, store, voice, cust_name, f, decision.eligible, ictx, now, llm)
        c, intro = choice.candidate, choice.intro_html
        s.add(rec)
        rec.ai_rejected_reason, rec.ai_reason = choice.ai_rejected_reason, choice.ai_reason
        coupon = new_coupon(coupon_prefix)
        # A customer asking for an offer is not an interruption, so quiet hours don't apply on demand. They only
        # hold back a *message*: for customers not on Telegram the coupon is simply saved.
        reachable = user is not None and not user.opted_out
        deferred = decision.send_after is not None and trigger != "on_demand" and reachable
        valid_until = c.valid_until if deferred else now + (c.valid_until - (decision.send_after or now))
        rec.outcome = "deferred" if deferred else "sent"
        rec.send_after = decision.send_after if deferred else None
        rec.offer_key, rec.chosen_by = c.offer_key, choice.chosen_by
        rec.headline, rec.terms = c.headline, c.terms
        rec.coupon_code = coupon
        rec.category, rec.percent, rec.amount_rs, rec.points = c.category, c.percent, c.amount_rs, c.points
        rec.min_order_value, rec.max_discount_rs = c.min_order_value, c.max_discount_rs
        rec.liability_rs, rec.expected_net_rs, rec.valid_until = c.liability_rs, c.expected_net_rs, valid_until
        if not deferred and decision.send_after is not None:
            c.terms = c.terms.rsplit("valid till", 1)[0] + f"valid till {valid_until:%d %b, %I:%M %p}"
            rec.terms = c.terms
        rec.message = offer_message(intro, c, coupon)
        s.flush()
        return OfferResult("deferred" if deferred else "offer", rec.id, rec.message, next_time=rec.send_after,
                           chat_id=user.chat_id if reachable else None, coupon=coupon, headline=c.headline)


def set_message_id(decision_id: int, message_id: int) -> None:
    with session_scope() as s:
        d = s.get(OfferDecision, decision_id)
        d.telegram_message_id = message_id
        if d.outcome == "deferred":
            d.outcome = "sent"


def linked_users(tenant_id: int) -> list[tuple[int, int, bool]]:
    """(chat_id, customer_id, opted_out) for every chat linked to a persona."""
    with session_scope() as s:
        return [tuple(r) for r in s.execute(select(TelegramUser.chat_id, TelegramUser.customer_id,
                                                   TelegramUser.opted_out).where(
            TelegramUser.tenant_id == tenant_id, TelegramUser.customer_id.is_not(None)))]


def chat_for_customer(customer_id: int) -> int | None:
    with session_scope() as s:
        return s.scalar(select(TelegramUser.chat_id).where(TelegramUser.customer_id == customer_id))


def due_deferred(tenant_id: int, now: datetime) -> list[tuple[int, int, str]]:
    """(decision_id, chat_id, message) for deferred offers whose quiet hours are over."""
    with session_scope() as s:
        rows = s.execute(select(OfferDecision.id, TelegramUser.chat_id, OfferDecision.message, TelegramUser.opted_out)
                         .join(TelegramUser, TelegramUser.customer_id == OfferDecision.customer_id)
                         .where(OfferDecision.tenant_id == tenant_id, OfferDecision.outcome == "deferred",
                                OfferDecision.simulated.is_(False), OfferDecision.send_after <= now))
        return [(i, chat, msg) for i, chat, msg, opted_out in rows if not opted_out]


def respond(tenant_id: int, chat_id: int, decision_id: int, response: str, now: datetime) -> tuple[str, dict | None]:
    """Record claimed/dismissed. Returns (status, decision info); status: ok | not_yours | done | expired."""
    with session_scope() as s:
        d = s.get(OfferDecision, decision_id)
        u = s.scalar(select(TelegramUser).where(TelegramUser.tenant_id == tenant_id, TelegramUser.chat_id == chat_id))
        if d is None or u is None or d.customer_id != u.customer_id:
            return "not_yours", None
        info = {"coupon": d.coupon_code, "headline": d.headline, "terms": d.terms, "response": d.response}
        if d.response is not None:
            return "done", info
        if d.valid_until and _aware(d.valid_until) < now:
            return "expired", info
        d.response, d.responded_at = response, now
        return "ok", info
