import time as clock
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Deal, OfferDecision, OfferOutcome, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.offers import deals as deal_engine
from app.offers import service
from app.offers.deals import DealSpec, personalise, plan_deal
from app.portal import routes as portal_routes
from app.portal.auth import reset_failures
from app.rules.policy import current_policy
from app.tenants.registry import TEMPLATE_DIR

SAME = {"Origin": "http://testserver"}
NOON = datetime.combine(datetime.now(IST).date(), time(12), IST)
SLUG = "kesar-fashions"
DEAL = {"name": "Diwali Deal", "type": "percent_off", "value": 15, "applies_to": "favourite", "segments": "all",
        "min_order_value": 699, "max_discount_rs": 300, "valid_hours": 168, "budget_rs": 200000}


def _signup(c, name, email):
    c.post("/app/register", data={"business_name": name, "email": email, "password": "sup3r-secret",
                                  "confirm": "sup3r-secret"}, headers=SAME)


@pytest.fixture
def biz(monkeypatch):
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOON)
    with TestClient(app) as c:
        _signup(c, "Kesar Fashions", "k@kesar.example")
        with session_scope() as s:
            t = s.scalar(select(Tenant).where(Tenant.slug == SLUG))
            tid, prefix = t.id, t.coupon_prefix
        ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=250, seed=8, as_of=NOON.date(), ref_prefix=prefix)
        with get_engine().begin() as conn:
            load_dataset(conn, tid, ds, progress=lambda *_: None)
            refresh_features(conn, tid, NOON, current_policy(SLUG).segments)
        yield c, tid


def _create(c, **changes):
    return c.post("/app/deals", data={**DEAL, **changes}, headers=SAME, follow_redirects=False)


def _deal_id(r):
    return int(r.headers["location"].rsplit("/", 1)[1])


def _wait_done(deal_id, timeout=20):
    end = clock.time() + timeout
    while clock.time() < end:
        with session_scope() as s:
            status = s.get(Deal, deal_id).status
        if status in ("done", "failed"):
            return status
        clock.sleep(0.2)
    pytest.fail("deal did not finish")


@pytest.mark.parametrize("changes, message", [
    ({"name": ""}, "Give the deal a name"),
    ({"budget_rs": 0}, "Set a budget"),
    ({"value": 95}, "can&#39;t be more than 90%"),
    ({"applies_to": "categories"}, "Pick at least one category"),
    ({"type": "flat_off", "value": 900, "min_order_value": 999}, "above the"),
    ({"max_discount_rs": 5000}, "store-wide limit"),
])
def test_deal_form_validation(biz, changes, message):
    c, _ = biz
    r = _create(c, **changes)
    assert r.status_code == 200 and message in r.text


def test_personalisation_caps_per_category_and_respects_category_filter(biz):
    _, tid = biz
    policy = current_policy(SLUG)
    spec = DealSpec("Big Sale", "percent_off", 25, "favourite", [], ["all"], 699, 300, 168, 10000)
    offer, _ = personalise(spec, 1, {"top_category": "electronics"}, policy)
    assert offer["category"] == "electronics" and offer["value"] == 5  # electronics cap
    offer, _ = personalise(spec, 1, {"top_category": "fashion"}, policy)
    assert offer["value"] == 25
    spec.categories = ["fashion"]
    assert personalise(spec, 1, {"top_category": "grocery"}, policy) == (
        None, "Their favourite category isn't part of this deal")
    assert personalise(spec, 1, {"top_category": None}, policy)[0] is None


def test_plan_spends_budget_on_best_offers_with_matched_control_group(biz):
    _, tid = biz
    policy = current_policy(SLUG)
    small = DealSpec("Small", "percent_off", 15, "favourite", [], ["all"], 699, 300, 168, 3000)
    with session_scope() as s:
        plan = plan_deal(s, tid, SLUG, policy, small, 99, NOON)
    assert plan.liability <= 3000 and plan.budget_stopped > 0
    assert len(plan.holdout) <= max(2, len(plan.send))  # only the funded slice is matched
    worst_sent = min(i.candidate.expected_net_rs for i in plan.send)
    assert all(i.candidate.expected_net_rs >= worst_sent for i in plan.holdout)


