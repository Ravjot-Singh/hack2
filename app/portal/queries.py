"""Portal-only read queries (always scoped to one business)."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Customer, CustomerFeature, OfferDecision, TelegramUser
from app.features.customer import _aware

SORTS = {
    "spend": (CustomerFeature.monetary_365.desc(),),
    "recent": (CustomerFeature.recency_days.is_(None), CustomerFeature.recency_days.asc()),
    "risk": (CustomerFeature.churn_risk.desc(),),
    "name": (Customer.name.asc(),),
}


def _like(text: str) -> str:
    return "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def customers(s: Session, tenant_id: int, q: str = "", group: str = "", telegram: bool = False, sort: str = "spend",
              limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    base = (select(Customer.id, Customer.external_ref, Customer.name, Customer.city, CustomerFeature.segment,
                   CustomerFeature.orders_365, CustomerFeature.monetary_365, CustomerFeature.recency_days,
                   CustomerFeature.churn_risk, TelegramUser.chat_id, TelegramUser.opted_out)
            .outerjoin(CustomerFeature, CustomerFeature.customer_id == Customer.id)
            .outerjoin(TelegramUser, TelegramUser.customer_id == Customer.id)
            .where(Customer.tenant_id == tenant_id))
    if q.strip():
        pat = _like(q.strip())
        base = base.where(or_(func.lower(Customer.name).like(pat, escape="\\"),
                              func.lower(Customer.external_ref).like(pat, escape="\\"),
                              func.lower(func.coalesce(Customer.phone, "")).like(pat, escape="\\"),
                              func.lower(func.coalesce(Customer.email, "")).like(pat, escape="\\")))
    if group:
        base = base.where(CustomerFeature.segment == group)
    if telegram:
        base = base.where(TelegramUser.chat_id.is_not(None))
    total = s.scalar(select(func.count()).select_from(base.subquery()))
    rows = s.execute(base.order_by(*SORTS.get(sort, SORTS["spend"]), Customer.id).limit(limit).offset(offset))
    return [dict(r._mapping) for r in rows], total or 0


def group_counts(s: Session, tenant_id: int) -> dict[str, int]:
    return dict(s.execute(select(CustomerFeature.segment, func.count()).where(CustomerFeature.tenant_id == tenant_id)
                          .group_by(CustomerFeature.segment)).all())


def offer_history(s: Session, customer_id: int, limit: int = 20) -> list[dict]:
    rows = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == customer_id,
                                                 OfferDecision.simulated.is_(False))
                     .order_by(OfferDecision.id.desc()).limit(limit))
    return [{"d": d, "created_at": _aware(d.created_at)} for d in rows]
