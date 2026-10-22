import asyncio
import random
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select, update

from app.bot.handlers import BotContext
from app.config import TENANTS_DIR
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import CustomerFeature, OfferDecision, OfferOutcome, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.measurement.holdout import in_holdout
from app.measurement.metrics import format_report, measure
from app.measurement.outcomes import simulate
from app.offers import service
from app.offers.campaign import run_simulated_campaign
from app.rules.policy import load_policy
from app.tenants.registry import load_tenants
from app.triggers.events import apply_event, fire_event

SHOPKART = TENANTS_DIR / "shopkart"
POLICY = load_policy(SHOPKART)
AS_OF = date(2026, 10, 22)
NOON = datetime.combine(AS_OF, time(11), IST)
CFG = load_tenants()["shopkart"]


@pytest.fixture
def world():
    ds = generate(SHOPKART / "catalog.yaml", n_customers=400, seed=9, as_of=AS_OF)
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "shopkart", "name": "ShopKart"}])
        load_dataset(conn, 1, ds, progress=lambda *_: None)
        refresh_features(conn, 1, NOON, POLICY.segments)
    return ds


def test_holdout_is_stable_and_about_the_right_size():
    ids = range(1, 20001)
    share = sum(in_holdout("shopkart", i, 10) for i in ids) / len(ids)
    assert 0.09 < share < 0.11
    assert in_holdout("shopkart", 42, 10) == in_holdout("shopkart", 42, 10)
    assert not any(in_holdout("shopkart", i, 0) for i in ids)


def test_offers_lift_purchases_in_the_simulated_world():
    rng = random.Random(1)
    f = {"buy_anyway_prob": 0.1, "aov": 1200}
    offer = {"key": "deal_flash", "type": "percent_off", "percent": 12, "max_discount_rs": 200, "min_order_value": 499}
    kw = dict(features=f, true_segment="bargain_hunter", price_sensitivity=0.8, window_hours=48, margin_pct=30,
              delivery_fee=40, point_value=1)
    with_offer = [simulate(rng, offer=offer, **kw) for _ in range(4000)]
    without = [simulate(rng, offer=None, **kw) for _ in range(4000)]
    assert sum(o.purchased for o in with_offer) > 1.5 * sum(o.purchased for o in without)
    assert not any(o.redeemed or o.discount_cost for o in without)
    assert all(o.discount_cost <= 200 for o in with_offer)


def _campaign(budget=None):
    with session_scope() as s:
        return run_simulated_campaign(s, "shopkart", 1, POLICY, "ShopKart", "SH", NOON, daily_budget=budget)


def test_campaign_respects_budget_and_records_everything(world):
    r = _campaign()
    with session_scope() as s:
        sent = s.scalars(select(OfferDecision).where(OfferDecision.outcome == "sent")).all()
        assert sum(d.liability_rs for d in sent) == r.liability <= POLICY.rules.limits.daily_budget_rs
        assert all(d.simulated for d in sent)
        n_decisions = s.scalar(select(func.count()).select_from(OfferDecision))
        n_outcomes = s.scalar(select(func.count()).select_from(OfferOutcome))
    assert n_decisions == n_outcomes == r.sent + r.holdout  # budget-stopped customers are counted, not stored
    assert r.budget_stopped > 0  # the real budget can't fund everyone


def test_holdout_is_about_ten_percent_when_everyone_is_funded(world):
    r = _campaign(budget=10_000_000)
    assert r.budget_stopped == 0 and 0.03 < r.holdout / r.eligible < 0.2


def test_holdout_only_includes_customers_the_budget_would_reach(world):
    _campaign()
    with session_scope() as s:
        sent = s.scalars(select(OfferDecision).where(OfferDecision.outcome == "sent")).all()
        held = s.scalars(select(OfferDecision).where(OfferDecision.outcome == "holdout")).all()
    assert held and len(held) < 0.5 * len(sent)  # matched to the funded slice, not all ~10% of eligible


def test_budget_goes_to_most_valuable_offers_first(world):
    _campaign()
    with session_scope() as s:
        sent_best = s.scalars(select(OfferDecision.expected_net_rs).where(OfferDecision.outcome == "sent")).all()
    assert sent_best and min(sent_best) > -20


