from collections import Counter
from datetime import date, datetime, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import TENANTS_DIR
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import CustomerFeature, Tenant
from app.db.session import get_engine
from app.features.customer import compute_features, score
from app.features.segments import SegmentConfig, assign_segment, load_segment_config
from app.features.store import refresh_features

AS_OF = date(2026, 10, 22)
AS_OF_TS = datetime.combine(AS_OF, time(0), IST)
SHOPKART = TENANTS_DIR / "shopkart"


def _f(**kw):
    base = dict(tenure_days=400, recency_days=10, orders_90=3, orders_365=12, aov=1000, _discounted_orders=2)
    base.update(kw)
    return score(base)


def test_churn_rises_with_gap_and_slowdown():
    steady = _f()
    lapsing = _f(recency_days=120, orders_90=0)
    assert steady["churn_risk"] < 0.3 < 0.7 < lapsing["churn_risk"]
    assert _f(orders_365=0, orders_90=0, recency_days=None)["churn_risk"] == 0.9


def test_frequent_recent_buyers_would_buy_anyway():
    assert _f(orders_90=8, orders_365=30)["buy_anyway_prob"] > _f(orders_90=1, orders_365=3)["buy_anyway_prob"]


def test_deal_affinity_is_smoothed():
    assert _f(orders_365=1, _discounted_orders=1)["deal_affinity"] == 0.5
    assert _f(orders_365=20, _discounted_orders=20)["deal_affinity"] > 0.9


CFG = SegmentConfig(segments=[
    {"key": "dormant", "label": "Dormant", "description": "d", "when": {"recency_days_min": 150}},
    {"key": "regular", "label": "Regular", "description": "r", "when": {"orders_365_min": 4}},
    {"key": "other", "label": "Other", "description": "o", "when": {}},
])


def test_first_matching_segment_wins_with_reason():
    key, reason = assign_segment({"recency_days": 200, "orders_365": 9}, CFG)
    assert key == "dormant" and "days since last order: 200 (>= 150)" in reason
    assert assign_segment({"recency_days": 5, "orders_365": 9}, CFG)[0] == "regular"
    assert assign_segment({"recency_days": 5, "orders_365": 1}, CFG) == ("other", "did not match any other group")


def test_never_ordered_counts_as_long_ago():
    key, reason = assign_segment({"recency_days": None, "orders_365": 0}, CFG)
    assert key == "dormant" and "never" in reason


@pytest.mark.parametrize("bad, msg", [
    ({"segments": [{"key": "a", "label": "A", "description": "", "when": {"spend_min": 5}}]}, "not a valid condition"),
    ({"segments": [{"key": "a", "label": "A", "description": "", "when": {"orders_365_min": 5}}]}, "last segment"),
])
def test_bad_segment_config_gives_friendly_error(bad, msg):
    with pytest.raises(ValidationError, match=msg):
        SegmentConfig(**bad)


def test_shopkart_segments_file_is_valid():
    assert load_segment_config(SHOPKART / "segments.yaml").segments[-1].key == "occasional"


@pytest.fixture
def seeded():
    ds = generate(SHOPKART / "catalog.yaml", n_customers=200, seed=11, as_of=AS_OF)
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "shopkart", "name": "ShopKart"}])
        load_dataset(conn, 1, ds, progress=lambda *_: None)
    return ds


def test_features_match_raw_data(seeded):
    ds = seeded
    with get_engine().connect() as conn:
        feats = compute_features(conn, 1, AS_OF_TS)
    placed = Counter(o["customer_id"] for o in ds.orders if o["status"] != "cancelled")
    abandoned_30 = Counter(c["customer_id"] for c in ds.carts if c["status"] == "abandoned"
                           and c["created_at"] >= AS_OF_TS - timedelta(days=30))
    for c in ds.customers:
        f = feats[c["id"]]
        assert f["orders_365"] == placed[c["id"]]
        assert f["abandoned_carts_30d"] == abandoned_30[c["id"]]
        assert f["points_balance"] == c["points_balance"]
        assert 0 <= f["churn_risk"] <= 1 and 0 <= f["buy_anyway_prob"] <= 1


def test_refresh_replaces_snapshot(seeded):
    cfg = load_segment_config(SHOPKART / "segments.yaml")
    with get_engine().begin() as conn:
        refresh_features(conn, 1, AS_OF_TS, cfg)
        refresh_features(conn, 1, AS_OF_TS, cfg)
        assert conn.scalar(select(func.count()).select_from(CustomerFeature)) == len(seeded.customers)
        refresh_features(conn, 1, AS_OF_TS, cfg, customer_ids=[1, 2])
        assert conn.scalar(select(func.count()).select_from(CustomerFeature)) == len(seeded.customers)
