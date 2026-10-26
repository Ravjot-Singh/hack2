"""Admin dashboard for the marketing team: overview, results, segments, decisions, customers, rules, actions."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, select
from telegram import Bot

from app.bot.handlers import BotContext
from app.dashboard import queries
from app.dashboard.auth import require_admin
from app.data.generator import IST
from app.db.models import Customer, OfferDecision, OfferOutcome, Tenant
from app.db.session import get_engine, session_scope
from app.measurement.holdout import in_holdout
from app.measurement.metrics import measure
from app.offers import service
from app.offers.campaign import run_simulated_campaign
from app.rules.engine import CustomerContext, evaluate
from app.rules.forms import friendly_errors, rules_form_data
from app.rules.policy import TenantPolicy, adapt_rules, current_policy, default_rules, save_rules
from app.rules.schema import RulesError
from app.tenants.registry import TenantConfig, get_tenants
from app.triggers.events import EVENTS, fire_event, run_live_campaign

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

OUTCOME_LABELS = {"sent": "Sent", "holdout": "Holdout", "no_offer": "No offer", "deferred": "Queued (quiet hours)"}


def _rs(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"−₹{-v:,.0f}" if round(v) < 0 else f"₹{v:,.0f}"


def _pct(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"{v * 100:.{digits}f}%"


def _dt(v) -> str:
    if not v:
        return "–"
    if v.tzinfo is None:
        v = v.replace(tzinfo=IST)
    return v.astimezone(IST).strftime("%d %b %H:%M")


STATIC_DIR = Path(__file__).parent / "static"
# Cache-busting stamp for /admin/static links, so a deploy never serves stale CSS/JS.
ASSET_VERSION = str(int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir())))

templates.env.filters.update(rs=_rs, pct=_pct, dt=_dt)
templates.env.globals.update(OUTCOME_LABELS=OUTCOME_LABELS, asset_version=ASSET_VERSION)


@dataclass
class Ctx:
    tenant: TenantConfig
    tenant_id: int
    policy: TenantPolicy
    tenants: list[TenantConfig]

    @property
    def offer_labels(self) -> dict[str, str]:
        return {o.key: o.label for o in self.policy.rules.offers}

    @property
    def segment_labels(self) -> dict[str, str]:
        return {s.key: s.label for s in self.policy.segments.segments}


def get_ctx(t: str | None = None) -> Ctx:
    tenants = get_tenants()
    if not tenants:
        raise HTTPException(404, "No tenants configured")
    cfg = tenants.get(t or "") or next(iter(tenants.values()))
    with session_scope() as s:
        tenant_id = s.scalar(select(Tenant.id).where(Tenant.slug == cfg.slug))
    if tenant_id is None:
        raise HTTPException(503, f"Tenant {cfg.slug} is not in the database yet; restart the app.")
    return Ctx(cfg, tenant_id, current_policy(cfg.slug, cfg.path), list(tenants.values()))


def render(request: Request, name: str, ctx: Ctx, page: str, **kw) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"ctx": ctx, "page": page, "now": datetime.now(IST), **kw})


def _now() -> datetime:
    return datetime.now(IST)


@router.get("", response_class=HTMLResponse)
def overview(request: Request, ctx: Ctx = Depends(get_ctx)):
    now = _now()
    with session_scope() as s:
        stats = queries.overview(s, ctx.tenant_id, now, ctx.policy.rules.limits.daily_budget_rs)
        sim_days, sim_series = queries.daily_outcomes(s, ctx.tenant_id, simulated=True)
        live_days, live_series = queries.daily_outcomes(s, ctx.tenant_id, simulated=False)
        segs = queries.segment_stats(s, ctx.tenant_id)
        latest = queries.decisions(s, ctx.tenant_id, False, None, None, 8, 0)
        llm = queries.llm_recent(s, ctx.tenant_id, now)
    return render(request, "overview.html", ctx, "overview", stats=stats, segs=segs, latest=latest, llm=llm,
                  sim_chart={"labels": sim_days, "series": sim_series},
                  live_chart={"labels": live_days, "series": live_series})


@router.get("/results", response_class=HTMLResponse)
def results(request: Request, ctx: Ctx = Depends(get_ctx)):
    with get_engine().connect() as conn:
        report = measure(conn, ctx.tenant_id)
    return render(request, "results.html", ctx, "results", r=report)


@router.get("/segments", response_class=HTMLResponse)
def segments(request: Request, ctx: Ctx = Depends(get_ctx), seg: str | None = None):
    with session_scope() as s:
        stats = queries.segment_stats(s, ctx.tenant_id)
        members = queries.segment_customers(s, ctx.tenant_id, seg) if seg else []
    return render(request, "segments.html", ctx, "segments", stats=stats, seg=seg, members=members)


@router.get("/decisions", response_class=HTMLResponse)
def decisions(request: Request, ctx: Ctx = Depends(get_ctx), mode: str = "live", outcome: str = "",
              offer: str = "", page: int = 1):
    simulated = {"live": False, "sim": True}.get(mode)
    page = max(1, page)
    with session_scope() as s:
        rows = queries.decisions(s, ctx.tenant_id, simulated, outcome or None, offer or None, 50, (page - 1) * 50)
    return render(request, "decisions.html", ctx, "decisions", rows=rows, mode=mode, outcome=outcome, offer=offer,
                  pg=page)


@router.get("/customer", response_class=HTMLResponse)
def customer_search(q: str, ctx: Ctx = Depends(get_ctx)):
    with session_scope() as s:
        cid = queries.find_customer(s, ctx.tenant_id, q)
    if cid is None:
        raise HTTPException(404, f"No customer '{q}'")
    return RedirectResponse(f"/admin/customer/{cid}?t={ctx.tenant.slug}", status_code=303)


@router.get("/customer/{customer_id}", response_class=HTMLResponse)
def customer(request: Request, customer_id: int, ctx: Ctx = Depends(get_ctx)):
    now = _now()
    with session_scope() as s:
        c = s.get(Customer, customer_id)
        if c is None or c.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "Customer not found")
        f = service._features(s, ctx.tenant_id, customer_id, ctx.policy, now)
        recent = service._recent_offers(s, customer_id, now, max(7, ctx.policy.rules.limits.same_offer_cooldown_days))
        budget_used = service.budget_used_today(s, ctx.tenant_id, now)
        history = queries.customer_history(s, customer_id)
        chat = service.chat_for_customer(customer_id)
        cust = {k: getattr(c, k) for k in ("id", "external_ref", "name", "gender", "age", "city", "city_tier",
                                           "signup_date", "loyalty_tier", "birthday")}
    decision = evaluate(ctx.policy.rules, ctx.policy.catalog, CustomerContext(customer_id, f, f["segment"], False,
                                                                              recent), budget_used, now)
    seg = next(x for x in ctx.policy.segments.segments if x.key == f["segment"])
    return render(request, "customer.html", ctx, "segments", c=cust, f=f, seg=seg, decision=decision,
                  history=history, chat=chat,
                  holdout=in_holdout(ctx.tenant.slug, customer_id, ctx.policy.rules.measurement.holdout_pct))


# ---- rules ----

@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, ctx: Ctx = Depends(get_ctx), saved: int = 0):
    with session_scope() as s:
        history = queries.rules_history(s, ctx.tenant_id)
    return render(request, "rules.html", ctx, "rules", data=ctx.policy.rules.model_dump(mode="json"),
                  errors=[], saved=saved, history=history)


@router.post("/rules", response_class=HTMLResponse)
async def rules_save(request: Request, ctx: Ctx = Depends(get_ctx), user: str = Depends(require_admin)):
    form = await request.form()
    data = ctx.policy.rules.model_dump(mode="json")
    try:
        data = rules_form_data(form, data)
        save_rules(ctx.tenant.slug, ctx.tenant_id, ctx.tenant.path, data, user, form.get("note"), _now())
    except (ValidationError, RulesError, ValueError) as exc:
        with session_scope() as s:
            history = queries.rules_history(s, ctx.tenant_id)
        return render(request, "rules.html", ctx, "rules", data=data, errors=friendly_errors(exc), saved=0,
                      history=history)
    return RedirectResponse(f"/admin/rules?t={ctx.tenant.slug}&saved=1", status_code=303)


@router.post("/rules/reset")
def rules_reset(ctx: Ctx = Depends(get_ctx), user: str = Depends(require_admin)):
    catalog = {k: {"label": ctx.policy.catalog.labels[k], "margin_pct": m} for k, m in ctx.policy.catalog.margins.items()}
    if ctx.tenant.path and (ctx.tenant.path / "rules.yaml").exists():
        data = adapt_rules(yaml.safe_load((ctx.tenant.path / "rules.yaml").read_text(encoding="utf-8")), catalog)
        note = "Reset to rules.yaml"
    else:
        data, note = default_rules(catalog), "Reset to default rules"
    save_rules(ctx.tenant.slug, ctx.tenant_id, ctx.tenant.path, data, user, note, _now())
    return RedirectResponse(f"/admin/rules?t={ctx.tenant.slug}&saved=1", status_code=303)


@router.get("/rules.yaml", response_class=PlainTextResponse)
def rules_yaml(ctx: Ctx = Depends(get_ctx)):
    return yaml.safe_dump(ctx.policy.rules.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


# ---- actions ----

@router.get("/actions", response_class=HTMLResponse)
def actions(request: Request, ctx: Ctx = Depends(get_ctx)):
    with session_scope() as s:
        chats = queries.linked_chats(s, ctx.tenant_id)
    return render(request, "actions.html", ctx, "actions", chats=chats, events=EVENTS, result=None)


def _actions_result(request: Request, ctx: Ctx, title: str, lines: list[str]) -> HTMLResponse:
    with session_scope() as s:
        chats = queries.linked_chats(s, ctx.tenant_id)
    return render(request, "actions.html", ctx, "actions", chats=chats, events=EVENTS,
                  result={"title": title, "lines": lines})


@router.post("/actions/simulate", response_class=HTMLResponse)
async def action_simulate(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    days = max(1, min(7, int(form.get("days") or 1)))
    budget = int(form["budget"]) if form.get("budget") else None
    lines = await asyncio.to_thread(_simulate, ctx, days, budget, form.get("reset") == "on")
    return _actions_result(request, ctx, "Simulated campaign", lines)


def _simulate(ctx: Ctx, days: int, budget: int | None, reset: bool) -> list[str]:
    lines = []
    with session_scope() as s:
        if reset:
            sim = select(OfferDecision.id).where(OfferDecision.tenant_id == ctx.tenant_id,
                                                 OfferDecision.simulated.is_(True))
            s.execute(delete(OfferOutcome).where(OfferOutcome.decision_id.in_(sim)))
            s.execute(delete(OfferDecision).where(OfferDecision.tenant_id == ctx.tenant_id,
                                                  OfferDecision.simulated.is_(True)))
            lines.append("Deleted earlier simulated decisions.")
    start = _now().date()
    for i in range(days):
        now = datetime.combine(start + timedelta(days=i), dtime(11), IST)
        t0 = time.time()
        with session_scope() as s:
            r = run_simulated_campaign(s, ctx.tenant.slug, ctx.tenant_id, ctx.policy, ctx.tenant.name,
                                       ctx.tenant.prefix, now, seed=i, daily_budget=budget)
        top = ", ".join(f"{k} {v}" for k, v in r.by_offer.most_common(3))
        lines.append(f"{now:%a %d %b}: {r.eligible:,} eligible, {r.sent:,} sent (worst-case ₹{r.liability:,}), "
                     f"{r.holdout} holdout, budget ran out for {r.budget_stopped:,} ({time.time() - t0:.0f}s). {top}")
    return lines


async def _with_bot(request: Request, ctx: Ctx, fn):
    running = getattr(request.app.state, "bots", {}).get(ctx.tenant.slug)
    if running is not None:
        return await fn(running.bot)
    if not ctx.tenant.bot_token:
        raise HTTPException(400, "This tenant has no bot token configured.")
    async with Bot(ctx.tenant.bot_token) as bot:
        return await fn(bot)


@router.post("/actions/live-campaign", response_class=HTMLResponse)
async def action_live_campaign(request: Request, ctx: Ctx = Depends(get_ctx)):
    bc = BotContext(ctx.tenant, ctx.tenant_id)
    lines = await _with_bot(request, ctx, lambda bot: run_live_campaign(bc, bot, _now()))
    return _actions_result(request, ctx, "Live campaign to linked Telegram users", lines or ["No linked users."])


@router.post("/actions/event", response_class=HTMLResponse)
async def action_event(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    kind = form.get("kind")
    if kind not in EVENTS:
        raise HTTPException(400, "Unknown event")
    customer_id = int(form.get("customer_id") or 0)
    with session_scope() as s:
        c = s.get(Customer, customer_id)
        if c is None or c.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "Customer not found")
    bc = BotContext(ctx.tenant, ctx.tenant_id)
    line = await _with_bot(request, ctx, lambda bot: fire_event(bc, bot, customer_id, kind, _now()))
    return _actions_result(request, ctx, f"Event: {kind.replace('_', ' ')}", [line])
