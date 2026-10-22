"""Behavioural features and scores per customer, computed only from observed data.

Features describe what the customer did (recency, frequency, spend, carts, browsing, points).
Scores turn them into the three numbers the offer engine cares about:
  churn_risk       how likely they are drifting away (0..1)
  buy_anyway_prob  chance they order in the next 14 days without any offer (0..1) -> don't waste discounts
  deal_affinity    how much they respond to discounts (smoothed share of discounted orders)
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

from app.data.generator import IST
from app.db.models import BrowseDaily, Cart, Customer, Order, OrderItem, PointsLedger

# Human-readable names, used in segment reasons and the dashboard.
FEATURE_LABELS = {
    "tenure_days": "days since joining",
    "recency_days": "days since last order",
    "orders_30": "orders in last 30 days",
    "orders_90": "orders in last 90 days",
    "orders_365": "orders in last 12 months",
    "monetary_365": "spend in last 12 months (Rs)",
    "aov": "average order value (Rs)",
    "discount_share": "share of orders with a discount",
    "return_rate": "share of items returned",
    "sessions_30": "app/web visits in last 30 days",
    "sessions_90": "app/web visits in last 90 days",
    "abandoned_carts_7d": "abandoned carts in last 7 days",
    "abandoned_carts_30d": "abandoned carts in last 30 days",
    "points_balance": "points balance",
    "points_expiring_30d": "points expiring within 30 days",
    "days_to_birthday": "days until birthday",
    "churn_risk": "churn risk",
    "buy_anyway_prob": "chance of buying anyway in 14 days",
    "deal_affinity": "deal affinity",
    "expected_value_90d": "expected spend next 90 days (Rs)",
}


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; everything in this app is IST.
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=IST)


def _next_birthday_in(birthday: date, today: date) -> int:
    for year in (today.year, today.year + 1):
        try:
            nxt = birthday.replace(year=year)
        except ValueError:  # 29 Feb
            nxt = date(year, 3, 1)
        if nxt >= today:
            return (nxt - today).days
    return 365


def score(f: dict) -> dict:
    """Add churn_risk, buy_anyway_prob, deal_affinity and expected_value_90d to a feature dict."""
    window = max(1, min(f["tenure_days"], 365))
    n365, n90 = f["orders_365"], f["orders_90"]
    if n365 == 0:
        churn = 0.9 if f["tenure_days"] > 120 else 0.5
    else:
        expected_gap = min(max(window / n365, 7), 365)
        gap_score = 1 / (1 + math.exp(-1.8 * (f["recency_days"] / expected_gap - 2.0)))
        churn = gap_score
        if f["tenure_days"] > 120 and n365 >= 3:  # need some history before a slowdown means anything
            drop = max(0.0, min(1.0, 1 - (n90 / 90) / (n365 / window)))
            churn = 0.55 * gap_score + 0.45 * drop
    daily_rate = 0.6 * n90 / min(90, window) + 0.4 * n365 / window
    f["churn_risk"] = round(churn, 3)
    f["buy_anyway_prob"] = round(1 - math.exp(-daily_rate * 14 * (1 - churn)), 3)
    f["deal_affinity"] = round((f["_discounted_orders"] + 1) / (n365 + 3), 3)
    f["expected_value_90d"] = int(daily_rate * 90 * f["aov"] * (1 - churn))
    return f


def compute_features(conn: Connection, tenant_id: int, as_of: datetime,
                     customer_ids: list[int] | None = None) -> dict[int, dict]:
    as_of = _aware(as_of)
    t = {d: as_of - timedelta(days=d) for d in (7, 30, 90, 365)}
    today = as_of.date()

    def scoped(stmt, model):
        stmt = stmt.where(model.tenant_id == tenant_id)
        return stmt.where(model.customer_id.in_(customer_ids)) if customer_ids is not None else stmt

    cust_q = select(Customer.id, Customer.signup_date, Customer.birthday).where(Customer.tenant_id == tenant_id)
    if customer_ids is not None:
        cust_q = cust_q.where(Customer.id.in_(customer_ids))
    feats: dict[int, dict] = {}
    for cid, signup, bday in conn.execute(cust_q):
        feats[cid] = {
            "customer_id": cid, "tenure_days": max(0, (today - signup).days), "recency_days": None,
            "orders_30": 0, "orders_90": 0, "orders_365": 0, "monetary_365": 0, "aov": 0,
            "discount_share": 0.0, "return_rate": 0.0, "sessions_30": 0, "sessions_90": 0,
            "abandoned_carts_7d": 0, "abandoned_carts_30d": 0, "last_abandoned_cart_at": None,
            "last_abandoned_cart_value": None, "last_abandoned_cart_category": None, "top_category": None,
            "points_balance": 0, "points_expiring_30d": 0, "next_points_expiry": None,
            "days_to_birthday": _next_birthday_in(bday, today), "_discounted_orders": 0,
        }

    placed = Order.status != "cancelled"
    in_year = (Order.created_at >= t[365]) & (Order.created_at < as_of)

    def count_since(days):
        return func.sum(case((placed & (Order.created_at >= t[days]) & (Order.created_at < as_of), 1), else_=0))

    q = scoped(select(
        Order.customer_id,
        func.max(case((placed & (Order.created_at < as_of), Order.created_at))),
        count_since(30), count_since(90), count_since(365),
        func.sum(case(((Order.status == "delivered") & in_year, Order.net_amount), else_=0)),
        func.sum(case((placed & in_year, Order.net_amount), else_=0)),
        func.sum(case((placed & in_year & ((Order.discount_amount > 0) | (Order.points_redeemed > 0)), 1), else_=0)),
    ), Order).group_by(Order.customer_id)
    for cid, last, o30, o90, o365, monetary, placed_value, discounted in conn.execute(q):
        f = feats.get(cid)
        if f is None:
            continue
        last = _aware(last)
        f.update(orders_30=int(o30), orders_90=int(o90), orders_365=int(o365), monetary_365=int(monetary),
                 _discounted_orders=int(discounted))
        if last is not None:
            f["recency_days"] = (as_of - last).days
        if o365:
            f["aov"] = int(placed_value / o365)
            f["discount_share"] = round(discounted / o365, 3)

    q = scoped(select(
        Order.customer_id, OrderItem.category,
        func.count(), func.sum(case((OrderItem.returned, 1), else_=0)),
        func.sum(OrderItem.quantity * OrderItem.unit_price),
    ).join(Order, OrderItem.order_id == Order.id).where(placed & in_year), Order) \
        .group_by(Order.customer_id, OrderItem.category)
    items, returned, best = defaultdict(int), defaultdict(int), {}
    for cid, cat, n, ret, spend in conn.execute(q):
        items[cid] += n
        returned[cid] += int(ret)
        if cid not in best or spend > best[cid][1]:
            best[cid] = (cat, spend)
    for cid, f in feats.items():
        if items[cid]:
            f["return_rate"] = round(returned[cid] / items[cid], 3)
        if cid in best:
            f["top_category"] = best[cid][0]

    q = scoped(select(
        BrowseDaily.customer_id,
        func.sum(case((BrowseDaily.day >= (today - timedelta(days=30)), BrowseDaily.sessions), else_=0)),
        func.sum(BrowseDaily.sessions),
    ).where(BrowseDaily.day >= today - timedelta(days=90), BrowseDaily.day < today), BrowseDaily) \
        .group_by(BrowseDaily.customer_id)
    for cid, s30, s90 in conn.execute(q):
        if cid in feats:
            feats[cid].update(sessions_30=int(s30), sessions_90=int(s90))

    q = scoped(select(Cart.customer_id, Cart.created_at, Cart.value, Cart.top_category).where(
        Cart.status == "abandoned", Cart.created_at >= t[30], Cart.created_at < as_of), Cart)
    for cid, created, value, cat in conn.execute(q):
        f = feats.get(cid)
        if f is None:
            continue
        created = _aware(created)
        f["abandoned_carts_30d"] += 1
        f["abandoned_carts_7d"] += created >= t[7]
        if f["last_abandoned_cart_at"] is None or created > f["last_abandoned_cart_at"]:
            f.update(last_abandoned_cart_at=created, last_abandoned_cart_value=value,
                     last_abandoned_cart_category=cat)

    _points(conn, scoped, feats, as_of)
    return {cid: score(f) for cid, f in feats.items()}


def _points(conn: Connection, scoped, feats: dict[int, dict], as_of: datetime) -> None:
    """Replay the ledger oldest-first: points earned first are spent/expired first."""
    q = scoped(select(PointsLedger.customer_id, PointsLedger.delta, PointsLedger.expires_at)
               .where(PointsLedger.created_at < as_of), PointsLedger) \
        .order_by(PointsLedger.customer_id, PointsLedger.created_at, PointsLedger.id)
    lots: dict[int, list[list]] = defaultdict(list)
    for cid, delta, expires in conn.execute(q):
        if delta > 0:
            lots[cid].append([delta, _aware(expires)])
            continue
        left = -delta
        for lot in lots[cid]:
            take = min(lot[0], left)
            lot[0] -= take
            left -= take
        lots[cid] = [lot for lot in lots[cid] if lot[0] > 0]
    horizon = as_of + timedelta(days=30)
    for cid, cust_lots in lots.items():
        f = feats.get(cid)
        if f is None:
            continue
        live = [lot for lot in cust_lots if lot[1] is None or lot[1] > as_of]
        f["points_balance"] = sum(lot[0] for lot in live)
        f["points_expiring_30d"] = sum(lot[0] for lot in live if lot[1] is not None and lot[1] <= horizon)
        expiries = [lot[1] for lot in live if lot[1] is not None]
        f["next_points_expiry"] = min(expiries) if expiries else None
