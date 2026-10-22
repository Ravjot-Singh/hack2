"""Operator portal (/app): what a business using the platform sees."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.dashboard import queries
from app.dashboard.routes import ASSET_VERSION, _dt, _pct, _rs
from app.data.generator import IST
from app.db.models import Customer, Deal, RulesVersion, TelegramUser
from app.db.session import session_scope
from app.portal import offers as portal_offers
from app.portal import queries as pq
from app.portal.auth import AuthError, Business, current_business, login, portal_available, register, same_origin

router = APIRouter(prefix="/app")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(rs=_rs, pct=_pct, dt=_dt)
templates.env.globals.update(asset_version=ASSET_VERSION)

# Plain-language names for decision results.
RESULT_LABELS = {"sent": "Offer sent", "holdout": "Control group", "no_offer": "Not sent",
                 "deferred": "Queued for morning"}
TRIGGER_LABELS = {"on_demand": "Customer asked", "campaign": "Campaign", "operator": "Sent by you", "deal": "Deal",
                  "event:cart_abandoned": "Left a cart", "event:points_expiring": "Points expiring",
                  "event:birthday": "Birthday"}
templates.env.globals.update(RESULT_LABELS=RESULT_LABELS, TRIGGER_LABELS=TRIGGER_LABELS)


def _now() -> datetime:
    return datetime.now(IST)


def page(request: Request, name: str, biz: Business | None, nav: str, **kw) -> HTMLResponse:
    flash = request.session.pop("flash", None) if "session" in request.scope else None
    return templates.TemplateResponse(request, name, {"biz": biz, "nav": nav, "now": _now(), "flash": flash, **kw})


def _level(x: float | None) -> str:
    x = x or 0
    return "High" if x >= 0.6 else "Medium" if x >= 0.3 else "Low"


def _ago(days: int | None, never: str = "none this year") -> str:
    if days is None:
        return never
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


templates.env.filters.update(level=_level, ago=_ago)


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/app") and not target.startswith("//") else "/app"


# ---- sign in / sign up ----

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    portal_available()
    if request.session.get("account_id"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return page(request, "login.html", None, "login", next=_safe_next(next), error=None, email="")


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/app")):
    portal_available()
    same_origin(request)
    try:
        account_id = login(email, password, request.client.host if request.client else "?", _now())
    except AuthError as exc:
        return page(request, "login.html", None, "login", next=_safe_next(next), error=str(exc), email=email)
    request.session.clear()
    request.session["account_id"] = account_id
    return RedirectResponse(_safe_next(next), status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    portal_available()
    return page(request, "register.html", None, "register", error=None, form={})


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, business_name: str = Form(""), email: str = Form(""),
                    password: str = Form(""), confirm: str = Form("")):
    portal_available()
    same_origin(request)
    try:
        account_id = register(business_name, email, password, confirm, _now())
    except AuthError as exc:
        return page(request, "register.html", None, "register", error=str(exc),
                    form={"business_name": business_name, "email": email})
    request.session.clear()
    request.session["account_id"] = account_id
    return RedirectResponse("/app?welcome=1", status_code=303)


@router.post("/logout")
def logout(request: Request):
    same_origin(request)
    request.session.clear()
    return RedirectResponse("/app/login", status_code=303)


# ---- home ----

@router.get("", response_class=HTMLResponse)
def home(request: Request, biz: Business = Depends(current_business), welcome: int = 0):
    now = _now()
    policy = biz.policy
    with session_scope() as s:
        stats = queries.overview(s, biz.tenant_id, now, policy.rules.limits.daily_budget_rs)
        feed = queries.decisions(s, biz.tenant_id, False, None, None, 12, 0)
        rules_edited = bool(s.scalar(select(func.count()).select_from(RulesVersion)
                                     .where(RulesVersion.tenant_id == biz.tenant_id)))
        recent_deals = [{"id": d.id, "name": d.name, "status": d.status, "progress": d.progress or {}}
                        for d in s.scalars(select(Deal).where(Deal.tenant_id == biz.tenant_id)
                                           .order_by(Deal.id.desc()).limit(3))]
    checklist = [
        {"done": stats["customers"] > 0, "title": "Add your customer data",
         "text": "Upload your customers, products and orders as CSV files, or start with demo data."},
        {"done": bool(biz.tenant.bot_token), "title": "Connect your Telegram bot",
         "text": "Create a bot with @BotFather and paste its token, so customers can receive offers."},
        {"done": rules_edited, "title": "Review your offer rules",
         "text": "Discount limits, budget and how often a customer may hear from you. Sensible defaults are set."},
    ]
    return page(request, "home.html", biz, "home", stats=stats, feed=feed, checklist=checklist, deals=recent_deals,
                setup_done=all(c["done"] for c in checklist), welcome=welcome, **_labels(biz))


def _labels(biz: Business) -> dict:
    p = biz.policy
    with session_scope() as s:
        deal_names = {f"deal_{i}": n for i, n in s.execute(select(Deal.id, Deal.name)
                                                           .where(Deal.tenant_id == biz.tenant_id))}
    return {"segment_labels": {x.key: x.label for x in p.segments.segments},
            "segment_info": {x.key: x for x in p.segments.segments},
            "offer_labels": {**{o.key: o.label for o in p.rules.offers}, portal_offers.CUSTOM_KEY: "Custom offer",
                             **deal_names},
            "category_labels": p.catalog.labels}


# ---- customers ----

PAGE_SIZE = 50


@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, biz: Business = Depends(current_business), q: str = "", group: str = "",
              telegram: int = 0, sort: str = "spend", page_no: int = 1):
    page_no = max(1, page_no)
    with session_scope() as s:
        rows, total = pq.customers(s, biz.tenant_id, q, group, bool(telegram), sort, PAGE_SIZE,
                                   (page_no - 1) * PAGE_SIZE)
        counts = pq.group_counts(s, biz.tenant_id)
    current = {"q": q, "group": group, "telegram": telegram, "sort": sort, "page_no": 1}

    def qs(**changes) -> str:  # link to this list with some filters changed
        params = {k: v for k, v in {**current, **changes}.items() if v}
        return "?" + urlencode(params)

    return page(request, "customers.html", biz, "customers", rows=rows, total=total, counts=counts, q=q,
                group=group, telegram=telegram, sort=sort, page_no=page_no, qs=qs,
                pages=max(1, -(-total // PAGE_SIZE)), **_labels(biz))


def _customer_or_404(s, biz: Business, customer_id: int) -> Customer:
    c = s.get(Customer, customer_id)
    if c is None or c.tenant_id != biz.tenant_id:
        raise HTTPException(404, "Customer not found")
    return c


def _customer_page(request: Request, biz: Business, customer_id: int, check=None, custom_form=None):
    now = _now()
    with session_scope() as s:
        c = _customer_or_404(s, biz, customer_id)
        cust = {k: getattr(c, k) for k in ("id", "external_ref", "name", "phone", "email", "city", "age",
                                           "signup_date", "loyalty_tier")}
        chat = s.scalar(select(TelegramUser).where(TelegramUser.customer_id == customer_id))
        tg = {"linked": chat is not None, "opted_out": bool(chat and chat.opted_out),
              "name": chat.tg_name if chat else None}
        history = pq.offer_history(s, customer_id)
    f, decision = portal_offers.preview(biz, customer_id, now)
    # Sending by hand skips the gap between offers, so tell the operator when they'd be overriding it.
    gap_h = biz.policy.rules.limits.min_hours_between_offers
    last_sent = next((h["created_at"] for h in history if h["d"].outcome in ("sent", "deferred")), None)
    recent_note = None
    if last_sent and (now - last_sent).total_seconds() < gap_h * 3600:
        hours = max(1, round((now - last_sent).total_seconds() / 3600))
        recent_note = (f"Heads-up: they got an offer {hours}h ago. Automatic offers wait {gap_h}h between offers; "
                       "sending by hand skips that wait.")
    return page(request, "customer.html", biz, "customers", c=cust, f=f, tg=tg, decision=decision, history=history,
                check=check, recent_note=recent_note, custom=custom_form or portal_offers.custom_form_defaults(decision, f),
                bot_connected=bool(biz.tenant.bot_token), **_labels(biz))


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer(request: Request, customer_id: int, biz: Business = Depends(current_business)):
    return _customer_page(request, biz, customer_id)


@router.post("/customers/{customer_id}/send")
async def customer_send(request: Request, customer_id: int, offer_key: str = Form("best"),
                        biz: Business = Depends(current_business)):
    """Send the engine's best offer ("best": the AI may choose among everything allowed) or one specific offer."""
    with session_scope() as s:
        _customer_or_404(s, biz, customer_id)
    policy = biz.policy if offer_key == "best" else portal_offers.only_offer(biz.policy, offer_key)
    if policy is None:
        raise HTTPException(400, "Unknown offer")
    result = portal_offers.send(biz, customer_id, policy, _now())
    request.session["flash"] = await portal_offers.deliver(request.app.state.bots, biz, result)
    return RedirectResponse(f"/app/customers/{customer_id}", status_code=303)


@router.post("/customers/{customer_id}/custom", response_class=HTMLResponse)
async def customer_custom(request: Request, customer_id: int, biz: Business = Depends(current_business)):
    form = dict(await request.form())
    with session_scope() as s:
        _customer_or_404(s, biz, customer_id)
    check = portal_offers.check_custom(biz, customer_id, form, _now())
    if form.get("action") != "send" or not check.ok:
        return _customer_page(request, biz, customer_id, check=check, custom_form=form)
    result = portal_offers.send(biz, customer_id, check.policy, _now())
    request.session["flash"] = await portal_offers.deliver(request.app.state.bots, biz, result)
    return RedirectResponse(f"/app/customers/{customer_id}", status_code=303)
