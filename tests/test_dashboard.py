from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import TENANTS_DIR, get_settings
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import RulesVersion, Tenant
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.offers.campaign import run_simulated_campaign
from app.rules.policy import current_policy, load_policy

SHOPKART = TENANTS_DIR / "shopkart"
POLICY = load_policy(SHOPKART)
NOON = datetime.combine(date(2026, 10, 22), time(11), IST)
AUTH = ("admin", "test-pass")
SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    get_settings.cache_clear()
    ds = generate(SHOPKART / "catalog.yaml", n_customers=150, seed=4, as_of=NOON.date())
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "shopkart", "name": "ShopKart"}])
        load_dataset(conn, 1, ds, progress=lambda *_: None)
        refresh_features(conn, 1, NOON, POLICY.segments)
    with session_scope() as s:
        run_simulated_campaign(s, "shopkart", 1, POLICY, "ShopKart", "SH", NOON, daily_budget=10_000_000)
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _form(**changes) -> dict:
    """The rules form exactly as the page would submit it, with some fields changed."""
    data = POLICY.rules.model_dump(mode="json")
    lim = data["limits"]
    form = {f"limits.{k}": lim[k] for k in ("max_discount_rs_per_offer", "max_offers_per_customer_per_week",
                                            "min_hours_between_offers", "same_offer_cooldown_days",
                                            "daily_budget_rs", "min_expected_net_rs", "delivery_fee_rs")}
    form["limits.max_buy_anyway_prob_for_discounts"] = lim["max_buy_anyway_prob_for_discounts"] * 100
    form["limits.max_return_rate_for_discounts"] = lim["max_return_rate_for_discounts"] * 100
    form.update({f"cap.{c}": v for c, v in lim["max_discount_pct"].items()})
    form.update({"quiet.start": "21:00", "quiet.end": "09:00", "holdout_pct": 10, "note": "test"})
    for o in data["offers"]:
        form[f"offer.{o['key']}.enabled"] = "on"
        for f in ("value", "min_order_value", "max_discount_rs", "valid_hours", "priority"):
            if o.get(f) is not None:
                form[f"offer.{o['key']}.{f}"] = o[f]
    form.update(changes)
    return form


def test_login_required(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401


@pytest.mark.parametrize("path", ["/admin", "/admin/results", "/admin/segments", "/admin/segments?seg=new",
                                  "/admin/decisions?mode=sim", "/admin/decisions?mode=live", "/admin/customer/1",
                                  "/admin/rules", "/admin/rules.yaml", "/admin/actions"])
def test_every_page_renders(client, path):
    r = client.get(path, auth=AUTH)
    assert r.status_code == 200, r.text[:500]


def test_results_show_measurement(client):
    page = client.get("/admin/results", auth=AUTH).text
    assert "Bought with an offer" in page and "By offer" in page and "Cart comeback" in page


def test_customer_search_redirects(client):
    r = client.get("/admin/customer?q=sk00003", auth=AUTH, follow_redirects=False)  # case-insensitive ref
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/customer/")


def test_cross_site_posts_are_refused(client):
    r = client.post("/admin/rules", data=_form(), auth=AUTH, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/admin/rules", data=_form(), auth=AUTH)  # no Origin/Referer at all
    assert r.status_code == 403


def test_saving_rules_creates_version_used_by_engine(client):
    r = client.post("/admin/rules", data=_form(**{"limits.daily_budget_rs": 31000, "offer.second_order.value": 150,
                                                   "offer.second_order.min_order_value": 1599}),
                    auth=AUTH, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    policy = current_policy("shopkart", SHOPKART)
    assert policy.version_id is not None
    assert policy.rules.limits.daily_budget_rs == 31000 and policy.rules.offer("second_order").value == 150
    page = client.get(r.headers["location"], auth=AUTH).text
    assert "✓ Saved" in page and "<td>admin</td>" in page  # banner + change history row


def test_invalid_rules_are_explained_and_not_saved(client):
    r = client.post("/admin/rules", data=_form(**{"cap.electronics": 12}), auth=AUTH, headers=SAME_ORIGIN)
    assert r.status_code == 200 and "Not saved" in r.text and "above its margin" in r.text
    r = client.post("/admin/rules", data=_form(**{"offer.second_order.value": 300}), auth=AUTH, headers=SAME_ORIGIN)
    assert "Not saved" in r.text and "cap for any" in r.text
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RulesVersion)) == 0
    assert current_policy("shopkart", SHOPKART).version_id is None


def test_turning_an_offer_off_stops_it(client):
    form = _form()
    form.pop("offer.cart_recovery.enabled")
    client.post("/admin/rules", data=form, auth=AUTH, headers=SAME_ORIGIN)
    assert current_policy("shopkart", SHOPKART).rules.offer("cart_recovery").enabled is False


def test_public_server_needs_a_real_password(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me")
    get_settings.cache_clear()
    assert client.get("/admin", auth=("admin", "change-me")).status_code == 503
