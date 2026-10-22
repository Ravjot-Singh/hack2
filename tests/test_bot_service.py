from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import TENANTS_DIR
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import CustomerFeature, OfferDecision, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.delivery import templates
from app.features.store import refresh_features
from app.main import app
from app.offers import service
from app.rules.policy import load_policy

SHOPKART = TENANTS_DIR / "shopkart"
POLICY = load_policy(SHOPKART)
AS_OF = date(2026, 10, 22)
NOON = datetime.combine(AS_OF, time(12), IST)


@pytest.fixture
def world():
    ds = generate(SHOPKART / "catalog.yaml", n_customers=150, seed=5, as_of=AS_OF)
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "shopkart", "name": "ShopKart"}])
        load_dataset(conn, 1, ds, progress=lambda *_: None)
        refresh_features(conn, 1, NOON, POLICY.segments)
    return ds


def ask(customer_id, now=NOON, trigger="on_demand"):
    return service.request_offer(1, customer_id, POLICY, "ShopKart", "SH", trigger, now)


def first_customer_with_offer(ds):
    for c in ds.customers:
        r = ask(c["id"])
        if r.kind == "offer":
            return c["id"], r
    pytest.fail("no customer got an offer")


def link(chat_id, customer_id):
    service.touch_user(1, chat_id, "Tester", NOON)
    with session_scope() as s:
        s.execute(update(TelegramUser).where(TelegramUser.chat_id == chat_id).values(customer_id=customer_id))


def test_persona_assignment_is_unique_per_chat(world):
    service.touch_user(1, 100, "A", NOON)
    service.touch_user(1, 200, "B", NOON)
    a = service.assign_persona(1, 100, "new")
    with session_scope() as s:
        assert s.get(CustomerFeature, a).segment == "new"
        n_new = len(s.scalars(select(CustomerFeature).where(CustomerFeature.segment == "new")).all())
    taken = {a}
    for _ in range(n_new - 1):
        taken.add(service.assign_persona(1, 200, "new"))
        service.touch_user(1, 300 + len(taken), "x", NOON)
        service.assign_persona(1, 300 + len(taken), "new")  # park it so 200 must find another
    assert service.assign_persona(1, 999, "new") is None  # all taken (999 not registered anyway)


def test_offer_is_recorded_and_repeated_while_live(world):
    cid, r = first_customer_with_offer(world)
    assert "Code: <code>SH-" in r.message and "Demo store" in r.message
    with session_scope() as s:
        d = s.get(OfferDecision, r.decision_id)
        assert d.outcome == "sent" and d.coupon_code in r.message and d.liability_rs > 0 and d.shortlist
    again = ask(cid, NOON + timedelta(minutes=5))
    assert again.kind == "repeat" and again.decision_id == r.decision_id


def test_claim_then_rules_make_customer_wait(world):
    cid, r = first_customer_with_offer(world)
    link(500, cid)
    assert service.respond(1, 500, r.decision_id, "claimed", NOON)[0] == "ok"
    assert service.respond(1, 500, r.decision_id, "claimed", NOON)[0] == "done"
    nxt = ask(cid, NOON + timedelta(hours=1))
    assert nxt.kind == "too_soon" and nxt.next_time == NOON + timedelta(hours=48)
    with session_scope() as s:
        assert s.scalar(select(OfferDecision.outcome).where(OfferDecision.id > r.decision_id)) == "no_offer"


def test_only_owner_can_respond_and_expiry(world):
    cid, r = first_customer_with_offer(world)
    link(600, cid)
    service.touch_user(1, 601, "Intruder", NOON)
    assert service.respond(1, 601, r.decision_id, "claimed", NOON)[0] == "not_yours"
    assert service.respond(1, 600, r.decision_id, "claimed", NOON + timedelta(days=40))[0] == "expired"


def test_opt_out_blocks_offers(world):
    cid, r = first_customer_with_offer(world)
    link(700, cid)
    service.respond(1, 700, r.decision_id, "dismissed", NOON)
    service.set_opt_out(1, 700, True)
    assert ask(cid, NOON + timedelta(days=5)).kind == "opted_out"


def test_budget_counts_todays_offers(world):
    cid, r = first_customer_with_offer(world)
    with session_scope() as s:
        assert service.budget_used_today(s, 1, NOON) == s.get(OfferDecision, r.decision_id).liability_rs
        assert service.budget_used_today(s, 1, NOON + timedelta(days=1)) == 0


def test_quiet_hours_on_demand_still_answers(world):
    for c in world.customers:
        r = ask(c["id"], NOON.replace(hour=23))
        if r.kind == "offer":
            with session_scope() as s:
                d = s.get(OfferDecision, r.decision_id)
                assert d.outcome == "sent" and d.send_after is None
            return
    pytest.fail("no customer got an offer")


def test_names_are_html_escaped():
    from app.rules.engine import Candidate
    c = Candidate("birthday", "B", "percent_off", "any", 10, None, None, 499, 250, [], 250, 10, 5, 8,
                  NOON, "10% off everything", "min order ₹499 <b>", [])
    intro = templates.template_intro(c, {"first": "<script>", "store": "S&Co"})
    assert "&lt;script&gt;" in intro and "S&amp;Co" in intro
    assert "&lt;b&gt;" in templates.offer_box(c, "SH-ABC")


def test_webhook_rejects_bad_secret_and_unknown_tenant():
    with TestClient(app) as client:
        assert client.post("/tg/shopkart/wrong", json={}).status_code == 404
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        assert client.post("/tg/nobody/test-secret", json={}, headers=headers).status_code == 404
        assert client.post("/tg/shopkart/test-secret", json={}).status_code == 404  # header missing
