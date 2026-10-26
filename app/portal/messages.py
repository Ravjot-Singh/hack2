"""Portal "Messages": every offer the engine produced for a customer, the exact message, who it went to, how
(Telegram, coupon only, demo-phone copy) and what happened next, as a status timeline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape, unescape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select

from app.db.models import Customer, Delivery, OfferDecision, TelegramUser
from app.db.session import session_scope
from app.features.customer import _aware
from app.offers.service import TRIGGER_NAMES
from app.portal import routes as base
from app.portal.auth import Business, current_business

router = APIRouter(prefix="/app/messages")
PAGE = 50

# Filter → which decisions it covers (applied in SQL where possible, else on the computed status).
STATUS_FILTERS = {
    "": "All messages", "delivered": "Delivered on Telegram", "coupon": "Coupon only", "queued": "Queued",
    "failed": "Failed", "claimed": "Claimed", "dismissed": "Not now", "expired": "Expired",
    "blocked": "Stopped by guardrail", "holdout": "Control group", "not_sent": "Not sent",
}
OUTCOME_FOR = {"blocked": "blocked", "holdout": "holdout", "not_sent": "no_offer", "queued": "deferred"}


@dataclass
class Step:
    when: datetime | None
    title: str
    detail: str = ""
    tone: str = ""  # sent | warn | holdout | deferred | no_offer | claimed


def telegram_html(message: str | None) -> str:
    """The stored Telegram message as safe HTML: decoded once (it is stored Telegram-escaped), everything escaped
    again, then only Telegram's own simple tags (b, i, u, s, code) restored and line breaks kept."""
    text = escape(unescape(message or ""))
    text = re.sub(r"&lt;(/?)(b|i|u|s|code)&gt;", r"<\1\2>", text)
    return text.replace("\n", "<br>")


def _channel(d: OfferDecision, dels: list[Delivery], on_telegram: bool) -> str:
    to_customer = [x for x in dels if x.kind == "customer"]
    parts = []
    if to_customer:
        parts.append("Telegram")
    elif d.outcome in ("sent", "deferred"):
        parts.append("Telegram (queued)" if d.outcome == "deferred" else
                     "Telegram" if on_telegram and d.trigger == "on_demand" else "Coupon only")
    if any(x.kind == "mirror" for x in dels):
        parts.append("copy on demo phone")
    return " + ".join(parts) or "–"


def status_of(d: OfferDecision, dels: list[Delivery], now: datetime) -> tuple[str, str, str]:
    """(filter key, label, badge tone) for the message's current state."""
    if d.outcome == "blocked":
        return "blocked", "Stopped by guardrail", "warn"
    if d.outcome == "holdout":
        return "holdout", "Control group", "holdout"
    if d.outcome == "no_offer":
        return "not_sent", "Not sent", "no_offer"
    if d.response == "claimed":
        return "claimed", "Claimed", "claimed"
    if d.response == "dismissed":
        return "dismissed", "Not now", ""
    customer = [x for x in dels if x.kind == "customer"]
    if customer and customer[-1].status == "failed":
        return "failed", "Failed", "warn"
    if d.valid_until and _aware(d.valid_until) < now:
        return "expired", "Expired", "no_offer"
    if d.outcome == "deferred":
        return "queued", "Queued", "deferred"
    if customer or d.trigger == "on_demand":
        return "delivered", "Delivered on Telegram", "sent"
    return "coupon", "Coupon only", "sent"


def timeline(d: OfferDecision, dels: list[Delivery], on_telegram: bool, now: datetime,
             offer_label: str, segment_labels: dict) -> list[Step]:
    steps = [Step(_aware(d.created_at), f"{TRIGGER_NAMES.get(d.trigger, d.trigger)}: engine decided",
                  " · ".join(filter(None, [
                      f"group {segment_labels.get(d.segment, d.segment)}",
                      f"offer {offer_label}" if d.offer_key else "",
                      {"bandit": "picked by the learning engine", "ai": "picked and written by the AI",
                       "rules": "picked by your rules"}.get(d.chosen_by or "", "")])))]
    if d.outcome == "blocked":
        reasons = "; ".join(v.get("text", "") for v in (d.guardrail or [])) or (d.block_reason or "")
        steps.append(Step(_aware(d.created_at), "Stopped by a safety guardrail", reasons, "warn"))
        return steps
    if d.outcome == "holdout":
        steps.append(Step(_aware(d.created_at), "Held back: control group",
                          "On purpose, so results can show what offers really add.", "holdout"))
        return steps
    if d.outcome == "no_offer":
        steps.append(Step(_aware(d.created_at), "Not sent", d.block_reason or "", "no_offer"))
        return steps
    steps.append(Step(_aware(d.created_at), "Coupon created",
                      f"{d.coupon_code} · {d.headline}" + (f" · can cost up to ₹{d.liability_rs:,}" if d.liability_rs else ""),
                      "sent"))
    if d.send_after:
        steps.append(Step(_aware(d.created_at), "Queued for quiet hours",
                          f"Goes out on Telegram at {_aware(d.send_after):%d %b, %I:%M %p}", "deferred"))
    for x in dels:
        if x.kind == "customer":
            steps.append(Step(_aware(x.created_at), "Delivered to their Telegram" if x.status == "sent"
                              else "Telegram didn't accept it", x.error or "", "sent" if x.status == "sent" else "warn"))
        else:
            steps.append(Step(_aware(x.created_at), "Copy sent to your demo phone" if x.status == "sent"
                              else "Demo-phone copy failed", x.error or "", "sent" if x.status == "sent" else "warn"))
    if d.outcome == "sent" and not any(x.kind == "customer" for x in dels):
        if d.trigger == "on_demand" and on_telegram:
            steps.append(Step(_aware(d.created_at), "Shown in their Telegram chat", "They asked with /offers.", "sent"))
        else:
            steps.append(Step(_aware(d.created_at), "Coupon saved (not on Telegram)",
                              "Share the code through your own channels (email, SMS, app).", "sent"))
    if d.response:
        steps.append(Step(_aware(d.responded_at) if d.responded_at else None,
                          "Claimed by the customer" if d.response == "claimed" else "Customer said not now",
                          "", "claimed" if d.response == "claimed" else ""))
    elif d.valid_until and _aware(d.valid_until) < now:
        steps.append(Step(_aware(d.valid_until), "Expired unused", "", "no_offer"))
    elif d.valid_until:
        steps.append(Step(None, f"Valid until {_aware(d.valid_until):%d %b, %I:%M %p}", "Waiting for the customer.", ""))
    return steps


