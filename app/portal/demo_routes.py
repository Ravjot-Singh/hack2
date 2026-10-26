"""Portal "Demo" page: make something happen to a demo customer (a cart left behind, a birthday coming up, points
about to expire, going quiet) and watch the engine react through the normal pipeline. Demo (synthetic)
customers only: events change their data, so a business's own customers are never touched."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select

from app.db.models import Customer, CustomerFeature, TelegramUser
from app.db.session import session_scope
from app.delivery.push import with_bot
from app.offers import service
from app.portal import routes as base
from app.portal.auth import Business, current_business
from app.triggers import events

router = APIRouter(prefix="/app/demo")
EVENT_BUTTONS = {
    "cart_abandoned": ("🛒 Leaves a cart", "A cart from their favourite category, left 10 minutes ago."),
    "birthday": ("🎂 Birthday in 2 days", "Their birthday moves to the day after tomorrow."),
    "points_expiring": ("⏳ Points about to expire", "150 points that expire in 10 days."),
    "lapsed": ("💤 Goes quiet", "Their orders move back about five months, so they look lapsed (win-back)."),
}


def _demo_customer(s, biz: Business, customer_id: int) -> Customer:
    c = s.get(Customer, customer_id)
    if c is None or c.tenant_id != biz.tenant_id:
        raise HTTPException(404, "Customer not found")
    if c.true_segment is None:
        raise HTTPException(400, "Demo events only work on demo customers, never on your own customers' data.")
    return c


@router.get("", response_class=HTMLResponse)
def demo(request: Request, biz: Business = Depends(current_business), q: str = "", customer_id: int = 0):
    labels = base._labels(biz)
    with session_scope() as s:
        has_demo = s.scalar(select(Customer.id).where(Customer.tenant_id == biz.tenant_id,
                                                      Customer.true_segment.is_not(None)).limit(1)) is not None
        query = (select(Customer.id, Customer.name, Customer.external_ref, CustomerFeature.segment, TelegramUser.chat_id)
                 .join(CustomerFeature, CustomerFeature.customer_id == Customer.id)
                 .outerjoin(TelegramUser, TelegramUser.customer_id == Customer.id)
                 .where(Customer.tenant_id == biz.tenant_id, Customer.true_segment.is_not(None)))
        if q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.where(or_(func.lower(Customer.name).like(like), func.lower(Customer.external_ref).like(like)))
        else:  # the demo phone's own shopper first, then other customers linked to Telegram, then the rest
            query = query.order_by(TelegramUser.chat_id.is_(None), Customer.id)
        found = [{"id": i, "name": n, "ref": r, "segment": seg, "telegram": chat is not None,
                  "is_phone": chat is not None and chat == biz.tenant.demo_chat_id}
                 for i, n, r, seg, chat in s.execute(query.limit(12))]
        selected = None
        if customer_id:
            c = _demo_customer(s, biz, customer_id)
            f = s.get(CustomerFeature, customer_id)
            chat = s.scalar(select(TelegramUser.chat_id).where(TelegramUser.customer_id == customer_id))
            selected = {"id": c.id, "name": c.name, "ref": c.external_ref, "segment": f.segment if f else "",
                        "telegram": chat is not None, "recency": f.recency_days if f else None,
                        "birthday_in": f.days_to_birthday if f else None,
                        "points_expiring": f.points_expiring_30d if f else 0,
                        "carts_7d": f.abandoned_carts_7d if f else 0}
    return base.page(request, "demo.html", biz, "demo", found=found, q=q, selected=selected, buttons=EVENT_BUTTONS,
                     has_demo=has_demo, demo_phone=bool(biz.tenant.demo_chat_id), **labels)


@router.post("/event")
async def demo_event(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    kind = form.get("kind") or ""
    try:
        customer_id = int(form.get("customer_id") or 0)
    except ValueError:
        raise HTTPException(400, "Unknown customer") from None
    if kind not in EVENT_BUTTONS:
        raise HTTPException(400, "Unknown event")
    with session_scope() as s:
        _demo_customer(s, biz, customer_id)
    now = base._now()
    policy = biz.policy
    desc = await asyncio.to_thread(events.apply_event, biz.tenant_id, customer_id, kind, policy, now)
    result = await asyncio.to_thread(service.request_offer, biz.tenant_id, customer_id, policy, biz.tenant.name,
                                     biz.tenant.prefix, events.EVENT_TRIGGER[kind], now, biz.tenant.brand_voice,
                                     tenant_slug=biz.tenant.slug)
    bc = SimpleNamespace(tenant=biz.tenant)
    try:
        line = await with_bot(request.app.state.bots, biz.tenant.slug, biz.tenant.bot_token,
                              lambda bot: events.send_result(bc, bot, customer_id, result, desc))
    except LookupError:  # no bot connected: the offer is still made and saved as a coupon
        line = await events.send_result(bc, None, customer_id, result, desc)
    outcome = {"offer": "Offer made", "deferred": "Offer queued for quiet hours", "holdout": "Held in the control group",
               "blocked": "Stopped by a guardrail", "none": "No offer fits", "too_soon": "Too soon after the last offer",
               "opted_out": "Customer turned offers off"}.get(result.kind, result.kind)
    request.session["flash"] = {"kind": "ok" if result.kind in ("offer", "deferred") else "warn",
                                "title": f"{EVENT_BUTTONS[kind][0]}: {outcome}.", "lines": [line.split(" -> ")[-1]]}
    if result.decision_id:
        return RedirectResponse(f"/app/messages/{result.decision_id}", status_code=303)
    return RedirectResponse(f"/app/demo?customer_id={customer_id}", status_code=303)