def test_measurement_shows_uplift_when_every_offer_is_funded(world):
    _campaign(budget=10_000_000)
    with get_engine().connect() as conn:
        rep = measure(conn, 1)
    assert rep.overall.treat.n > 5 * rep.overall.hold.n > 0
    assert rep.overall.treat.conv > rep.overall.hold.conv
    assert rep.overall.treat.discounts > 0
    text = format_report(rep, {o.key: o.label for o in POLICY.rules.offers}, {})
    assert "All offers" in text and "Engine's expected profit" in text


def test_holdout_and_treated_face_the_same_gaps_on_later_days(world):
    _campaign(budget=10_000_000)
    with session_scope() as s:
        r2 = run_simulated_campaign(s, "shopkart", 1, POLICY, "ShopKart", "SH", NOON + timedelta(days=1),
                                    daily_budget=10_000_000)
    assert r2.holdout == 0 and r2.sent == 0  # everyone (both groups) is inside the 48h gap


def test_linked_customers_are_left_out_of_simulations(world):
    service.touch_user(1, 77, "Real person", NOON)
    cid = service.assign_persona(1, 77, None)
    _campaign(budget=10_000_000)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OfferDecision).where(OfferDecision.customer_id == cid)) == 0


def test_simulated_decisions_do_not_touch_live_limits(world):
    _campaign(budget=10_000_000)
    with session_scope() as s:
        assert service.budget_used_today(s, 1, NOON) == 0
        cid = s.scalar(select(OfferDecision.customer_id).where(OfferDecision.outcome == "sent").limit(1))
    assert service.request_offer(1, cid, POLICY, "ShopKart", "SH", "on_demand", NOON + timedelta(hours=1),
                                 llm=None).kind == "offer"


def test_holdout_applies_to_proactive_triggers_only(world):
    with session_scope() as s:
        ids = s.scalars(select(CustomerFeature.customer_id)).all()
    held = next(i for i in ids if in_holdout("shopkart", i, POLICY.rules.measurement.holdout_pct)
                and service.request_offer(1, i, POLICY, "ShopKart", "SH", "campaign", NOON, llm=None,
                                          tenant_slug="shopkart").kind == "holdout")
    with session_scope() as s:
        d = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == held)).one()
        assert d.offer_key and d.liability_rs == 0 and d.coupon_code is None
    assert service.request_offer(1, held, POLICY, "ShopKart", "SH", "on_demand", NOON, llm=None,
                                 tenant_slug="shopkart").kind == "offer"


def test_cart_event_updates_features_and_triggers_cart_offer(world):
    cid = next(c["id"] for c in world.customers
               if not in_holdout("shopkart", c["id"], POLICY.rules.measurement.holdout_pct)
               and c["true_segment"] == "regular")
    desc = apply_event(1, cid, "cart_abandoned", POLICY, NOON, random.Random(0))
    assert "left items in their cart" in desc
    with session_scope() as s:
        assert s.get(CustomerFeature, cid).abandoned_carts_7d >= 1
    bc = BotContext(CFG, 1, POLICY)
    out = asyncio.run(fire_event(bc, None, cid, "points_expiring", NOON))
    with session_scope() as s:
        assert s.get(CustomerFeature, cid).points_expiring_30d >= 150
        d = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == cid)).one()
    assert d.trigger == "event:points_expiring" and str(d.id) in out


def test_quiet_hours_defer_event_offers_until_morning(world):
    service.touch_user(1, 88, "Night owl", NOON)
    late = NOON.replace(hour=23)
    for c in world.customers:
        if in_holdout("shopkart", c["id"], POLICY.rules.measurement.holdout_pct):
            continue
        with session_scope() as s:
            s.execute(update(TelegramUser).where(TelegramUser.chat_id == 88).values(customer_id=c["id"]))
        r = service.request_offer(1, c["id"], POLICY, "ShopKart", "SH", "event:birthday", late, llm=None,
                                  tenant_slug="shopkart")
        if r.kind == "deferred":
            assert service.due_deferred(1, late) == []
            due = service.due_deferred(1, late + timedelta(hours=10))
            assert [d[0] for d in due] == [r.decision_id] and due[0][1] == 88
            service.set_message_id(r.decision_id, 123)
            with session_scope() as s:
                assert s.get(OfferDecision, r.decision_id).outcome == "sent"
            return
    pytest.fail("no deferred offer produced")
