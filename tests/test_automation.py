"""Automatic offers (scheduler) and the portal Demo page."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app.db.models import Cart, CustomerFeature, OfferDecision, Tenant
from app.db.session import session_scope
from app.triggers import automation, events
from tests.test_hardening import NOW, SAME, biz_of, c, customers_where, seeded  # noqa: F401
from tests.test_messages import FakeBot


def _enable(tid, **on):
    with session_scope() as s:
        s.get(Tenant, tid).automation = {"enabled": on, "every_min": 10}


def _decisions(tid, trigger=None):
    with session_scope() as s:
        q = select(func.count()).select_from(OfferDecision).where(OfferDecision.tenant_id == tid)
        if trigger:
            q = q.where(OfferDecision.trigger == trigger)
        return s.scalar(q)


def _leave_cart(tid, cid, minutes_ago):
    with session_scope() as s:
        s.add(Cart(tenant_id=tid, customer_id=cid, created_at=NOW - timedelta(minutes=minutes_ago), status="abandoned",
                   items_count=2, value=1800, top_category="fashion", order_id=None))


def test_nothing_runs_while_switched_off(seeded):
    _, tid = seeded
    assert asyncio.run(automation.run_automation(tid, None, NOW, force=True)) is None
    assert _decisions(tid) == 0


def test_cart_detector_picks_only_carts_in_the_window_once(seeded):
    _, tid = seeded
    ids = customers_where(tid)[:3]
    _leave_cart(tid, ids[0], 45)          # due
    _leave_cart(tid, ids[1], 10)          # too fresh: they may still buy
    _leave_cart(tid, ids[2], 60 * 30)     # too old
    due = automation.due_customers(tid, {"cart": True}, NOW)
    assert (ids[0], "cart") in due and all(cid not in (ids[1], ids[2]) for cid, k in due if k == "cart")
    _enable(tid, cart=True)
    first = asyncio.run(automation.run_automation(tid, FakeBot(), NOW, force=True))
    assert first["by_trigger"].get("cart", 0) >= 1
    assert _decisions(tid, "event:cart_abandoned") >= 1
    again = automation.due_customers(tid, {"cart": True}, NOW + timedelta(minutes=10))
    assert (ids[0], "cart") not in again  # reminded once, not every run


def test_birthday_points_and_win_back_detectors(seeded):
    _, tid = seeded
    with session_scope() as s:
        rows = s.scalars(select(CustomerFeature).where(CustomerFeature.tenant_id == tid)
                         .order_by(CustomerFeature.customer_id)).all()
        rows[0].days_to_birthday = 3
        rows[1].points_expiring_30d, rows[1].days_to_birthday = 120, 200
        rows[2].segment, rows[2].days_to_birthday = "dormant", 200
        a, b, d = rows[0].customer_id, rows[1].customer_id, rows[2].customer_id
    due = dict(automation.due_customers(tid, {"birthday": True, "points": True, "win_back": True}, NOW, limit=500))
    assert due.get(a) == "birthday" and due.get(b) == "points" and due.get(d) == "win_back"


def test_runs_respect_interval_pause_and_the_per_run_cap(seeded, monkeypatch):
    _, tid = seeded
    monkeypatch.setattr(automation, "MAX_PER_RUN", 5)
    with session_scope() as s:
        for f in s.scalars(select(CustomerFeature).where(CustomerFeature.tenant_id == tid)):
            f.segment = "dormant"
    _enable(tid, win_back=True)
    first = asyncio.run(automation.run_automation(tid, None, NOW))
    assert first["picked"] == 5
    assert asyncio.run(automation.run_automation(tid, None, NOW + timedelta(minutes=3))) is None  # not due yet
    with session_scope() as s:
        s.get(Tenant, tid).paused = True
    assert asyncio.run(automation.run_automation(tid, None, NOW + timedelta(minutes=30))) is None  # paused: skipped
    assert _decisions(tid, "event:win_back") == 5


def test_automation_settings_and_run_now_from_the_portal(seeded):
    c, tid = seeded
    page = c.get("/app/guardrails").text
    assert "Automatic offers" in page and "Not run yet" in page
    c.post("/app/guardrails/automation", data={"auto.win_back": "on", "every_min": "15"}, headers=SAME)
    with session_scope() as s:
        cfg = automation.settings_of(s.get(Tenant, tid).automation)
    assert cfg["enabled"]["win_back"] and not cfg["enabled"]["cart"] and cfg["every_min"] == 15
    r = c.post("/app/guardrails/automation/run", headers=SAME)
    assert "Automation ran for" in r.text


def test_demo_event_runs_the_pipeline_and_opens_the_message(seeded):
    c, tid = seeded
    cid = customers_where(tid)[4]
    r = c.post("/app/demo/event", data={"customer_id": cid, "kind": "cart_abandoned"}, headers=SAME,
               follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        d = s.scalars(select(OfferDecision).where(OfferDecision.customer_id == cid)).first()
    assert d.trigger == "event:cart_abandoned" and r.headers["location"] == f"/app/messages/{d.id}"
    assert "Demo" in c.get(f"/app/demo?customer_id={cid}").text


def test_lapsed_event_makes_a_customer_look_lapsed(seeded):
    _, tid = seeded
    cid = customers_where(tid)[6]
    events.apply_event(tid, cid, "lapsed", biz_of("kesar-fashions").policy, NOW)
    with session_scope() as s:
        assert s.get(CustomerFeature, cid).recency_days >= 140


def test_demo_events_never_touch_real_customers(c):
    from tests.test_portal_data import _wait_job, good_files
    c.post("/app/data/upload", data={"confirm": "yes"}, headers=SAME,
           files={k: (f"{k}.csv", v, "text/csv") for k, v in good_files().items()})
    _wait_job()
    with session_scope() as s:
        tid = s.scalar(select(Tenant.id).where(Tenant.slug == "kesar-fashions"))
    cid = customers_where(tid)[0]
    r = c.post("/app/demo/event", data={"customer_id": cid, "kind": "lapsed"}, headers=SAME)
    assert r.status_code == 400 and _decisions(tid) == 0
