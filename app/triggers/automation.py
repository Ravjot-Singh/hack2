"""Automatic offers: every few minutes, find customers who have a reason to hear from the business right now
and run the normal pipeline for them (rules, control group, guardrails, quiet hours, budget, learning,
preferences). Delivery: Telegram if they're linked, else a saved coupon; demo customers are copied to the demo
phone (first 10 per run, then a summary).

Triggers (each switched on separately in the portal; all off by default):
  cart       an abandoned cart 30 min – 24 h old, with no cart reminder since
  birthday   birthday within 7 days, no birthday offer in the last 30 days
  points     50+ points expiring within 30 days, no points offer in the last 14 days
  win_back   at-risk / dormant customers, no offer of any kind in the last 7 days

At most MAX_PER_RUN offers per run. Runs in the process that runs the business's bot (one per business, so a
customer is never picked twice), and on demand from the portal ("Run now").
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, exists, select
from telegram import Bot

from app.db.models import Cart, CustomerFeature, OfferDecision, Tenant
from app.db.session import get_engine, session_scope
from app.delivery.push import MIRROR_BATCH_LIMIT, Mirror
from app.features.customer import _aware
from app.features.store import refresh_features
from app.offers import service
from app.rules.policy import current_policy

log = logging.getLogger(__name__)
MAX_PER_RUN = 50
TRIGGERS = {  # key -> (pipeline trigger, label for people)
    "cart": ("event:cart_abandoned", "Abandoned cart"),
    "birthday": ("event:birthday", "Birthday week"),
    "points": ("event:points_expiring", "Points expiring"),
    "win_back": ("event:win_back", "Win-back"),
}
INTERVALS = (5, 10, 15, 30, 60)
DEFAULT = {"enabled": {k: False for k in TRIGGERS}, "every_min": 10}


def settings_of(raw: dict | None) -> dict:
    cfg = {"enabled": dict(DEFAULT["enabled"]), "every_min": DEFAULT["every_min"]}
    if raw:
        cfg["enabled"].update({k: bool(v) for k, v in (raw.get("enabled") or {}).items() if k in TRIGGERS})
        if raw.get("every_min") in INTERVALS:
            cfg["every_min"] = raw["every_min"]
    return cfg


def _no_recent(trigger: str | None, since: datetime):
    """SQL: the customer has no decision (of this trigger, or any if None) created after `since`."""
    cond = [OfferDecision.customer_id == CustomerFeature.customer_id, OfferDecision.created_at > since,
            OfferDecision.simulated.is_(False)]
    if trigger:
        cond.append(OfferDecision.trigger == trigger)
    return ~exists().where(and_(*cond))


def due_customers(tenant_id: int, enabled: dict, now: datetime, limit: int | None = None) -> list[tuple[int, str]]:
    """[(customer_id, trigger key)] in priority order (most time-sensitive first), each customer once."""
    limit = MAX_PER_RUN if limit is None else limit
    picked: list[tuple[int, str]] = []
    seen: set[int] = set()

    def add(rows, key):
        for (cid,) in rows:
            if cid not in seen and len(picked) < limit:
                seen.add(cid)
                picked.append((cid, key))

    with session_scope() as s:
        base = select(CustomerFeature.customer_id).where(CustomerFeature.tenant_id == tenant_id)
        if enabled.get("cart"):
            carts = s.execute(select(Cart.customer_id, Cart.created_at).where(
                Cart.tenant_id == tenant_id, Cart.status == "abandoned",
                Cart.created_at <= now - timedelta(minutes=30), Cart.created_at >= now - timedelta(hours=24))
                .order_by(Cart.created_at.desc())).all()
            for cid, created in carts:
                reminded = s.scalar(select(OfferDecision.id).where(
                    OfferDecision.customer_id == cid, OfferDecision.trigger == TRIGGERS["cart"][0],
                    OfferDecision.simulated.is_(False), OfferDecision.created_at >= _aware(created)).limit(1))
                if reminded is None:
                    add([(cid,)], "cart")
        if enabled.get("birthday"):
            add(s.execute(base.where(CustomerFeature.days_to_birthday <= 7,
                                     _no_recent(TRIGGERS["birthday"][0], now - timedelta(days=30)))
                          .order_by(CustomerFeature.days_to_birthday)), "birthday")
        if enabled.get("points"):
            add(s.execute(base.where(CustomerFeature.points_expiring_30d >= 50,
                                     _no_recent(TRIGGERS["points"][0], now - timedelta(days=14)))
                          .order_by(CustomerFeature.next_points_expiry)), "points")
        if enabled.get("win_back"):
            add(s.execute(base.where(CustomerFeature.segment.in_(("at_risk", "dormant")),
                                     _no_recent(None, now - timedelta(days=7)))
                          .order_by(CustomerFeature.expected_value_90d.desc())), "win_back")
    return picked


def _claim_run(tenant_id: int, now: datetime, force: bool) -> dict | None:
    """Settings for this run if it's due (or forced), marking it as running so no other run overlaps."""
    with session_scope() as s:
        t = s.get(Tenant, tenant_id, with_for_update=True)
        cfg = settings_of(t.automation)
        state = dict(t.automation_state or {})
        if not any(cfg["enabled"].values()) or t.paused:  # paused: skip, don't log a block per customer
            return None
        running_until = state.get("running_until")
        if running_until and datetime.fromisoformat(running_until) > now:
            return None
        last = state.get("last_run")
        if not force and last and datetime.fromisoformat(last) + timedelta(minutes=cfg["every_min"]) > now:
            return None
        state["running_until"] = (now + timedelta(minutes=5)).isoformat()
        t.automation_state = state
        return {"cfg": cfg, "state": state, "slug": t.slug, "name": t.name, "prefix": t.coupon_prefix,
                "voice": t.brand_voice, "demo_chat_id": t.demo_chat_id}


