"""Business A must never see or change business B's customers, offers, deals or data, through any portal URL."""

from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Customer, Deal, OfferDecision, Tenant
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.portal import routes as portal_routes
from app.portal.auth import reset_failures
from app.rules.policy import current_policy
from app.tenants.registry import TEMPLATE_DIR

SAME = {"Origin": "http://testserver"}
HTML = {"Accept": "text/html"}
NOW = datetime.combine(datetime.now(IST).date(), time(12), IST)


def _signup(c, name, email):
    c.post("/app/register", data={"business_name": name, "email": email, "password": "sup3r-secret",
                                  "confirm": "sup3r-secret"}, headers=SAME)


@pytest.fixture
def two_businesses(monkeypatch):
    """Business B has data, a sent offer and a draft deal; the client ends up logged in as business A."""
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOW)
    with TestClient(app) as c:
        _signup(c, "Beta Bazaar", "b@beta.example")
        with session_scope() as s:
            beta = s.scalar(select(Tenant).where(Tenant.slug == "beta-bazaar"))
            tid, prefix = beta.id, beta.coupon_prefix
        ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=60, seed=6, as_of=NOW.date(), ref_prefix=prefix)
        with get_engine().begin() as conn:
            load_dataset(conn, tid, ds, progress=lambda *_: None)
            refresh_features(conn, tid, NOW, current_policy("beta-bazaar").segments)
        with session_scope() as s:
            cid = s.scalar(select(Customer.id).where(Customer.tenant_id == tid).order_by(Customer.id))
        c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME)
        r = c.post("/app/deals", data={"name": "Beta Deal", "type": "percent_off", "value": 10,
                                       "applies_to": "any", "segments": "all", "min_order_value": 499,
                                       "max_discount_rs": 200, "valid_hours": 72, "budget_rs": 5000},
                   headers=SAME, follow_redirects=False)
        did = int(r.headers["location"].rsplit("/", 1)[1])
        c.post("/app/logout", headers=SAME)
        _signup(c, "Alpha Mart", "a@alpha.example")
        yield c, tid, cid, did


def test_every_id_url_is_closed_to_other_businesses(two_businesses):
    c, tid, cid, did = two_businesses
    with session_scope() as s:
        before = (s.scalar(select(func.count()).select_from(OfferDecision)), s.get(Deal, did).status,
                  s.get(Deal, did).name)
    gets = [f"/app/customers/{cid}", f"/app/deals/{did}", f"/app/deals/{did}/edit", f"/app/deals/{did}/coupons.csv"]
    posts = [(f"/app/customers/{cid}/send", {"offer_key": "best"}),
             (f"/app/customers/{cid}/custom", {"action": "send", "type": "percent_off", "value": 5,
                                               "category": "any", "min_order_value": 499, "max_discount_rs": 100,
                                               "valid_hours": 72, "label": "x"}),
             (f"/app/deals/{did}/edit", {"name": "Hijacked", "type": "percent_off", "value": 10, "applies_to": "any",
                                         "segments": "all", "min_order_value": 499, "max_discount_rs": 200,
                                         "valid_hours": 72, "budget_rs": 5000}),
             (f"/app/deals/{did}/launch", {}),
             (f"/app/deals/{did}/delete", {})]
    for url in gets:
        assert c.get(url).status_code == 404, url
    for url, data in posts:
        assert c.post(url, data=data, headers=SAME).status_code == 404, url
    with session_scope() as s:
        after = (s.scalar(select(func.count()).select_from(OfferDecision)), s.get(Deal, did).status,
                 s.get(Deal, did).name)
    assert after == before
    for url in ("/app", "/app/customers", "/app/deals", "/app/activity", "/app/results", "/app/data"):
        page = c.get(url).text
        assert "Beta Deal" not in page and "Beta Bazaar" not in page, url
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == tid)) == 60


@pytest.mark.parametrize("raw, plain", [
    ("Only for at_risk, dormant; customer is regular.", "Only for At risk, Dormant (this customer is Regular)."),
    ("Audience rule not met - abandoned carts in last 7 days: 0 (needs >= 1).",
     "Only when abandoned carts in last 7 days is at least 1 (this customer: 0)."),
    ("segment: bargain_hunter", "they're in Bargain hunter"),
    ("days until birthday: 3 (<= 7)", "days until birthday: 3"),
    ("Not worth it: expected value ₹-30 is below the ₹-20 floor.",
     "It would probably cost more than it earns from this customer."),
    ("Customer has opted out of offers.", "Customer has opted out of offers."),
])
def test_engine_reasons_are_rewritten_for_business_users(raw, plain):
    labels = {"at_risk": "At risk", "dormant": "Dormant", "regular": "Regular", "bargain_hunter": "Bargain hunter"}
    assert portal_routes._plain(raw, labels) == plain


def test_friendly_error_pages_for_people_json_for_machines(two_businesses):
    c, _, cid, _ = two_businesses
    page = c.get(f"/app/customers/{cid}", headers=HTML)
    assert page.status_code == 404 and "Not found" in page.text and "Back to Home" in page.text
    assert c.get(f"/app/customers/{cid}").json() == {"detail": "Customer not found"}
    assert c.get("/app/nothing-here", headers=HTML).status_code == 404
    r = c.get("/app", headers=HTML, follow_redirects=False)
    assert r.status_code == 200  # logged in: no error page
    c.post("/app/logout", headers=SAME)
    r = c.get("/app/customers", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/login")  # redirects untouched
