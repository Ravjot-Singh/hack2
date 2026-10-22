import asyncio
import time as clock
from datetime import datetime, time

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot import runner
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Deal, ImportJob, Tenant
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.portal import routes as portal_routes
from app.portal import settings_routes
from app.portal.auth import reset_failures
from app.rules.policy import current_policy
from app.tenants.registry import TEMPLATE_DIR, clear_tenant_cache, get_tenants

SAME = {"Origin": "http://testserver"}
NOW = datetime.combine(datetime.now(IST).date(), time(12), IST)
SLUG = "kesar-fashions"
TOKEN = "123456789:AAH" + "x" * 32


def _signup(c, name="Kesar Fashions", email="k@kesar.example"):
    c.post("/app/register", data={"business_name": name, "email": email, "password": "sup3r-secret",
                                  "confirm": "sup3r-secret"}, headers=SAME)


@pytest.fixture
def client(monkeypatch):
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOW)

    async def fake_verify(token):
        if token.endswith("bad" + "x" * 29):
            raise ValueError("Telegram doesn't recognise this token. Copy it again from @BotFather.")
        return "kesar_offers_bot" if token == TOKEN else "other_bot"

    monkeypatch.setattr(settings_routes, "verify_bot_token", fake_verify)
    with TestClient(app) as c:
        _signup(c)
        yield c


def _seed(n=150):
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == SLUG))
        tid, prefix = t.id, t.coupon_prefix
    ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=n, seed=2, as_of=NOW.date(), ref_prefix=prefix)
    with get_engine().begin() as conn:
        load_dataset(conn, tid, ds, progress=lambda *_: None)
        refresh_features(conn, tid, NOW, current_policy(SLUG).segments)
    return tid


def test_profile(client):
    client.post("/app/settings/profile", data={"name": "Kesar Fashion House", "brand_voice": "warm, Punjabi flavour"},
                headers=SAME)
    clear_tenant_cache()
    assert get_tenants()[SLUG].name == "Kesar Fashion House"
    assert get_tenants()[SLUG].brand_voice == "warm, Punjabi flavour"


def test_connect_bot_encrypts_and_unlocks_checklist(client):
    assert "doesn&#39;t look like a bot token" in client.post("/app/settings/bot", data={"token": "hello"},
                                                               headers=SAME).text
    bad = "123456789:AAH" + "bad" + "x" * 29
    assert "doesn&#39;t recognise this token" in client.post("/app/settings/bot", data={"token": bad},
                                                              headers=SAME).text
    page = client.post("/app/settings/bot", data={"token": TOKEN}, headers=SAME).text
    assert "Connected @kesar_offers_bot" in page and "t.me/kesar_offers_bot" in page
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == SLUG))
        assert t.bot_token_enc and TOKEN not in t.bot_token_enc and t.bot_username == "kesar_offers_bot"
    clear_tenant_cache()
    assert get_tenants()[SLUG].bot_token == TOKEN
    assert 'href="/app/settings#bot"' not in client.get("/app").text  # checklist step done (no longer a link)
    client.post("/app/settings/bot/disconnect", headers=SAME)
    clear_tenant_cache()
    assert get_tenants()[SLUG].bot_token is None


def test_same_bot_cannot_serve_two_businesses(client):
    client.post("/app/settings/bot", data={"token": TOKEN}, headers=SAME)
    client.post("/app/logout", headers=SAME)
    _signup(client, "Beta Bazaar", "b@beta.example")
    r = client.post("/app/settings/bot", data={"token": TOKEN}, headers=SAME)
    assert "already connected to another business" in r.text


