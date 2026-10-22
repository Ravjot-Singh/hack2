from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Customer, OfferDecision, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.measurement.holdout import in_holdout
from app.offers import service
from app.portal import offers as portal_offers
from app.portal import routes as portal_routes
from app.portal.auth import reset_failures
from app.rules.policy import current_policy
from app.tenants.registry import TEMPLATE_DIR

SAME = {"Origin": "http://testserver"}
TODAY_NOON = datetime.combine(datetime.now(IST).date(), time(12), IST)


def _signup(c, name, email):
    c.post("/app/register", data={"business_name": name, "email": email, "password": "sup3r-secret",
                                  "confirm": "sup3r-secret"}, headers=SAME)


def _seed(slug, n=120, seed=3):
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == slug))
        tid, prefix = t.id, t.coupon_prefix
    ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=n, seed=seed, as_of=TODAY_NOON.date(), ref_prefix=prefix)
    with get_engine().begin() as conn:
        load_dataset(conn, tid, ds, progress=lambda *_: None)
        refresh_features(conn, tid, TODAY_NOON, current_policy(slug).segments)
    return tid, ds


@pytest.fixture
def biz(monkeypatch):
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: TODAY_NOON)
    with TestClient(app) as c:
        _signup(c, "Kesar Fashions", "k@kesar.example")
        tid, ds = _seed("kesar-fashions")
        yield c, tid, ds


def _cid(tid, n):
    """The database id of the n-th customer of this business."""
    with session_scope() as s:
        return s.scalars(select(Customer.id).where(Customer.tenant_id == tid).order_by(Customer.id)).all()[n]


def _first_with_offer(c, tid, skip_holdout=None):
    for n in range(100):
        cid = _cid(tid, n)
        if skip_holdout is not None and in_holdout("kesar-fashions", cid, 10) != skip_holdout:
            continue
        if "Send recommended offer" in c.get(f"/app/customers/{cid}").text:
            return cid
    pytest.fail("no customer with an offer")


def test_customer_list_search_and_filters(biz):
    c, tid, ds = biz
    page = c.get("/app/customers").text
    assert "120 customers" in page and ds.customers[0]["name"].split()[0] in c.get("/app/customers?sort=name").text
    ref = ds.customers[5]["external_ref"]
    found = c.get(f"/app/customers?q={ref.lower()}").text
    assert "1 customer match" in found and ref in found
    assert "No customers match" in c.get("/app/customers?q=zzz%25").text  # % is not a wildcard
    assert "0 customers match" in c.get("/app/customers?telegram=1").text
    assert "customers match" in c.get("/app/customers?group=new").text


def test_other_businesses_cannot_see_or_send(biz):
    c, tid, _ = biz
    cid = _cid(tid, 0)
    c.post("/app/logout", headers=SAME)
    _signup(c, "Beta Bazaar", "b@beta.example")
    assert c.get(f"/app/customers/{cid}").status_code == 404
    assert c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME).status_code == 404
    assert "120 customers" not in c.get("/app/customers").text


def test_send_best_offer_to_customer_not_on_telegram(biz):
    c, tid, _ = biz
    cid = _first_with_offer(c, tid, skip_holdout=True)  # a control-group member: operator sends still go out
    r = c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME)
    assert "Saved:" in r.text and "isn&#39;t on Telegram" in r.text
    with session_scope() as s:
        d = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == cid)).one()
    assert d.trigger == "operator" and d.outcome == "sent" and d.coupon_code in r.text
    assert "Sent by you" in c.get("/app").text  # shows up in the activity feed


def test_send_to_telegram_customer_pushes(biz, monkeypatch):
    c, tid, _ = biz
    cid = _first_with_offer(c, tid)
    service.touch_user(tid, 4242, "Asha", TODAY_NOON)
    with session_scope() as s:
        s.execute(TelegramUser.__table__.update().where(TelegramUser.chat_id == 4242).values(customer_id=cid))
    pushed = []

    async def fake_with_bot(bots, slug, token, fn):
        pushed.append(slug)

    monkeypatch.setattr(portal_offers, "with_bot", fake_with_bot)
    r = c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME)
    assert pushed == ["kesar-fashions"] and "Sent on Telegram" in r.text


def _custom(c, cid, action="check", **kw):
    form = {"label": "Thank you", "type": "percent_off", "value": 10, "category": "any", "min_order_value": 499,
            "max_discount_rs": 200, "valid_hours": 72, "action": action, **kw}
    return c.post(f"/app/customers/{cid}/custom", data=form, headers=SAME)


def test_custom_offer_checked_against_rules(biz):
    c, tid, _ = biz
    cid = _first_with_offer(c, tid)
    assert "cap for fashion is 30%" in _custom(c, cid, category="fashion", value=40).text
    assert "above the" in _custom(c, cid, type="flat_off", value=900, min_order_value=99999).text
    assert "above the store-wide limit" in _custom(c, cid, max_discount_rs=5000).text
    ok = _custom(c, cid, category="fashion", value=15)
    assert "Passes every rule: 15% off Fashion" in ok.text and "Send this offer" in ok.text
    sent = _custom(c, cid, action="send", category="fashion", value=15)
    assert "Saved: 15% off Fashion" in sent.text
    with session_scope() as s:
        d = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == cid)).one()
    assert d.offer_key == "custom" and d.percent == 15 and d.trigger == "operator"
    # Sending by hand skips the frequency limits (with a heads-up), but never the price rules.
    again = _custom(c, cid, category="fashion", value=15)
    assert "Passes every rule" in again.text and "Heads-up: they got an offer" in again.text
    assert "cap for fashion is 30%" in _custom(c, cid, category="fashion", value=40).text


def test_send_button_cannot_bypass_failed_check(biz):
    c, tid, _ = biz
    cid = _first_with_offer(c, tid)
    r = _custom(c, cid, action="send", category="fashion", value=40)
    assert "breaks your rules" in r.text
    with session_scope() as s:
        assert not s.scalars(select(OfferDecision).where(OfferDecision.customer_id == cid)).all()


def test_quiet_hours_queue_messages_but_not_coupons(biz, monkeypatch):
    c, tid, _ = biz
    late = TODAY_NOON.replace(hour=23)
    monkeypatch.setattr(portal_routes, "_now", lambda: late)
    cid = _first_with_offer(c, tid)
    service.touch_user(tid, 777, "Night owl", late)
    with session_scope() as s:
        s.execute(TelegramUser.__table__.update().where(TelegramUser.chat_id == 777).values(customer_id=cid))
    assert "Queued:" in c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME).text
    other = next(_cid(tid, n) for n in range(60, 100) if "Send recommended offer" in c.get(f"/app/customers/{_cid(tid, n)}").text)
    assert "Saved:" in c.post(f"/app/customers/{other}/send", data={"offer_key": "best"}, headers=SAME).text
