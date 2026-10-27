"""Portal settings: business profile, Telegram bot, categories & margins, offer rules, customer groups."""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from telegram import Bot
from telegram.error import InvalidToken, TelegramError

from app.dashboard import queries
from app.delivery.push import with_bot
from app.db.models import Customer, RulesVersion, TelegramUser, Tenant
from app.db.session import session_scope
from app.features.conditions import fmt
from app.features.customer import FEATURE_LABELS
from app.features.segments import SegmentConfig
from app.ingest import jobs
from app.portal import routes as base
from app.portal.auth import Business, current_business
from app.rules.forms import friendly_errors, rules_form_data
from app.rules.policy import adapt_rules, clear_policy_cache, default_rules, save_rules
from app.rules.schema import RulesError
from app.security import encrypt_secret, link_token, read_link_token
from app.tenants.registry import TEMPLATE_DIR, clear_tenant_cache

log = logging.getLogger(__name__)
router = APIRouter(prefix="/app/settings")
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,50}$")
PERCENT_MEASURES = {"discount_share", "return_rate", "churn_risk", "buy_anyway_prob", "deal_affinity"}


async def verify_bot_token(token: str) -> str:
    """Ask Telegram who this bot is. Returns its username; raises ValueError with a friendly message."""
    try:
        async with Bot(token) as bot:
            me = await bot.get_me()
    except InvalidToken:
        raise ValueError("Telegram doesn't recognise this token. Copy it again from @BotFather.") from None
    except TelegramError as exc:
        raise ValueError(f"Couldn't reach Telegram to check the token ({exc}). Try again in a minute.") from None
    return me.username


def _flash(request: Request, kind: str, title: str, *lines: str) -> None:
    request.session["flash"] = {"kind": kind, "title": title, "lines": list(lines)}


def _bump_config(tenant_id: int, slug: str) -> None:
    with session_scope() as s:
        s.get(Tenant, tenant_id).config_version += 1
    clear_tenant_cache()
    clear_policy_cache(slug)


async def _sync_bots(request: Request) -> None:
    manager = getattr(request.app.state, "bot_manager", None)
    if manager is not None:
        await manager.sync()


# ---- overview: profile, bot, categories ----

@router.get("", response_class=HTMLResponse)
def settings(request: Request, biz: Business = Depends(current_business)):
    with session_scope() as s:
        t = s.get(Tenant, biz.tenant_id)
        tenant = {"name": t.name, "brand_voice": t.brand_voice, "prefix": t.coupon_prefix,
                  "bot_username": t.bot_username, "has_own_token": bool(t.bot_token_enc),
                  "demo_chat_id": t.demo_chat_id}
        catalog = t.catalog or {}
        chats = [{"key": _chat_key(biz, u.chat_id), "name": u.tg_name, "last_seen": u.last_seen_at,
                  "is_demo": u.chat_id == t.demo_chat_id, "shopper": shopper}
                 for u, shopper in s.execute(
                     select(TelegramUser, Customer.name).outerjoin(Customer, Customer.id == TelegramUser.customer_id)
                     .where(TelegramUser.tenant_id == biz.tenant_id)
                     .order_by(TelegramUser.last_seen_at.desc()).limit(20))]
    caps = biz.policy.rules.limits.max_discount_pct
    return base.page(request, "settings.html", biz, "settings", tab="general", tenant=tenant, catalog=catalog,
                     caps=caps, env_bot=bool(biz.tenant.bot_token) and not tenant["has_own_token"], chats=chats)


def _chat_key(biz: Business, chat_id: int) -> str:
    """The chat id is not put in the page as-is: a signed reference only this business's session can use."""
    return link_token(-biz.tenant_id, chat_id)  # separate namespace from customer invite links