def test_bot_manager_starts_restarts_and_stops(monkeypatch):
    events = []

    async def fake_start(self, slug, cfg):
        self.apps[slug], self._tokens[slug] = object(), cfg.bot_token
        events.append(("start", slug))

    async def fake_stop(self, slug):
        self.apps.pop(slug)
        self._tokens.pop(slug)
        events.append(("stop", slug))

    monkeypatch.setattr(runner.BotManager, "_start", fake_start)
    monkeypatch.setattr(runner.BotManager, "_stop", fake_stop)
    tokens = {"a": "1:x", "b": "2:y"}

    class Cfg:
        def __init__(self, t):
            self.bot_token = t

    monkeypatch.setattr(runner, "get_tenants", lambda: {k: Cfg(v) for k, v in tokens.items()})
    m = runner.BotManager(None, "")
    asyncio.run(m.sync())
    assert sorted(events) == [("start", "a"), ("start", "b")]
    events.clear()
    tokens["a"] = "1:changed"
    del tokens["b"]
    asyncio.run(m.sync())
    assert events == [("stop", "a"), ("stop", "b"), ("start", "a")]


def test_lowering_a_margin_lowers_its_discount_cap(client):
    assert current_policy(SLUG).rules.cap_pct("fashion") == 30
    r = client.post("/app/settings/categories", data={"label.fashion": "Clothing", "margin.fashion": "20"},
                    headers=SAME)
    assert "lowered from 30% to 20%" in r.text
    policy = current_policy(SLUG)
    assert policy.catalog.labels["fashion"] == "Clothing" and policy.rules.cap_pct("fashion") == 20
    assert "between 1% and 95%" in client.post("/app/settings/categories", data={"margin.fashion": "150"},
                                                headers=SAME).text


def test_rules_editor_in_portal(client):
    page = client.get("/app/settings/rules").text
    assert "Limits on every offer" in page and "Control group" in page
    form = {"limits.daily_budget_rs": "40000", "note": "more budget"}
    for o in current_policy(SLUG).rules.offers:
        form[f"offer.{o.key}.enabled"] = "on"
    r = client.post("/app/settings/rules", data=form, headers=SAME)
    assert "Saved your offer rules" in r.text and current_policy(SLUG).rules.limits.daily_budget_rs == 40000
    r = client.post("/app/settings/rules", data={**form, "cap.electronics": "12"}, headers=SAME)
    assert "Not saved" in r.text and "above its margin" in r.text


def test_groups_editor_saves_and_regroups(client):
    _seed()
    r = client.post("/app/settings/groups", data={"g.new.label": "Newcomers", "g.new.when.tenure_days_max": "30",
                                                  "g.dormant.when.churn_risk_min": "70"}, headers=SAME)
    assert "Saved customer groups" in r.text
    groups = {g.key: g for g in current_policy(SLUG).segments.segments}
    assert groups["new"].label == "Newcomers" and groups["new"].when["tenure_days_max"] == 30
    assert groups["dormant"].when["churn_risk_min"] == 0.7  # entered as %
    end = clock.time() + 30
    while clock.time() < end:
        with session_scope() as s:
            job = s.scalars(select(ImportJob).where(ImportJob.kind == "regroup")).first()
            if job and job.status != "running":
                break
        clock.sleep(0.2)
    assert job.status == "done" and job.summary["customers"] == 150
    assert "not a number" in client.post("/app/settings/groups", data={"g.new.when.tenure_days_max": "soon"},
                                         headers=SAME).text


def test_results_page(client):
    assert "No measured purchases yet" in client.get("/app/results").text
    _seed()
    r = client.post("/app/deals", data={"name": "Big Sale", "type": "percent_off", "value": 15,
                                        "applies_to": "favourite", "segments": "all", "min_order_value": 699,
                                        "max_discount_rs": 300, "valid_hours": 168, "budget_rs": 100000},
                    headers=SAME, follow_redirects=False)
    did = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/app/deals/{did}/launch", headers=SAME)
    end = clock.time() + 30
    while clock.time() < end:
        with session_scope() as s:
            if s.get(Deal, did).status == "done":
                break
        clock.sleep(0.2)
    page = client.get("/app/results").text
    assert "By deal" in page and "Big Sale" in page and "By customer group" in page
