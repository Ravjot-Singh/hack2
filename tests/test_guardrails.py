"""Guardrail harness: the final, independent safety gate before any coupon is issued."""

from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from app import guardrails
from app.config import TENANTS_DIR
from app.data.generator import IST
from app.db.models import Deal, OfferDecision, Tenant
from app.db.session import session_scope
from app.guardrails import BusinessState, CustomerState
from app.offers import campaign, service
from app.rules.engine import CustomerContext, evaluate
from app.rules.policy import current_policy, load_policy
from tests.test_agent import FEATS
from tests.test_hardening import DEAL_FORM, SAME, biz_of, c, customers_where, seed, seeded  # noqa: F401

POLICY = load_policy(TENANTS_DIR / "shopkart")
NOON = datetime(2026, 10, 22, 12, tzinfo=IST)
NIGHT = datetime(2026, 10, 22, 23, tzinfo=IST)
CANDS = {c.offer_key: c for c in evaluate(POLICY.rules, POLICY.catalog, CustomerContext(1, FEATS, "regular"), 0,
                                          NOON).eligible}


def run(c, trigger="campaign", cust=None, biz=None, now=NOON, pushes_now=True, **kw):
    return guardrails.check(POLICY, c, trigger, cust or CustomerState(), biz or BusinessState(), now, pushes_now, **kw)


def texts(vs):
    return " | ".join(v.text for v in vs)


def test_a_normal_offer_passes():
    for cand in CANDS.values():
        assert run(cand) == [], cand.offer_key


@pytest.mark.parametrize("change, expected", [
    (dict(category="electronics", percent=10.0), "above the 5% limit for Electronics"),
    (dict(category="electronics", percent=9.0), "above the 8% margin of Electronics"),
    (dict(category="any", percent=10.0, excluded_categories=[]), "must exclude Electronics, Grocery"),
    (dict(liability_rs=5000), "per-offer limit"),
])
def test_money_limits(change, expected):
    vs = run(replace(CANDS["cart_recovery"], **change))
    assert vs and vs[0].code == "money" and expected in texts(vs)


def test_flat_off_share_of_min_order_is_checked():
    flat = replace(CANDS["cart_recovery"], type="flat_off", percent=None, amount_rs=100, min_order_value=500,
                   category="electronics")
    assert "20% off is above the 5% limit" in texts(run(flat))


def test_budget_and_total_ceiling():
    lim = POLICY.rules.limits
    cand = CANDS["cart_recovery"]
    assert "Today's offer budget" in texts(run(cand, biz=BusinessState(spend_today=lim.daily_budget_rs)))
    assert "total spend ceiling" in texts(run(cand, trigger="deal", budget_left=10**9,
                                              biz=BusinessState(spend_today=lim.total_spend_ceiling_rs)))
    assert "Deal budget used up" in texts(run(cand, trigger="deal", budget_left=1))
    assert run(cand, biz=BusinessState(spend_today=10**9), simulated=True) == []  # simulations: own budget


def test_contact_limits():
    cand = CANDS["cart_recovery"]
    assert "turned offers off" in texts(run(cand, cust=CustomerState(opted_out=True)))
    assert "quiet hours" in texts(run(cand, now=NIGHT, pushes_now=True))
    assert run(cand, now=NIGHT, pushes_now=False) == []  # queued for the morning: fine
    assert "offer(s) today" in texts(run(cand, trigger="operator", cust=CustomerState(offers_today=2)))
    assert "live, unused" in texts(run(cand, cust=CustomerState(live_offer_keys={"cart_recovery"})))
    assert run(cand, trigger="on_demand", cust=CustomerState(offers_today=5)) == []  # the customer asked


def test_pause_stops_automatic_offers_only():
    paused = BusinessState(paused=True)
    for trigger in ("campaign", "deal", "event:cart_abandoned"):
        assert run(CANDS["cart_recovery"], trigger=trigger, biz=paused)[0].code == "pause"
    for trigger in ("operator", "on_demand"):
        assert run(CANDS["cart_recovery"], trigger=trigger, biz=paused) == []


# ---- through the real issuing paths ----

def _pause(tid, on=True):
    with session_scope() as s:
        s.get(Tenant, tid).paused = on


def _first_with_offer(biz, tid, now):
    from app.portal import offers as portal_offers
    return next(x for x in customers_where(tid) if portal_offers.preview(biz, x, now)[1].eligible)


def test_service_blocks_and_records_when_paused(seeded):
    from tests.test_hardening import NOW
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    cid = _first_with_offer(biz, tid, NOW)
    _pause(tid)
    r = service.request_offer(tid, cid, biz.policy, "Kesar", "KF", "event:cart_abandoned", NOW, llm=None,
                              tenant_slug="kesar-fashions")
    assert r.kind == "blocked"
    with session_scope() as s:
        d = s.get(OfferDecision, r.decision_id)
        assert d.outcome == "blocked" and d.coupon_code is None and d.guardrail[0]["code"] == "pause"
    assert service.request_offer(tid, cid, biz.policy, "Kesar", "KF", "operator", NOW, llm=None).kind == "offer"


