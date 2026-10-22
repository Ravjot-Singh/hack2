from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.data.generator import IST
from app.db.models import Customer, OfferDecision, Tenant
from app.db.session import session_scope
from app.main import app
from app.portal.auth import reset_failures
from app.rules.policy import current_policy
from app.security import decrypt_secret, encrypt_secret, hash_password, verify_password
from app.tenants.registry import get_tenants

SAME = {"Origin": "http://testserver"}
NOW = datetime.combine(date(2026, 10, 22), time(12), IST)


@pytest.fixture
def client():
    reset_failures()
    with TestClient(app) as c:
        yield c


def signup(c, name="Kesar Fashions", email="owner@kesar.example", password="sup3r-secret"):
    return c.post("/app/register", data={"business_name": name, "email": email, "password": password,
                                         "confirm": password}, headers=SAME, follow_redirects=False)


def test_password_hashing_and_secret_encryption():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    assert h.count("$") == 5 and "correct" not in h
    token = encrypt_secret("123456:ABC-bot-token")
    assert "ABC" not in token and decrypt_secret(token) == "123456:ABC-bot-token"
    assert decrypt_secret("garbage") is None


def test_portal_requires_login(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/login?next=/app"
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"


def test_register_creates_business_with_working_defaults(client):
    r = signup(client)
    assert r.status_code == 303 and r.headers["location"] == "/app?welcome=1"
    home = client.get("/app?welcome=1").text
    assert "Welcome, Kesar Fashions!" in home and "Add your customer data" in home
    slug = next(s for s, t in get_tenants().items() if t.name == "Kesar Fashions")
    cfg = get_tenants()[slug]
    assert slug == "kesar-fashions" and cfg.prefix == "KF" and cfg.bot_token is None
    policy = current_policy(slug)  # defaults copied from the demo store, valid against its catalog
    assert policy.version_id is None and len(policy.rules.offers) >= 5 and policy.segments.segments


def test_register_validation_messages(client):
    assert "valid email" in signup(client, email="nope").text
    assert "at least 8" in signup(client, password="short").text
    r = client.post("/app/register", data={"business_name": "X Store", "email": "a@b.co", "password": "longenough",
                                           "confirm": "different1"}, headers=SAME)
    assert "passwords don&#39;t match" in r.text  # HTML-escaped apostrophe
    signup(client)
    client.post("/app/logout", headers=SAME)
    assert "already exists" in signup(client, name="Another").text


def test_login_logout_and_safe_redirect(client):
    signup(client)
    client.post("/app/logout", headers=SAME)
    assert client.get("/app", follow_redirects=False).status_code == 303
    r = client.post("/app/login", data={"email": "OWNER@kesar.example", "password": "sup3r-secret",
                                        "next": "https://evil.example/steal"}, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"  # no open redirect
    assert client.get("/app").status_code == 200


def test_wrong_password_and_lockout(client):
    signup(client)
    client.post("/app/logout", headers=SAME)
    for _ in range(5):
        assert "Wrong email or password" in client.post(
            "/app/login", data={"email": "owner@kesar.example", "password": "bad-guess"}, headers=SAME).text
    r = client.post("/app/login", data={"email": "owner@kesar.example", "password": "sup3r-secret"}, headers=SAME)
    assert "Too many attempts" in r.text


def test_cross_site_posts_refused(client):
    r = client.post("/app/register", data={"business_name": "Evil", "email": "e@e.co", "password": "12345678",
                                           "confirm": "12345678"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    signup(client)
    assert client.post("/app/logout").status_code == 403  # no Origin header at all


def test_businesses_only_see_their_own_activity(client):
    signup(client, name="Alpha Mart", email="a@alpha.example")
    client.post("/app/logout", headers=SAME)
    signup(client, name="Beta Bazaar", email="b@beta.example")
    with session_scope() as s:
        beta = s.scalar(select(Tenant).where(Tenant.slug == "beta-bazaar"))
        cust = Customer(tenant_id=beta.id, external_ref="B-1", name="Secret Shopper", signup_date=date(2026, 1, 1))
        s.add(cust)
        s.flush()
        s.add(OfferDecision(tenant_id=beta.id, customer_id=cust.id, created_at=NOW, trigger="on_demand",
                            segment="regular", outcome="no_offer", block_reason="test", liability_rs=0, shortlist=[]))
    assert "Secret Shopper" in client.get("/app").text
    client.post("/app/logout", headers=SAME)
    client.post("/app/login", data={"email": "a@alpha.example", "password": "sup3r-secret"}, headers=SAME)
    assert "Secret Shopper" not in client.get("/app").text


def test_public_server_needs_real_secrets(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    monkeypatch.setenv("SESSION_SECRET", "change-me")
    get_settings.cache_clear()
    assert client.get("/app/login").status_code == 503
    get_settings.cache_clear()
