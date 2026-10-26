"""Portal "Guardrails" page: the pause switch, today's hard limits, and every offer the safety gate stopped."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app import guardrails
from app.dashboard import queries
from app.delivery.push import with_bot
from app.triggers import automation
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
        t = s.get(Tenant, biz.tenant_id)
        auto = {"cfg": automation.settings_of(t.automation), "state": t.automation_state or {},
                "triggers": {k: v[1] for k, v in automation.TRIGGERS.items()}, "intervals": automation.INTERVALS,
                "has_bot": bool(biz.tenant.bot_token)}
    by_code = Counter(v["code"] for vs in today for v in (vs or []))
    return base.page(request, "guardrails.html", biz, "guardrails", paused=state.paused, auto=auto,
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


@router.post("/automation")
async def save_automation(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    try:
        every = int(form.get("every_min") or 10)
    except ValueError:
        every = 10
    cfg = automation.settings_of({"enabled": {k: form.get(f"auto.{k}") == "on" for k in automation.TRIGGERS},
                                  "every_min": every})
    with session_scope() as s:
        s.get(Tenant, biz.tenant_id).automation = cfg
    on = [automation.TRIGGERS[k][1] for k, v in cfg["enabled"].items() if v]
    request.session["flash"] = {"kind": "ok", "title": "Automation saved.", "lines": [
        f"Running every {cfg['every_min']} minutes: {', '.join(on)}." if on else "All automatic triggers are off."]}
    return RedirectResponse("/app/guardrails#automation", status_code=303)


@router.post("/automation/run")
async def run_automation_now(request: Request, biz: Business = Depends(current_business)):
    async def run(bot):
        return await automation.run_automation(biz.tenant_id, bot, base._now(), force=True)
    try:
        summary = await with_bot(request.app.state.bots, biz.tenant.slug, biz.tenant.bot_token, run)
    except LookupError:  # no bot: offers are still made, as coupons
        summary = await automation.run_automation(biz.tenant_id, None, base._now(), force=True)
    if summary is None:
        flash = {"kind": "err", "title": "Nothing ran.",
                 "lines": ["Switch on at least one trigger first (or a run is already in progress)."]}
    else:
        results = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in summary["results"].items()) or "nobody was due"
        flash = {"kind": "ok", "title": f"Automation ran for {summary['picked']} customer(s).",
                 "lines": [f"Results: {results}. Each one is on the Messages page."]}
    request.session["flash"] = flash
    return RedirectResponse("/app/guardrails#automation", status_code=303)
