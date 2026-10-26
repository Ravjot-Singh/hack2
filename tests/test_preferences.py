"""Customer offer preferences from Telegram: soft ranking boost, strict personal quiet times."""

from datetime import datetime, time

from sqlalchemy import select

from app import guardrails, preferences
from app.bot import handlers
from app.config import TENANTS_DIR
from app.data.generator import IST
from app.db.models import Customer, TelegramUser
from app.db.session import session_scope
from app.learning import bandit
from app.offers import service
from app.preferences import Prefs
from app.rules.policy import load_policy
from tests.test_bandit import F, NOON, SHORTLIST, _with_mode
from tests.test_hardening import _bot_call, biz_of, c, customers_where, seeded  # noqa: F401

POLICY = load_policy(TENANTS_DIR / "shopkart")


def test_toggle_round_trip(seeded):
    _, tid = seeded
    cid = customers_where(tid)[0]
    preferences.toggle(tid, cid, "cat", "fashion", NOON)
    preferences.toggle(tid, cid, "type", "free_delivery", NOON)
    p = preferences.toggle(tid, cid, "quiet", "evening", NOON)
    assert p == Prefs(["fashion"], ["free_delivery"], "evening")
    p = preferences.toggle(tid, cid, "cat", "fashion", NOON)  # tap again: off
    assert p.categories == [] and preferences.get(cid).quiet == "evening"
    assert preferences.toggle(tid, cid, "quiet", "none", NOON).quiet is None
    assert preferences.toggle(tid, cid, "type", "free_money", NOON).offer_types == ["free_delivery"]  # ignored


def test_soft_boost_reorders_only_allowed_offers():
    rules = _with_mode("rules")
    keys = [x.offer_key for x in SHORTLIST]
    last = SHORTLIST[-1]
    liked = Prefs(offer_types=[last.type], categories=[last.category])
    boosts = preferences.boosts(liked, SHORTLIST)
    assert last.offer_key in boosts and set(boosts) <= set(keys)
    ranked = [x.offer_key for x in bandit.rank(rules, 1, SHORTLIST, F, NOON, boosts)]
    assert sorted(ranked) == sorted(keys)  # nothing added or removed
    same_priority = [x for x in SHORTLIST if x.priority == last.priority]
    if len(same_priority) > 1:  # a +1 priority bump lifts it above offers of equal priority
        assert ranked.index(last.offer_key) < keys.index(last.offer_key)
    assert preferences.boosts(Prefs(), SHORTLIST) == {}


def test_personal_quiet_time_window():
    p = Prefs(quiet="evening")
    seven_pm = datetime(2026, 10, 22, 19, tzinfo=IST)
    assert preferences.quiet_until(p, seven_pm) == datetime(2026, 10, 23, 9, tzinfo=IST)
    assert preferences.quiet_until(p, NOON) is None
    assert preferences.quiet_until(Prefs(quiet="workday"), NOON) == datetime(2026, 10, 22, 17, tzinfo=IST)


def test_guardrail_refuses_to_push_during_personal_quiet_time():
    c0 = SHORTLIST[0]
    state = guardrails.CustomerState(quiet=preferences.QUIET_PRESETS["workday"][1])
    vs = guardrails.check(POLICY, c0, "event:cart_abandoned", state, guardrails.BusinessState(), NOON, True)
    assert vs and "quiet times they chose" in vs[0].text
    assert guardrails.check(POLICY, c0, "event:cart_abandoned", state, guardrails.BusinessState(), NOON, False) == []


def test_live_offer_waits_for_the_customers_quiet_time(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    from app.portal import offers as portal_offers
    cid = next(x for x in customers_where(tid) if portal_offers.preview(biz, x, NOON)[1].eligible)
    with session_scope() as s:
        s.add(TelegramUser(tenant_id=tid, chat_id=4242, customer_id=cid, opted_out=False))
    preferences.toggle(tid, cid, "quiet", "workday", NOON)
    r = service.request_offer(tid, cid, biz.policy, "Kesar", "KF", "operator", NOON, llm=None)
    assert r.kind == "deferred" and r.next_time == datetime.combine(NOON.date(), time(17), IST)
    # The customer asking for an offer is never delayed.
    preferences.toggle(tid, cid, "quiet", "none", NOON)


def test_bot_preferences_flow(seeded):
    _, tid = seeded
    cid = customers_where(tid)[0]
    with session_scope() as s:
        s.add(TelegramUser(tenant_id=tid, chat_id=777, customer_id=cid, opted_out=False))
    sent = _bot_call(handlers.prefs_cmd, tid, 777)
    assert "Your offer preferences" in sent[0]
    _bot_call(handlers.button, tid, 777, data="pref:cat:fashion")
    _bot_call(handlers.button, tid, 777, data="pref:quiet:evening")
    _bot_call(handlers.button, tid, 777, data="pref:cat:not_a_category")  # ignored
    assert preferences.get(cid) == Prefs(["fashion"], [], "evening")


def test_portal_shows_preferences_read_only(seeded):
    c, tid = seeded
    cid = customers_where(tid)[1]
    with session_scope() as s:
        s.add(TelegramUser(tenant_id=tid, chat_id=888, customer_id=cid, opted_out=False))
        name = s.scalar(select(Customer.name).where(Customer.id == cid))
    preferences.toggle(tid, cid, "type", "free_delivery", NOON)
    page = c.get(f"/app/customers/{cid}").text
    assert name in page and "Prefers Free delivery" in page
