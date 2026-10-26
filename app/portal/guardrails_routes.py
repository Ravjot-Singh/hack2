"""Portal "Guardrails" page: the pause switch, today's hard limits, and every offer the safety gate stopped."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app import guardrails
from app.dashboard import queries
from app.db.models import Deal, OfferDecision, Tenant
from app.db.session import session_scope
from app.portal import routes as base
from app.portal.auth import Business, current_business

router = APIRouter(prefix="/app/guardrails")
CODE_LABELS = {"money": "Money limits", "contact": "Contact limits", "pause": "Paused", "fairness": "Fairness"}


@router.get("", response_class=HTMLResponse)
def guardrails_page(request: Request, biz: Business = Depends(current_business)):
    now = base._now()
    lim = biz.policy.rules.limits
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    with session_scope() as s:
        state = guardrails.business_state(s, biz.tenant_id, now)
        today = s.scalars(select(OfferDecision.guardrail).where(
            OfferDecision.tenant_id == biz.tenant_id, OfferDecision.outcome == "blocked",
            OfferDecision.simulated.is_(False), OfferDecision.created_at >= midnight)).all()
        feed = queries.decisions(s, biz.tenant_id, False, "blocked", None, 25, 0)
        fairness = [{"id": d.id, "name": d.name, "fairness": (d.progress or {}).get("fairness")}
                    for d in s.scalars(select(Deal).where(Deal.tenant_id == biz.tenant_id, Deal.status != "draft")
                                       .order_by(Deal.id.desc()).limit(5))
                    if (d.progress or {}).get("fairness")]
    by_code = Counter(v["code"] for vs in today for v in (vs or []))
    return base.page(request, "guardrails.html", biz, "guardrails", paused=state.paused,
                     spend=state.spend_today, ceiling=lim.total_spend_ceiling_rs, daily_budget=lim.daily_budget_rs,
                     per_day=lim.max_offers_per_customer_per_day, max_per_offer=lim.max_discount_rs_per_offer,
                     blocked_today=len(today), by_code=[(CODE_LABELS.get(k, k), n) for k, n in by_code.most_common()],
                     feed=feed, fairness=fairness, min_ratio=biz.policy.rules.fairness.min_ratio, **base._labels(biz))


@router.post("/pause")
async def set_pause(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    paused = form.get("paused") == "on"
    with session_scope() as s:
        s.get(Tenant, biz.tenant_id).paused = paused
    request.session["flash"] = (
        {"kind": "warn", "title": "Automatic offers paused.",
         "lines": ["Campaigns, deals and event offers are stopped. Offers you send by hand, and offers customers "
                   "ask for, still work."]} if paused else
        {"kind": "ok", "title": "Automatic offers resumed.", "lines": []})
    return RedirectResponse("/app/guardrails", status_code=303)
