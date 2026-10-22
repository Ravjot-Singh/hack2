"""Operator portal (/app): what a business using the platform sees."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.dashboard import queries
from app.dashboard.routes import ASSET_VERSION, _dt, _pct, _rs
from app.data.generator import IST
from app.db.models import RulesVersion
from app.db.session import session_scope
from app.portal.auth import AuthError, Business, current_business, login, portal_available, register, same_origin

router = APIRouter(prefix="/app")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(rs=_rs, pct=_pct, dt=_dt)
templates.env.globals.update(asset_version=ASSET_VERSION)

# Plain-language names for decision results.
RESULT_LABELS = {"sent": "Offer sent", "holdout": "Control group", "no_offer": "Not sent",
                 "deferred": "Queued for morning"}
TRIGGER_LABELS = {"on_demand": "Customer asked", "campaign": "Campaign", "operator": "Sent by you",
                  "event:cart_abandoned": "Left a cart", "event:points_expiring": "Points expiring",
                  "event:birthday": "Birthday"}
templates.env.globals.update(RESULT_LABELS=RESULT_LABELS, TRIGGER_LABELS=TRIGGER_LABELS)


def _now() -> datetime:
    return datetime.now(IST)


def page(request: Request, name: str, biz: Business | None, nav: str, **kw) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"biz": biz, "nav": nav, "now": _now(), **kw})


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
    checklist = [
        {"done": stats["customers"] > 0, "title": "Add your customer data",
         "text": "Upload your customers, products and orders as CSV files, or start with demo data."},
        {"done": bool(biz.tenant.bot_token), "title": "Connect your Telegram bot",
         "text": "Create a bot with @BotFather and paste its token, so customers can receive offers."},
        {"done": rules_edited, "title": "Review your offer rules",
         "text": "Discount limits, budget and how often a customer may hear from you. Sensible defaults are set."},
    ]
    return page(request, "home.html", biz, "home", stats=stats, feed=feed, checklist=checklist,
                setup_done=all(c["done"] for c in checklist), welcome=welcome,
                segment_labels={x.key: x.label for x in policy.segments.segments},
                offer_labels={o.key: o.label for o in policy.rules.offers})
