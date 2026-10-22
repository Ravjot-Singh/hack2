"""Live events: something happens to a customer, their features update, and the engine reacts immediately.

Events change the synthetic data the way a real platform would see it (a new abandoned cart, points about to
expire, a birthday coming up), then run the normal decision pipeline with trigger "event:<kind>". All rules
still apply: the holdout group, weekly cap, gap between offers, quiet hours (the offer is deferred) and budget.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta

from sqlalchemy import select
from telegram import Bot

from app.db.models import Cart, Customer, CustomerFeature, PointsLedger, Product
from app.db.session import session_scope
from app.delivery.push import push_offer
from app.features.store import refresh_features
from app.offers import service
from app.rules.policy import TenantPolicy

EVENTS = {
    "cart_abandoned": "left items in their cart a few minutes ago",
    "points_expiring": "has 150 points expiring in 10 days",
    "birthday": "has a birthday in 2 days",
}


def apply_event(tenant_id: int, customer_id: int, kind: str, policy: TenantPolicy, now: datetime,
                rng: random.Random | None = None) -> str:
    """Write the event into the data and refresh that customer's features. Returns a description."""
    if kind not in EVENTS:
        raise ValueError(f"Unknown event '{kind}'. Known: {', '.join(EVENTS)}")
    rng = rng or random.Random()
    with session_scope() as s:
        cust = s.get(Customer, customer_id)
        feat = s.get(CustomerFeature, customer_id)
        if kind == "cart_abandoned":
            cat = (feat.top_category if feat and feat.top_category else rng.choice(list(policy.catalog.margins)))
            products = s.scalars(select(Product).where(Product.tenant_id == tenant_id, Product.category == cat)).all()
            picked = rng.sample(products, k=min(len(products), rng.randint(1, 3)))
            s.add(Cart(tenant_id=tenant_id, customer_id=customer_id, created_at=now - timedelta(minutes=10),
                       status="abandoned", items_count=len(picked), value=sum(p.price for p in picked),
                       top_category=cat, order_id=None))
        elif kind == "points_expiring":
            earned = now - timedelta(days=170)
            s.add(PointsLedger(tenant_id=tenant_id, customer_id=customer_id, created_at=earned, kind="earn",
                               delta=150, order_id=None, expires_at=now + timedelta(days=10)))
            cust.points_balance += 150
        else:
            soon = (now + timedelta(days=2)).date()
            day = 28 if (soon.month, soon.day) == (2, 29) else soon.day
            cust.birthday = date(cust.birthday.year, soon.month, day)
        s.flush()
        refresh_features(s.connection(), tenant_id, now, policy.segments, [customer_id])
    return f"{cust.external_ref} {EVENTS[kind]}"


async def fire_event(bc, bot: Bot | None, customer_id: int, kind: str, now: datetime) -> str:
    """Apply an event and run the pipeline; pushes to Telegram if the customer is a linked real chat."""
    desc = await asyncio.to_thread(apply_event, bc.tenant_id, customer_id, kind, bc.policy, now)
    result = await asyncio.to_thread(service.request_offer, bc.tenant_id, customer_id, bc.policy, bc.tenant.name,
                                     bc.coupon_prefix, f"event:{kind}", now, bc.tenant.brand_voice,
                                     tenant_slug=bc.tenant.slug)
    chat = await asyncio.to_thread(service.chat_for_customer, customer_id)
    if result.kind == "offer" and chat and bot:
        await push_offer(bot, chat, result.decision_id, result.message)
        return f"{desc} -> offer pushed to Telegram (decision {result.decision_id})"
    if result.kind == "deferred":
        return f"{desc} -> quiet hours: offer queued until {result.next_time:%d %b %H:%M}"
    return f"{desc} -> {result.kind}" + (f" (decision {result.decision_id})" if result.decision_id else "")


async def run_live_campaign(bc, bot: Bot, now: datetime) -> list[str]:
    """Proactive campaign for real linked chats (each still goes through every rule and the holdout)."""
    lines = []
    for chat_id, customer_id, _ in await asyncio.to_thread(service.linked_users, bc.tenant_id):
        r = await asyncio.to_thread(service.request_offer, bc.tenant_id, customer_id, bc.policy, bc.tenant.name,
                                    bc.coupon_prefix, "campaign", now, bc.tenant.brand_voice,
                                    tenant_slug=bc.tenant.slug)
        if r.kind == "offer":
            await push_offer(bot, chat_id, r.decision_id, r.message)
        lines.append(f"chat {chat_id}: {r.kind}")
    return lines


async def dispatch_deferred(bc, bot: Bot, now: datetime) -> int:
    """Send offers that were held back by quiet hours, once the quiet hours are over."""
    due = await asyncio.to_thread(service.due_deferred, bc.tenant_id, now)
    for decision_id, chat_id, message in due:
        await push_offer(bot, chat_id, decision_id, message)
    return len(due)
