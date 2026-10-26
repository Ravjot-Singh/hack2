"""Live events: something happens to a customer, their features update, and the engine reacts immediately.

Events change the synthetic data the way a real platform would see it (a new abandoned cart, points about to
expire, a birthday coming up), then run the normal decision pipeline with trigger "event:<kind>". All rules
still apply: the holdout group, weekly cap, gap between offers, quiet hours (the offer is deferred) and budget.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from telegram import Bot

from app.db.models import Cart, Customer, CustomerFeature, Order, PointsLedger, Product
from app.features.customer import _aware
from app.db.session import session_scope
from app.delivery.push import MIRROR_BATCH_LIMIT, Mirror, deliver
from app.features.store import refresh_features
from app.offers import service
from app.rules.policy import TenantPolicy

EVENTS = {
    "cart_abandoned": "left items in their cart a few minutes ago",
    "points_expiring": "has 150 points expiring in 10 days",
    "birthday": "has a birthday in 2 days",
    "lapsed": "hasn't ordered for about five months",
}
# The pipeline trigger each event leads to (a lapsed customer is a win-back).
EVENT_TRIGGER = {"cart_abandoned": "event:cart_abandoned", "points_expiring": "event:points_expiring",
                 "birthday": "event:birthday", "lapsed": "event:win_back"}


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
        elif kind == "lapsed":
            # Move their history back ~5 months: same customer, but now someone who has gone quiet.
            last = s.scalar(select(func.max(Order.created_at)).where(Order.customer_id == customer_id))
            shift = timedelta(days=150) - (now - _aware(last)) if last else timedelta(0)
            if shift > timedelta(0):  # in Python: portable between SQLite and Postgres
                for row in s.scalars(select(Order).where(Order.customer_id == customer_id)):
                    row.created_at = _aware(row.created_at) - shift
                for row in s.scalars(select(Cart).where(Cart.customer_id == customer_id)):
                    row.created_at = _aware(row.created_at) - shift
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
                                     bc.coupon_prefix, EVENT_TRIGGER[kind], now, bc.tenant.brand_voice,
                                     tenant_slug=bc.tenant.slug)
    return await send_result(bc, bot, customer_id, result, desc)


async def send_result(bc, bot: Bot | None, customer_id: int, result, desc: str = "",
                      mirror: Mirror | None = None) -> str:
    """Deliver one pipeline result: push to the customer's chat if linked (logged, never raises) and copy it to
    the demo phone for demo customers. Returns a one-line description."""
    chat = await asyncio.to_thread(service.chat_for_customer, customer_id)
    mirror = mirror or Mirror(bc.tenant.demo_chat_id)
    if result.kind == "offer":
        pushed = bool(chat and bot) and await deliver(bot, chat, result.decision_id, result.message)
        await mirror.copy(bot, result.decision_id)
        where = "pushed to Telegram" if pushed else "coupon saved" if not chat else "Telegram push failed"
        return f"{desc} -> offer {where} (decision {result.decision_id})"
    if result.kind == "deferred":
        return f"{desc} -> quiet hours: offer queued until {result.next_time:%d %b %H:%M}"
    return f"{desc} -> {result.kind}" + (f" (decision {result.decision_id})" if result.decision_id else "")


async def run_live_campaign(bc, bot: Bot, now: datetime) -> list[str]:
    """Proactive campaign for real linked chats (each still goes through every rule and the holdout)."""
    lines = []
    mirror = Mirror(bc.tenant.demo_chat_id, MIRROR_BATCH_LIMIT, "Live campaign")
    for chat_id, customer_id, _ in await asyncio.to_thread(service.linked_users, bc.tenant_id):
        r = await asyncio.to_thread(service.request_offer, bc.tenant_id, customer_id, bc.policy, bc.tenant.name,
                                    bc.coupon_prefix, "campaign", now, bc.tenant.brand_voice,
                                    tenant_slug=bc.tenant.slug)
        lines.append(await send_result(bc, bot, customer_id, r, f"chat {chat_id}", mirror))
    await mirror.finish(bot)
    return lines


async def dispatch_deferred(bc, bot: Bot, now: datetime) -> int:
    """Send offers that were held back by quiet hours, once the quiet hours are over."""
    due = await asyncio.to_thread(service.due_deferred, bc.tenant_id, now)
    mirror = Mirror(bc.tenant.demo_chat_id, MIRROR_BATCH_LIMIT, "Queued offers sent after quiet hours")
    for decision_id, chat_id, message in due:
        await deliver(bot, chat_id, decision_id, message)
        await mirror.copy(bot, decision_id)
    await mirror.finish(bot)
    return len(due)