def test_draft_preview_edit_delete(biz):
    c, _ = biz
    r = _create(c)
    did = _deal_id(r)
    page = c.get(f"/app/deals/{did}").text
    assert "Will get the deal" in page and "Launch deal" in page and "Control group" in page
    assert "Who won't get it" in page
    c.post(f"/app/deals/{did}/edit", data={**DEAL, "name": "Diwali Mega Deal"}, headers=SAME)
    assert "Diwali Mega Deal" in c.get("/app/deals").text
    c.post(f"/app/deals/{did}/delete", headers=SAME)
    assert "Diwali Mega Deal" not in c.get("/app/deals").text


def test_launch_records_offers_simulates_demo_responses_and_exports_coupons(biz):
    c, tid = biz
    did = _deal_id(_create(c))
    to_send = int(c.get(f"/app/deals/{did}").text.split("Will get the deal</div><div class=\"value\">")[1]
                  .split("<")[0].replace(",", ""))
    c.post(f"/app/deals/{did}/launch", headers=SAME)
    assert _wait_done(did) == "done"
    with session_scope() as s:
        sent = s.scalar(select(func.count()).select_from(OfferDecision).where(
            OfferDecision.deal_id == did, OfferDecision.outcome == "sent"))
        outcomes = s.scalar(select(func.count()).select_from(OfferOutcome).join(
            OfferDecision, OfferDecision.id == OfferOutcome.decision_id).where(OfferDecision.deal_id == did))
        trig = set(s.scalars(select(OfferDecision.trigger).where(OfferDecision.deal_id == did)))
    assert sent == to_send > 0 and outcomes >= sent and trig == {"deal"}
    page = c.get(f"/app/deals/{did}").text
    assert "Did it work?" in page and "Download coupons" in page
    csv_text = c.get(f"/app/deals/{did}/coupons.csv").text
    assert csv_text.startswith("customer_id,name") and csv_text.count("\n") == sent + 1
    assert "Diwali Deal" in c.get("/app/activity").text
    # The same customers can't get a second deal straight away: frequency limits apply to deals.
    did2 = _deal_id(_create(c, name="Second Deal"))
    assert "Got an offer too recently" in c.get(f"/app/deals/{did2}").text


def test_launch_pushes_to_telegram_customers(biz, monkeypatch):
    c, tid = biz
    policy = current_policy(SLUG)
    with session_scope() as s:
        plan = plan_deal(s, tid, SLUG, policy, DealSpec.from_deal(Deal(**{**DEAL, "segments": ["all"],
                                                                          "categories": []})), 0, NOON)
    target = plan.send[0].customer_id
    service.touch_user(tid, 31337, "Asha", NOON)
    with session_scope() as s:
        s.execute(TelegramUser.__table__.update().where(TelegramUser.chat_id == 31337).values(customer_id=target))
    pushed = []

    async def fake_with_bot(bots, slug, token, fn):
        pushed.append(slug)

    monkeypatch.setattr(deal_engine, "with_bot", fake_with_bot)
    did = _deal_id(_create(c))
    c.post(f"/app/deals/{did}/launch", headers=SAME)
    assert _wait_done(did) == "done"
    with session_scope() as s:
        assert s.get(Deal, did).progress["delivered"] == 1
    assert pushed == [SLUG]


def test_other_businesses_cannot_see_deals(biz):
    c, _ = biz
    did = _deal_id(_create(c))
    c.post("/app/logout", headers=SAME)
    _signup(c, "Beta Bazaar", "b@beta.example")
    assert c.get(f"/app/deals/{did}").status_code == 404
    assert c.post(f"/app/deals/{did}/launch", headers=SAME).status_code == 404
    assert c.get(f"/app/deals/{did}/coupons.csv").status_code == 404
