"""Read-only queries behind the dashboard pages (all tenant-scoped)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.agent.groq_client import calls_today
from app.config import get_settings
from app.data.generator import IST
from app.db.models import Customer, CustomerFeature, LLMCall, OfferDecision, RulesVersion, TelegramUser
from app.features.customer import _aware
from app.offers.service import budget_used_today


def overview(s: Session, tenant_id: int, now: datetime, daily_budget: int) -> dict:
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    live = OfferDecision.simulated.is_(False)
    customers = s.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id))
    linked, opted_out = s.execute(select(func.count(), func.sum(case((TelegramUser.opted_out, 1), else_=0)))
                                  .where(TelegramUser.tenant_id == tenant_id,
                                         TelegramUser.customer_id.is_not(None))).one()
    sent_live, claimed, dismissed, by_ai, ai_rejected = s.execute(select(
        func.sum(case((OfferDecision.outcome == "sent", 1), else_=0)),
        func.sum(case((OfferDecision.response == "claimed", 1), else_=0)),
        func.sum(case((OfferDecision.response == "dismissed", 1), else_=0)),
        func.sum(case((OfferDecision.chosen_by == "ai", 1), else_=0)),
        func.sum(case((OfferDecision.ai_rejected_reason.like("AI answer rejected%"), 1), else_=0)),
    ).where(OfferDecision.tenant_id == tenant_id, live)).one()
    sent_today = s.scalar(select(func.count()).select_from(OfferDecision).where(
        OfferDecision.tenant_id == tenant_id, live, OfferDecision.outcome == "sent",
        OfferDecision.created_at >= midnight)) or 0
    st = get_settings()
    return {
        "customers": customers or 0,
        "linked": linked or 0,
        "opted_out": int(opted_out or 0),
        "sent_live": int(sent_live or 0),
        "sent_today": sent_today,
        "claimed": int(claimed or 0),
        "dismissed": int(dismissed or 0),
        "by_ai": int(by_ai or 0),
        "ai_rejected": int(ai_rejected or 0),
        "budget_used": budget_used_today(s, tenant_id, now),
        "daily_budget": daily_budget,
        "llm_calls_today": calls_today(now),
        "llm_limit": st.llm_max_calls_per_day,
        "llm_enabled": st.llm_enabled and bool(st.groq_api_key),
    }


def daily_outcomes(s: Session, tenant_id: int, simulated: bool) -> tuple[list[str], dict[str, list[int]]]:
    """Decisions per day split by outcome (labels, {outcome: counts})."""
    rows = s.execute(select(OfferDecision.created_at, OfferDecision.outcome).where(
        OfferDecision.tenant_id == tenant_id, OfferDecision.simulated.is_(simulated)))
    per_day: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ts, outcome in rows:
        per_day[_aware(ts).astimezone(IST).date()][outcome] += 1
    days = sorted(per_day)
    outcomes = ["sent", "holdout", "no_offer", "deferred"]
    series = {o: [per_day[d].get(o, 0) for d in days] for o in outcomes if any(per_day[d].get(o) for d in days)}
    return [d.strftime("%d %b") for d in days], series


def segment_stats(s: Session, tenant_id: int) -> dict[str, dict]:
    rows = s.execute(select(
        CustomerFeature.segment, func.count(), func.avg(CustomerFeature.churn_risk),
        func.avg(CustomerFeature.buy_anyway_prob), func.avg(CustomerFeature.deal_affinity),
        func.avg(CustomerFeature.monetary_365), func.avg(CustomerFeature.orders_365),
        func.avg(CustomerFeature.expected_value_90d),
    ).where(CustomerFeature.tenant_id == tenant_id).group_by(CustomerFeature.segment))
    return {seg: {"n": n, "churn": churn or 0, "buy_anyway": ba or 0, "deal": deal or 0, "spend": spend or 0,
                  "orders": orders or 0, "ev90": ev or 0}
            for seg, n, churn, ba, deal, spend, orders, ev in rows}


def segment_customers(s: Session, tenant_id: int, segment: str, limit: int = 25) -> list[dict]:
    rows = s.execute(select(Customer.id, Customer.external_ref, Customer.name, Customer.city,
                            CustomerFeature.orders_365, CustomerFeature.monetary_365, CustomerFeature.recency_days,
                            CustomerFeature.churn_risk, CustomerFeature.segment_reason)
                     .join(CustomerFeature, CustomerFeature.customer_id == Customer.id)
                     .where(Customer.tenant_id == tenant_id, CustomerFeature.segment == segment)
                     .order_by(CustomerFeature.monetary_365.desc()).limit(limit))
    return [dict(r._mapping) for r in rows]


def decisions(s: Session, tenant_id: int, simulated: bool | None, outcome: str | None, offer: str | None,
              limit: int, offset: int) -> list[dict]:
    q = (select(OfferDecision, Customer.external_ref, Customer.name, TelegramUser.tg_name)
         .join(Customer, Customer.id == OfferDecision.customer_id)
         .outerjoin(TelegramUser, TelegramUser.customer_id == OfferDecision.customer_id)
         .where(OfferDecision.tenant_id == tenant_id))
    if simulated is not None:
        q = q.where(OfferDecision.simulated.is_(simulated))
    if outcome:
        q = q.where(OfferDecision.outcome == outcome)
    if offer:
        q = q.where(OfferDecision.offer_key == offer)
    out = []
    for d, ref, name, tg in s.execute(q.order_by(OfferDecision.id.desc()).limit(limit).offset(offset)):
        out.append({"d": d, "ref": ref, "name": name, "tg_name": tg, "created_at": _aware(d.created_at)})
    return out


def customer_history(s: Session, customer_id: int) -> list[OfferDecision]:
    return list(s.scalars(select(OfferDecision).where(OfferDecision.customer_id == customer_id)
                          .order_by(OfferDecision.id.desc()).limit(30)))


def find_customer(s: Session, tenant_id: int, query: str) -> int | None:
    q = query.strip()
    if q.isdigit():
        return s.scalar(select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.id == int(q)))
    return s.scalar(select(Customer.id).where(Customer.tenant_id == tenant_id,
                                              func.upper(Customer.external_ref) == q.upper()))


def linked_chats(s: Session, tenant_id: int) -> list[dict]:
    rows = s.execute(select(TelegramUser.chat_id, TelegramUser.tg_name, TelegramUser.opted_out,
                            TelegramUser.last_seen_at, Customer.id, Customer.external_ref, Customer.name,
                            CustomerFeature.segment)
                     .join(Customer, Customer.id == TelegramUser.customer_id)
                     .outerjoin(CustomerFeature, CustomerFeature.customer_id == Customer.id)
                     .where(TelegramUser.tenant_id == tenant_id)
                     .order_by(TelegramUser.last_seen_at.desc()))
    return [dict(r._mapping) for r in rows]


def rules_history(s: Session, tenant_id: int) -> list[RulesVersion]:
    return list(s.scalars(select(RulesVersion).where(RulesVersion.tenant_id == tenant_id)
                          .order_by(RulesVersion.id.desc()).limit(15)))


def llm_recent(s: Session, tenant_id: int, now: datetime) -> dict:
    since = now - timedelta(days=1)
    ok, failed, avg_ms = s.execute(select(func.sum(case((LLMCall.ok, 1), else_=0)),
                                          func.sum(case((LLMCall.ok, 0), else_=1)), func.avg(LLMCall.latency_ms))
                                   .where(LLMCall.tenant_id == tenant_id, LLMCall.created_at >= since)).one()
    return {"ok": int(ok or 0), "failed": int(failed or 0), "avg_ms": int(avg_ms or 0)}
