"""Realism and consistency checks for the synthetic data generator."""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest
from sqlalchemy import func, select

from app.config import TENANTS_DIR
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.data.summary import realism_metrics
from app.db.models import Customer, Order, PointsLedger, Tenant
from app.db.session import get_engine

CATALOG = TENANTS_DIR / "shopkart" / "catalog.yaml"
AS_OF = date(2026, 10, 22)


@pytest.fixture(scope="module")
def ds():
    return generate(CATALOG, n_customers=1500, seed=7, as_of=AS_OF)


@pytest.fixture(scope="module")
def metrics(ds):
    return realism_metrics(ds)


def test_same_seed_same_data():
    a = generate(CATALOG, n_customers=150, seed=1, as_of=AS_OF)
    b = generate(CATALOG, n_customers=150, seed=1, as_of=AS_OF)
    assert a.orders == b.orders and a.customers == b.customers and a.points_ledger == b.points_ledger


def test_nothing_in_the_future(ds):
    cutoff = datetime.combine(AS_OF, time(0), IST)
    assert all(o["created_at"] < cutoff for o in ds.orders)
    assert all(c["created_at"] < cutoff for c in ds.carts)
    assert all(b["day"] < AS_OF for b in ds.browse_daily)
    assert all(c["signup_date"] <= AS_OF for c in ds.customers)


def test_revenue_is_concentrated_like_real_ecommerce(metrics):
    assert 0.55 <= metrics["top20_revenue_share"] <= 0.80


def test_cart_abandonment_near_industry_average(metrics):
    assert 0.60 <= metrics["cart_abandonment"] <= 0.80


def test_return_rates_follow_category(metrics):
    r = metrics["return_rates"]
    assert r["fashion"] > r["electronics"] > r["grocery"]
    assert r["fashion"] > 0.12


def test_segments_behave_differently(metrics):
    per_cust = {s: v["orders"] / v["customers"] for s, v in metrics["by_segment"].items()}
    assert per_cust["loyal_high_value"] > per_cust["regular"] > per_cust["occasional"] > per_cust["window_shopper"]


def test_lapsing_customers_have_gone_quiet(ds):
    seg = {c["id"]: c["true_segment"] for c in ds.customers}
    recent = datetime.combine(AS_OF - timedelta(days=120), time(0), IST)
    dormant_recent = [o for o in ds.orders if seg[o["customer_id"]] == "dormant" and o["created_at"] >= recent]
    assert len(dormant_recent) <= 3

    def rate(lo, hi):
        lo_ts, hi_ts = (datetime.combine(AS_OF - timedelta(days=d), time(0), IST) for d in (lo, hi))
        return sum(1 for o in ds.orders if seg[o["customer_id"]] == "at_risk" and lo_ts <= o["created_at"] < hi_ts) / (lo - hi)

    assert rate(40, 0) < 0.4 * rate(365, 200)


def test_new_customers_signed_up_recently(ds):
    new = [c for c in ds.customers if c["true_segment"] == "new"]
    assert new and all(AS_OF - c["signup_date"] <= timedelta(days=61) for c in new)


def test_sales_and_weekends_lift_orders(ds):
    per_day = Counter(o["created_at"].date() for o in ds.orders)
    median = np.median(list(per_day.values()))
    festive = [per_day[date(2026, 9, 23) + timedelta(days=i)] for i in range(10)]
    assert np.mean(festive) > 1.5 * median
    weekend = np.mean([v for d, v in per_day.items() if d.weekday() >= 5])
    weekday = np.mean([v for d, v in per_day.items() if d.weekday() < 5])
    assert weekend > weekday


def test_order_amounts_add_up(ds):
    for o in ds.orders:
        assert o["net_amount"] == o["gross_amount"] - o["discount_amount"] - o["points_redeemed"]
        assert 0 <= o["discount_amount"] < o["gross_amount"]
    items = defaultdict(int)
    for it in ds.order_items:
        items[it["order_id"]] += it["quantity"] * it["unit_price"]
    assert all(items[o["id"]] == o["gross_amount"] for o in ds.orders)


def test_points_ledger_matches_balances(ds):
    totals = defaultdict(int)
    for row in ds.points_ledger:
        totals[row["customer_id"]] += row["delta"]
    for c in ds.customers:
        assert c["points_balance"] == totals.get(c["id"], 0) >= 0


def test_loader_replaces_tenant_data():
    small = generate(CATALOG, n_customers=120, seed=3, as_of=AS_OF)
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}])
        load_dataset(conn, 1, small, progress=lambda *_: None)
        load_dataset(conn, 2, small, progress=lambda *_: None)
        load_dataset(conn, 1, small, progress=lambda *_: None)  # reseed tenant 1 only
        for tid in (1, 2):
            assert conn.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == tid)) == 120
            assert conn.scalar(select(func.count()).select_from(Order).where(Order.tenant_id == tid)) == len(small.orders)
        # Foreign keys were shifted to the right tenant's rows.
        orphan = conn.scalar(
            select(func.count()).select_from(PointsLedger).join(Customer, PointsLedger.customer_id == Customer.id)
            .where(PointsLedger.tenant_id != Customer.tenant_id)
        )
        assert orphan == 0