def _deliveries(s, ids: list[int]) -> dict[int, list[Delivery]]:
    out: dict[int, list[Delivery]] = {i: [] for i in ids}
    if ids:
        for x in s.scalars(select(Delivery).where(Delivery.decision_id.in_(ids)).order_by(Delivery.id)):
            out[x.decision_id].append(x)
    return out


@router.get("", response_class=HTMLResponse)
def messages(request: Request, biz: Business = Depends(current_business), feature: str = "", status: str = "",
             q: str = "", page_no: int = 1):
    now = base._now()
    page_no = min(max(1, page_no), 10_000)
    labels = base._labels(biz)
    query = (select(OfferDecision, Customer.name, Customer.external_ref, TelegramUser.chat_id)
             .join(Customer, Customer.id == OfferDecision.customer_id)
             .outerjoin(TelegramUser, TelegramUser.customer_id == OfferDecision.customer_id)
             .where(OfferDecision.tenant_id == biz.tenant_id, OfferDecision.simulated.is_(False)))
    if feature:
        query = query.where(OfferDecision.trigger == feature)
    if status in OUTCOME_FOR:
        query = query.where(OfferDecision.outcome == OUTCOME_FOR[status])
    elif status in ("claimed", "dismissed"):
        query = query.where(OfferDecision.response == status)
    elif status:
        query = query.where(OfferDecision.outcome.in_(("sent", "deferred")))
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(Customer.name).like(like), func.lower(Customer.external_ref).like(like),
                                func.lower(OfferDecision.coupon_code).like(like)))
    rows_out = []
    with session_scope() as s:
        # Computed statuses (delivered / coupon / failed / expired) are filtered after loading a wider window.
        window = PAGE * (4 if status in ("delivered", "coupon", "failed", "expired") else 1)
        rows = s.execute(query.order_by(OfferDecision.id.desc()).limit(window + 1)
                         .offset((page_no - 1) * window)).all()
        more = len(rows) > window
        rows = rows[:window]
        dels = _deliveries(s, [r[0].id for r in rows])
        for d, name, ref, chat in rows:
            key, label, tone = status_of(d, dels[d.id], now)
            if status and status not in OUTCOME_FOR and status not in ("claimed", "dismissed") and key != status:
                continue
            rows_out.append({"d": d, "name": name, "ref": ref, "status": label, "tone": tone,
                             "channel": _channel(d, dels[d.id], chat is not None),
                             "feature": TRIGGER_NAMES.get(d.trigger, d.trigger)})
    features = {k: v for k, v in TRIGGER_NAMES.items()}

    def qs(**changes) -> str:
        params = {k: v for k, v in {"feature": feature, "status": status, "q": q, "page_no": page_no,
                                    **changes}.items() if v}
        return "?" + urlencode(params)

    return base.page(request, "messages.html", biz, "messages", rows=rows_out[:PAGE], features=features,
                     statuses=STATUS_FILTERS, feature=feature, status=status, q=q, page_no=page_no, more=more,
                     qs=qs, **labels)


@router.get("/{decision_id}", response_class=HTMLResponse)
def message(request: Request, decision_id: int, biz: Business = Depends(current_business)):
    now = base._now()
    labels = base._labels(biz)
    if decision_id > 2**62:
        raise HTTPException(404, "Message not found")
    with session_scope() as s:
        d = s.get(OfferDecision, decision_id)
        if d is None or d.tenant_id != biz.tenant_id:
            raise HTTPException(404, "Message not found")
        c = s.get(Customer, d.customer_id)
        chat = s.scalar(select(TelegramUser).where(TelegramUser.customer_id == d.customer_id))
        dels = _deliveries(s, [d.id])[d.id]
        _, status_label, tone = status_of(d, dels, now)
        steps = timeline(d, dels, chat is not None, now, labels["offer_labels"].get(d.offer_key, d.offer_key or ""),
                         labels["segment_labels"])
        info = {"d": d, "customer": {"id": c.id, "name": c.name, "ref": c.external_ref, "demo": c.true_segment is not None},
                "telegram": chat is not None, "status": status_label, "tone": tone,
                "channel": _channel(d, dels, chat is not None), "feature": TRIGGER_NAMES.get(d.trigger, d.trigger),
                "preview": telegram_html(d.message), "steps": steps,
                "deliveries": [{"kind": x.kind, "status": x.status, "error": x.error, "at": _aware(x.created_at)}
                               for x in dels]}
    return base.page(request, "message.html", biz, "messages", m=info, **labels)