@router.post("/demo-phone")
async def set_demo_phone(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    raw = form.get("chat") or ""
    with session_scope() as s:
        t = s.get(Tenant, biz.tenant_id)
        if raw == "none":
            t.demo_chat_id = None
            _flash(request, "ok", "Demo phone turned off.", "Offers to demo customers are no longer copied.")
        else:
            chat_id = read_link_token(-biz.tenant_id, raw)
            known = chat_id is not None and s.scalar(select(TelegramUser.id).where(
                TelegramUser.tenant_id == biz.tenant_id, TelegramUser.chat_id == chat_id)) is not None
            if not known:
                _flash(request, "err", "That chat isn't known to your bot.")
            else:
                t.demo_chat_id = chat_id
                _flash(request, "ok", "Demo phone set.",
                       "Every offer to a demo customer is now copied to that chat, headed with who it was for.")
    clear_tenant_cache()
    return RedirectResponse("/app/settings#demo-phone", status_code=303)


DEMO_PHONE_HELLO = ("📨 <b>Demo phone connected</b> for {store}.\nFrom now on you'll get a copy of every offer a demo "
                    "customer receives, headed with who it was for.")


@router.post("/demo-phone-id")
async def set_demo_phone_by_id(request: Request, biz: Business = Depends(current_business)):
    """Owner types their own Telegram user ID: the bot sends it a confirmation, which only works if that person has
    pressed Start in this business's bot. Only then does it become the demo phone."""
    form = await request.form()
    raw = (form.get("telegram_id") or "").strip()
    if not re.fullmatch(r"\d{5,15}", raw):
        _flash(request, "err", "That doesn't look like a Telegram ID.",
               "It's a number like 123456789. In Telegram, message @userinfobot and it replies with your ID.")
        return RedirectResponse("/app/settings#demo-phone", status_code=303)
    chat_id = int(raw)

    async def hello(bot):
        await bot.send_message(chat_id, DEMO_PHONE_HELLO.format(store=escape(biz.tenant.name)), parse_mode="HTML")
    try:
        await with_bot(request.app.state.bots, biz.tenant.slug, biz.tenant.bot_token, hello)
    except LookupError:
        _flash(request, "err", "Connect your Telegram bot first.", "The demo phone receives messages from your bot.")
        return RedirectResponse("/app/settings#bot", status_code=303)
    except TelegramError as exc:
        who = f"t.me/{biz.tenant.bot_username}" if biz.tenant.bot_username else "your bot"
        _flash(request, "err", "Your bot can't message that ID yet.",
               f"Open {who} on that phone, press Start, then try again. (Telegram said: {exc})")
        return RedirectResponse("/app/settings#demo-phone", status_code=303)
    with session_scope() as s:
        s.get(Tenant, biz.tenant_id).demo_chat_id = chat_id
    clear_tenant_cache()
    _flash(request, "ok", "Demo phone set.", "A confirmation message was sent to that Telegram account.")
    return RedirectResponse("/app/settings#demo-phone", status_code=303)


@router.post("/profile")
async def save_profile(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    name, voice = (form.get("name") or "").strip(), (form.get("brand_voice") or "").strip()
    if not 2 <= len(name) <= 120:
        _flash(request, "err", "Please enter your business name.")
    else:
        with session_scope() as s:
            t = s.get(Tenant, biz.tenant_id)
            t.name, t.brand_voice = name, (voice or t.brand_voice)[:200]
        clear_tenant_cache()
        _flash(request, "ok", "Saved your business profile.")
    return RedirectResponse("/app/settings", status_code=303)


@router.post("/bot")
async def connect_bot(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    token = (form.get("token") or "").strip()
    if not TOKEN_RE.match(token):
        _flash(request, "err", "That doesn't look like a bot token.",
               "In Telegram, open @BotFather, send /newbot (or /token for an existing bot) and copy the token "
               "it gives you, which looks like 123456789:AAH...")
        return RedirectResponse("/app/settings#bot", status_code=303)
    try:
        username = await verify_bot_token(token)
    except ValueError as exc:
        _flash(request, "err", "Couldn't connect the bot.", str(exc))
        return RedirectResponse("/app/settings#bot", status_code=303)
    with session_scope() as s:
        other = s.scalar(select(Tenant.name).where(Tenant.bot_username == username, Tenant.id != biz.tenant_id))
        if other is None:
            t = s.get(Tenant, biz.tenant_id)
            t.bot_token_enc, t.bot_username = encrypt_secret(token), username
    if other is not None:
        _flash(request, "err", f"@{username} is already connected to another business.",
               "Each business needs its own bot. Create a new one with @BotFather.")
        return RedirectResponse("/app/settings#bot", status_code=303)
    clear_tenant_cache()
    await _sync_bots(request)
    _flash(request, "ok", f"Connected @{username}.",
           f"Share t.me/{username} with your customers. Offers reach them once they press Start.")
    return RedirectResponse("/app/settings#bot", status_code=303)


@router.post("/bot/disconnect")
async def disconnect_bot(request: Request, biz: Business = Depends(current_business)):
    with session_scope() as s:
        t = s.get(Tenant, biz.tenant_id)
        t.bot_token_enc, t.bot_username = None, None
    clear_tenant_cache()
    await _sync_bots(request)
    _flash(request, "ok", "Bot disconnected.", "Offers are still recorded, as coupons you can share yourself.")
    return RedirectResponse("/app/settings#bot", status_code=303)


@router.post("/categories")
async def save_categories(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    with session_scope() as s:
        catalog = dict(s.get(Tenant, biz.tenant_id).catalog or {})
    new = {}
    try:
        for key, info in catalog.items():
            label = (form.get(f"label.{key}") or info["label"]).strip()[:40]
            margin = float(form.get(f"margin.{key}") or info["margin_pct"])
            if not 1 <= margin <= 95:
                raise ValueError(f"The margin for {label} should be between 1% and 95%.")
            new[key] = {"label": label, "margin_pct": margin}
    except ValueError as exc:
        _flash(request, "err", "Not saved.", str(exc) if "margin" in str(exc) else "Please enter numbers only.")
        return RedirectResponse("/app/settings#categories", status_code=303)
    old_caps = dict(biz.policy.rules.limits.max_discount_pct)
    with session_scope() as s:
        s.get(Tenant, biz.tenant_id).catalog = new
        latest = s.execute(select(RulesVersion.content).where(RulesVersion.tenant_id == biz.tenant_id)
                           .order_by(RulesVersion.id.desc()).limit(1)).scalar()
    _bump_config(biz.tenant_id, biz.tenant.slug)
    lines = []
    if latest is not None:  # saved rules: keep every discount cap below its (new) margin
        adapted = adapt_rules(yaml.safe_load(latest), new)
        if adapted != yaml.safe_load(latest):
            save_rules(biz.tenant.slug, biz.tenant_id, biz.tenant.path, adapted, biz.email,
                       "Discount caps lowered to stay below new margins", base._now())
    new_caps = biz.policy.rules.limits.max_discount_pct
    for key, cap in old_caps.items():
        if key in new_caps and new_caps[key] < cap:
            lines.append(f"Discount limit for {new.get(key, {}).get('label', 'the whole store')} lowered from "
                         f"{cap:g}% to {new_caps[key]:g}% to stay below its margin.")
    _flash(request, "ok", "Saved categories and margins.", *lines)
    return RedirectResponse("/app/settings#categories", status_code=303)


# ---- offer rules ----

def _rules_page(request: Request, biz: Business, data: dict, errors: list[str]):
    with session_scope() as s:
        history = queries.rules_history(s, biz.tenant_id)
    return base.page(request, "settings_rules.html", biz, "settings", tab="rules", data=data, errors=errors,
                     history=history, margins=biz.policy.catalog.margins, **base._labels(biz))


@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, biz: Business = Depends(current_business)):
    return _rules_page(request, biz, biz.policy.rules.model_dump(mode="json"), [])


@router.post("/rules", response_class=HTMLResponse)
async def rules_save(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    data = biz.policy.rules.model_dump(mode="json")
    try:
        data = rules_form_data(form, data)
        save_rules(biz.tenant.slug, biz.tenant_id, biz.tenant.path, data, biz.email, form.get("note"), base._now())
    except (ValidationError, RulesError, ValueError) as exc:
        return _rules_page(request, biz, data, friendly_errors(exc))
    _flash(request, "ok", "Saved your offer rules.", "They apply to every offer from now on.")
    return RedirectResponse("/app/settings/rules", status_code=303)


@router.post("/rules/reset")
async def rules_reset(request: Request, biz: Business = Depends(current_business)):
    catalog = {k: {"label": biz.policy.catalog.labels[k], "margin_pct": m}
               for k, m in biz.policy.catalog.margins.items()}
    if biz.tenant.path and (biz.tenant.path / "rules.yaml").exists():
        data = adapt_rules(yaml.safe_load((biz.tenant.path / "rules.yaml").read_text(encoding="utf-8")), catalog)
    else:
        data = default_rules(catalog)
    save_rules(biz.tenant.slug, biz.tenant_id, biz.tenant.path, data, biz.email, "Reset to the default rules",
               base._now())
    _flash(request, "ok", "Offer rules reset to the defaults.")
    return RedirectResponse("/app/settings/rules", status_code=303)


# ---- customer groups ----

def _groups_page(request: Request, biz: Business, groups: list[dict], errors: list[str]):
    with session_scope() as s:
        counts = dict(queries.segment_stats(s, biz.tenant_id))
    return base.page(request, "settings_groups.html", biz, "settings", tab="groups", groups=groups, errors=errors,
                     counts={k: v["n"] for k, v in counts.items()}, measure_labels=FEATURE_LABELS,
                     percent_measures=PERCENT_MEASURES)


def _groups_from_policy(biz: Business) -> list[dict]:
    return [s.model_dump() for s in biz.policy.segments.segments]


@router.get("/groups", response_class=HTMLResponse)
def groups(request: Request, biz: Business = Depends(current_business)):
    return _groups_page(request, biz, _groups_from_policy(biz), [])


async def _start_regroup(biz: Business) -> bool:
    """Re-assign customers in the background (must be called from the event loop)."""
    if not await asyncio.to_thread(_has_customers, biz):
        return False
    job_id = await asyncio.to_thread(jobs.create_job, biz.tenant_id, "regroup", base._now())
    if job_id is None:
        return False
    jobs.start(jobs.run_regroup, job_id, biz.tenant_id, biz.tenant.slug, base._now)
    return True


def _save_groups(biz: Business, cfg: SegmentConfig) -> None:
    with session_scope() as s:
        s.get(Tenant, biz.tenant_id).segments_yaml = yaml.safe_dump({"segments": [g.model_dump() for g in cfg.segments]},
                                                                     sort_keys=False, allow_unicode=True)
    _bump_config(biz.tenant_id, biz.tenant.slug)


@router.post("/groups", response_class=HTMLResponse)
async def groups_save(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    groups = _groups_from_policy(biz)
    errors = []
    for g in groups:
        k = g["key"]
        g["label"] = (form.get(f"g.{k}.label") or g["label"]).strip()[:40]
        g["description"] = (form.get(f"g.{k}.description") or g["description"]).strip()[:300]
        for cond in list(g["when"]):
            raw = form.get(f"g.{k}.when.{cond}")
            if raw in (None, ""):
                continue
            try:
                value = float(raw)
            except ValueError:
                errors.append(f"{g['label']}: '{raw}' is not a number.")
                continue
            measure = cond.rpartition("_")[0]
            g["when"][cond] = value / 100 if measure in PERCENT_MEASURES else value
    if not errors:
        try:
            cfg = SegmentConfig(segments=groups)
        except ValidationError as exc:
            errors = [e["msg"] for e in exc.errors()]
    if errors:
        return _groups_page(request, biz, groups, errors)
    await asyncio.to_thread(_save_groups, biz, cfg)
    started = await _start_regroup(biz)
    _flash(request, "ok", "Saved customer groups.",
           "Re-assigning your customers now (see Data for progress)." if started else
           "Customers will be grouped this way from now on.")
    return RedirectResponse("/app/settings/groups", status_code=303)


@router.post("/groups/reset")
async def groups_reset(request: Request, biz: Business = Depends(current_business)):
    if biz.tenant.path and (biz.tenant.path / "segments.yaml").exists():
        text = (biz.tenant.path / "segments.yaml").read_text(encoding="utf-8")
    else:
        text = (TEMPLATE_DIR / "segments.yaml").read_text(encoding="utf-8")
    await asyncio.to_thread(_save_groups, biz, SegmentConfig(**yaml.safe_load(text)))
    await _start_regroup(biz)
    _flash(request, "ok", "Customer groups reset to the defaults.")
    return RedirectResponse("/app/settings/groups", status_code=303)


def _has_customers(biz: Business) -> bool:
    with session_scope() as s:
        return s.scalar(select(Customer.id).where(Customer.tenant_id == biz.tenant_id).limit(1)) is not None


def condition_text(cond: str) -> tuple[str, str]:
    """('orders in last 12 months', 'at least') for 'orders_365_min'."""
    measure, _, bound = cond.rpartition("_")
    return FEATURE_LABELS.get(measure, measure), "at least" if bound == "min" else "at most"


base.templates.env.globals.update(condition_text=condition_text, fmt_measure=fmt)