def test_hand_sent_offers_hit_the_daily_ceiling(seeded):
    from tests.test_hardening import NOW
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    cid = _first_with_offer(biz, tid, NOW)
    kinds = [service.request_offer(tid, cid, biz.policy, "Kesar", "KF", "operator", NOW + timedelta(minutes=i),
                                   llm=None).kind for i in range(4)]
    assert kinds.count("offer") <= 2 and "blocked" in kinds


def test_paused_business_cannot_launch_a_deal(seeded):
    c, tid = seeded
    c.post("/app/deals", headers=SAME, data=DEAL_FORM)
    with session_scope() as s:
        deal_id = s.scalar(select(Deal.id))
    _pause(tid)
    assert "paused" in c.get(f"/app/deals/{deal_id}").text
    assert c.post(f"/app/deals/{deal_id}/launch", headers=SAME).status_code == 400
    with session_scope() as s:
        assert s.get(Deal, deal_id).status == "draft"


def test_deal_records_what_the_guardrail_stops(seeded, monkeypatch):
    """The rules engine normally stops these first; the gate must still catch anything that slips through."""
    from app.offers import deals as deal_engine
    from tests.test_hardening import NOW
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    spec = deal_engine.DealSpec(name="D", type="percent_off", value=10, applies_to="any", categories=[],
                                segments=["all"], min_order_value=699, max_discount_rs=200, valid_hours=72,
                                budget_rs=10**6)
    real_check = guardrails.check
    calls = []

    def leaky_upstream(policy, cand, trigger, cust, biz_state, now, pushes_now, **kw):
        if not calls:  # the first (most valuable) customer: as if already messaged a lot today
            cust.offers_today = 99
        calls.append(1)
        return real_check(policy, cand, trigger, cust, biz_state, now, pushes_now, **kw)
    monkeypatch.setattr(deal_engine.guardrails, "check", leaky_upstream)
    with session_scope() as s:
        d = Deal(tenant_id=tid, created_at=NOW, created_by="t", status="sending", progress={}, **spec.__dict__)
        s.add(d)
        s.flush()
        deal_id = d.id
    deal_engine.execute_deal(deal_id, tid, "kesar-fashions", "Kesar", "KF", "friendly", NOW, llm=None)
    with session_scope() as s:
        blocked = s.scalars(select(OfferDecision).where(OfferDecision.deal_id == deal_id,
                                                        OfferDecision.outcome == "blocked")).all()
        sent = s.scalar(select(func.count()).select_from(OfferDecision).where(
            OfferDecision.deal_id == deal_id, OfferDecision.outcome == "sent"))
    assert len(blocked) == 1 and blocked[0].coupon_code is None and "offer(s) today" in blocked[0].block_reason
    assert sent > 0


def test_normal_simulated_campaign_is_not_blocked(seeded):
    from tests.test_hardening import NOW
    _, tid = seeded
    pol = current_policy("kesar-fashions")
    with session_scope() as s:
        res = campaign.run_simulated_campaign(s, "kesar-fashions", tid, pol, "Kesar", "KF", NOW)
    assert res.sent > 0 and res.guard_blocked == 0
    _pause(tid)
    with session_scope() as s:
        res = campaign.run_simulated_campaign(s, "kesar-fashions", tid, pol, "Kesar", "KF", NOW + timedelta(days=3))
    assert res.sent == 0 and res.guard_blocked > 0


def test_guardrails_page_and_pause_switch(seeded):
    c, tid = seeded
    page = c.get("/app/guardrails").text
    assert "Automatic offers are running" in page and "Hard limits today" in page
    c.post("/app/guardrails/pause", data={"paused": "on"}, headers=SAME)
    with session_scope() as s:
        assert s.get(Tenant, tid).paused
    assert "Automatic offers are paused" in c.get("/app").text  # banner everywhere else
    c.post("/app/guardrails/pause", data={"paused": "off"}, headers=SAME)
    with session_scope() as s:
        assert not s.get(Tenant, tid).paused


def test_new_limits_are_editable_in_the_rules_form(seeded):
    from tests.test_dashboard import _form  # same field names in the portal
    c, _ = seeded
    form = {k: v for k, v in _form().items() if not k.startswith("offer.") or "kesar" in k}
    pol = current_policy("kesar-fashions")
    form.update({f"offer.{o.key}.enabled": "on" for o in pol.rules.offers})
    form.update({"limits.max_offers_per_customer_per_day": 3, "limits.max_total_spend_per_day_rs": 50000})
    r = c.post("/app/settings/rules", data=form, headers=SAME, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    lim = current_policy("kesar-fashions").rules.limits
    assert lim.max_offers_per_customer_per_day == 3 and lim.total_spend_ceiling_rs == 50000