def _refresh_daily(tenant_id: int, slug: str, state: dict, now: datetime) -> None:
    """Birthdays and expiring points move with the calendar: refresh profiles once a day before detecting."""
    if state.get("features_refreshed") == now.date().isoformat():
        return
    with get_engine().begin() as conn:
        refresh_features(conn, tenant_id, now, current_policy(slug).segments)
    state["features_refreshed"] = now.date().isoformat()


def _save_state(tenant_id: int, state: dict, summary: dict, now: datetime) -> None:
    with session_scope() as s:
        t = s.get(Tenant, tenant_id)
        state = {**state, "last_run": now.isoformat(), "summary": summary}
        state.pop("running_until", None)
        t.automation_state = state


@dataclass
class RunSummary:
    picked: int = 0
    by_trigger: dict | None = None
    results: dict | None = None

    def as_dict(self) -> dict:
        return {"picked": self.picked, "by_trigger": self.by_trigger or {}, "results": self.results or {}}


async def run_automation(tenant_id: int, bot: Bot | None, now: datetime, force: bool = False) -> dict | None:
    """One automation run for a business (None if nothing is switched on, it isn't due, or another is running)."""
    run = await asyncio.to_thread(_claim_run, tenant_id, now, force)
    if run is None:
        return None
    summary = RunSummary(by_trigger={}, results={})
    try:
        await asyncio.to_thread(_refresh_daily, tenant_id, run["slug"], run["state"], now)
        due = await asyncio.to_thread(due_customers, tenant_id, run["cfg"]["enabled"], now)
        policy = current_policy(run["slug"])
        mirror = Mirror(run["demo_chat_id"], MIRROR_BATCH_LIMIT, "Automatic offers")
        bc = SimpleNamespace(tenant=SimpleNamespace(demo_chat_id=run["demo_chat_id"]))
        from app.triggers.events import send_result  # (events imports push; avoid a cycle at import time)
        for customer_id, key in due:
            trigger = TRIGGERS[key][0]
            result = await asyncio.to_thread(service.request_offer, tenant_id, customer_id, policy, run["name"],
                                             run["prefix"], trigger, now, run["voice"], tenant_slug=run["slug"])
            await send_result(bc, bot, customer_id, result, mirror=mirror)
            summary.picked += 1
            summary.by_trigger[key] = summary.by_trigger.get(key, 0) + 1
            summary.results[result.kind] = summary.results.get(result.kind, 0) + 1
        await mirror.finish(bot)
    except Exception:
        log.exception("Automation run failed for business %s", tenant_id)
        summary.results["error"] = 1
    await asyncio.to_thread(_save_state, tenant_id, run["state"], summary.as_dict(), now)
    return summary.as_dict()
