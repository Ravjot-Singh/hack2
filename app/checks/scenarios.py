"""One scenario per feature: set up a situation, run the real code, compare with what should happen.

Runs only inside the sandbox prepared by `run_all` (a fresh SQLite database with demo customers and a copy of the
business's rules), so nothing here can reach the business's data, Telegram or Groq: Telegram is a fake bot.
"""

from __future__ import annotations

import asyncio
import random
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Account, Base, Customer, Deal, Delivery, OfferDecision, TelegramUser, Tenant
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.tenants.registry import TEMPLATE_DIR, catalog_from_file, create_tenant, get_tenants


@dataclass
class Result:
    feature: str
    scenario: str
    expected: str
    actual: str = ""
    passed: bool = False

    def as_dict(self) -> dict:
        return self.__dict__


class FakeBot:
    def __init__(self, refuse=()):
        self.sent, self.refuse = [], set(refuse)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.refuse:
            from telegram.error import BadRequest
            raise BadRequest("Chat not found")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


class Sandbox:
    def __init__(self, rules_data: dict | None):
        from app.portal.auth import Business
        from app.rules.policy import adapt_rules, current_policy, save_rules
        self.now = datetime.combine(datetime.now(IST).date(), time(12), IST)
        Base.metadata.create_all(get_engine())
        catalog = catalog_from_file(TEMPLATE_DIR / "catalog.yaml")
        with session_scope() as s:
            t = create_tenant(s, "Check store")
            s.add(Account(tenant_id=t.id, email="checks@sandbox.local", password_hash="-", created_at=self.now))
            self.tid, self.slug, prefix = t.id, t.slug, t.coupon_prefix
        self.rules_note = "the default rules"
        if rules_data:
            try:
                save_rules(self.slug, self.tid, None, adapt_rules(rules_data, catalog), "checks",
                           "Copy of the business's rules", self.now)
                self.rules_note = "a copy of your offer rules"
            except Exception as exc:  # their rules don't fit the demo catalog: checks still run on the defaults
                self.rules_note = f"the default rules (yours didn't fit the demo catalog: {exc})"
        ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=400, seed=11, as_of=self.now.date(), ref_prefix=prefix)
        policy = current_policy(self.slug)
        with get_engine().begin() as conn:
            load_dataset(conn, self.tid, ds, progress=lambda *_: None)
            refresh_features(conn, self.tid, self.now, policy.segments)
        with session_scope() as s:
            account_id = s.scalar(select(Account.id).where(Account.tenant_id == self.tid))
        self.biz = Business(account_id, "checks@sandbox.local", get_tenants()[self.slug], self.tid)
        self.used: set[int] = set()

    @property
    def policy(self):
        from app.rules.policy import current_policy
        return current_policy(self.slug)

    def fresh_customer(self, eligible: bool = True, holdout: bool | None = False, min_offers: int = 1) -> int:
        """A customer not used by an earlier scenario (so frequency limits don't mix scenarios)."""
        from app.measurement.holdout import in_holdout
        from app.portal import offers as portal_offers
        with session_scope() as s:
            ids = s.scalars(select(Customer.id).where(Customer.tenant_id == self.tid).order_by(Customer.id)).all()
        pct = self.policy.rules.measurement.holdout_pct
        for cid in ids:
            if cid in self.used or (holdout is not None and in_holdout(self.slug, cid, pct) != holdout):
                continue
            if eligible and len(portal_offers.preview(self.biz, cid, self.now)[1].eligible) < min_offers:
                continue
            self.used.add(cid)
            return cid
        raise RuntimeError("no suitable demo customer left")

    def send(self, cid: int, trigger: str = "operator", now: datetime | None = None):
        from app.offers import service
        return service.request_offer(self.tid, cid, self.policy, "Check store", self.biz.tenant.prefix, trigger,
                                     now or self.now, llm=None, tenant_slug=self.slug)

    def link(self, cid: int, chat_id: int, opted_out: bool = False) -> None:
        with session_scope() as s:
            s.add(TelegramUser(tenant_id=self.tid, chat_id=chat_id, customer_id=cid, opted_out=opted_out))

    def set_tenant(self, **values) -> None:
        with session_scope() as s:
            t = s.get(Tenant, self.tid)
            for k, v in values.items():
                setattr(t, k, v)
        from app.tenants.registry import clear_tenant_cache
        clear_tenant_cache()


# ---- scenarios ----

def guard_category_cap(sb: Sandbox, r: Result):
    from app import guardrails
    from app.rules.engine import CustomerContext, evaluate
    pol = sb.policy
    cid = sb.fresh_customer()
    with session_scope() as s:
        from app.offers.service import _features
        f = _features(s, sb.tid, cid, pol, sb.now)
    c = evaluate(pol.rules, pol.catalog, CustomerContext(cid, f, f["segment"]), 0, sb.now).best
    cap = pol.rules.cap_pct("electronics")
    bad = replace(c, type="percent_off", category="electronics", percent=cap + 5, amount_rs=None)
    vs = guardrails.check(pol, bad, "operator", guardrails.CustomerState(), guardrails.BusinessState(), sb.now, False)
    r.actual = vs[0].text if vs else "allowed"
    r.passed = bool(vs) and vs[0].code == "money"


def guard_store_wide_exclusion(sb: Sandbox, r: Result):
    from app.offers import deals as deal_engine
    spec = deal_engine.DealSpec(name="Flat 100", type="flat_off", value=100, applies_to="any", categories=[],
                                segments=["all"], min_order_value=1000, max_discount_rs=None, valid_hours=72,
                                budget_rs=10**6)
    with session_scope() as s:
        plan = deal_engine.plan_deal(s, sb.tid, sb.slug, sb.policy, spec, 999, sb.now)
    leaky = [i for i in plan.send if "electronics" not in i.candidate.excluded_categories]
    r.actual = f"{len(plan.send)} coupons, {len(leaky)} usable on Electronics"
    r.passed = bool(plan.send) and not leaky


def guard_per_offer_max(sb: Sandbox, r: Result):
    from app.portal import offers as portal_offers
    lim = sb.policy.rules.limits.max_discount_rs_per_offer
    chk = portal_offers.check_custom(sb.biz, sb.fresh_customer(), {
        "type": "percent_off", "value": "10", "category": "any", "min_order_value": "499",
        "max_discount_rs": str(lim * 3), "valid_hours": "72"}, sb.now)
    r.actual = (chk.problems or chk.blocked or ["allowed"])[0]
    r.passed = not chk.ok


def guard_daily_ceiling(sb: Sandbox, r: Result):
    from app import guardrails
    pol = sb.policy
    cid = sb.fresh_customer()
    from app.portal import offers as portal_offers
    c = portal_offers.preview(sb.biz, cid, sb.now)[1].best
    biz_state = guardrails.BusinessState(spend_today=pol.rules.limits.total_spend_ceiling_rs)
    vs = guardrails.check(pol, c, "deal", guardrails.CustomerState(), biz_state, sb.now, False, budget_left=10**9)
    r.actual = vs[0].text if vs else "allowed"
    r.passed = any("ceiling" in v.text for v in vs)


def guard_contact_cap(sb: Sandbox, r: Result):
    """Hand-sent offers of *different* offers, so only the per-day cap can stop the last one."""
    from app.portal import offers as portal_offers
    cap = sb.policy.rules.limits.max_offers_per_customer_per_day
    cid = sb.fresh_customer(min_offers=cap + 1)
    keys = [c.offer_key for c in portal_offers.preview(sb.biz, cid, sb.now)[1].eligible][:cap + 1]
    results = [service_send_only(sb, cid, k, sb.now + timedelta(minutes=i)) for i, k in enumerate(keys)]
    r.actual = " → ".join(x.kind for x in results) + (f" ({results[-1].headline})" if results[-1].kind == "blocked" else "")
    r.passed = [x.kind for x in results] == ["offer"] * cap + ["blocked"] and "today" in (results[-1].headline or "")


def guard_live_coupon(sb: Sandbox, r: Result):
    cid = sb.fresh_customer()
    first, second = sb.send(cid, "operator"), sb.send(cid, "operator", sb.now + timedelta(minutes=5))
    r.actual = f"{first.kind} → {second.kind}" + (f" ({second.headline})" if second.kind == "blocked" else "")
    r.passed = first.kind == "offer" and second.kind == "blocked" and "live" in (second.headline or "")


def service_send_only(sb: Sandbox, cid: int, offer_key: str, now: datetime):
    from app.offers import service
    from app.portal.offers import only_offer
    return service.request_offer(sb.tid, cid, only_offer(sb.policy, offer_key), "Check store", sb.biz.tenant.prefix,
                                 "operator", now, llm=None, tenant_slug=sb.slug)


def guard_pause(sb: Sandbox, r: Result):
    sb.set_tenant(paused=True)
    try:
        auto = sb.send(sb.fresh_customer(), "event:cart_abandoned").kind
        hand = sb.send(sb.fresh_customer(), "operator").kind
    finally:
        sb.set_tenant(paused=False)
    r.actual = f"automatic: {auto}, by hand: {hand}"
    r.passed = auto == "blocked" and hand == "offer"


def quiet_hours(sb: Sandbox, r: Result):
    cid = sb.fresh_customer()
    sb.link(cid, 700001)
    q = sb.policy.rules.limits.quiet_hours
    night = datetime.combine(sb.now.date(), q.start, IST) + timedelta(minutes=30)
    res = sb.send(cid, "operator", night)
    r.actual = f"{res.kind}" + (f", goes out {res.next_time:%d %b %H:%M}" if res.next_time else "")
    r.passed = res.kind == "deferred" and res.next_time is not None


def personal_quiet_time(sb: Sandbox, r: Result):
    from app import preferences
    cid = sb.fresh_customer()
    sb.link(cid, 700002)
    preferences.toggle(sb.tid, cid, "quiet", "workday", sb.now)
    res = sb.send(cid, "operator")
    r.actual = f"{res.kind}" + (f", goes out {res.next_time:%H:%M}" if res.next_time else "")
    r.passed = res.kind == "deferred" and res.next_time is not None and res.next_time.hour == 17


def opt_out(sb: Sandbox, r: Result):
    cid = sb.fresh_customer()
    sb.link(cid, 700003, opted_out=True)
    res = sb.send(cid, "event:birthday")
    r.actual = res.kind
    r.passed = res.kind == "opted_out"


def control_group(sb: Sandbox, r: Result):
    cid = sb.fresh_customer(holdout=True)
    auto = sb.send(cid, "event:cart_abandoned").kind
    r.actual = f"automatic: {auto}"
    r.passed = auto == "holdout"


def duplicate_hand_send(sb: Sandbox, r: Result):
    from app.portal import offers as portal_offers
    cid = sb.fresh_customer()
    sb.send(cid, "operator")
    again = portal_offers.just_sent(sb.biz, cid, sb.now + timedelta(seconds=30))
    later = portal_offers.just_sent(sb.biz, cid, sb.now + timedelta(minutes=5))
    r.actual = f"30 s later: {'refused' if again else 'allowed'}; 5 min later: {'refused' if later else 'allowed'}"
    r.passed = again and not later


def deal_double_launch(sb: Sandbox, r: Result):
    from fastapi import HTTPException
    from app.portal import deals_routes
    with session_scope() as s:
        d = Deal(tenant_id=sb.tid, created_at=sb.now, created_by="checks", status="draft", progress={}, name="Twice",
                 type="percent_off", value=10, applies_to="any", categories=[], segments=["all"], min_order_value=499,
                 max_discount_rs=200, valid_hours=72, budget_rs=1000)
        s.add(d)
        s.flush()
        deal_id = d.id
    deals_routes._mark_sending(sb.biz, deal_id)
    try:
        deals_routes._mark_sending(sb.biz, deal_id)
        r.actual = "launched twice"
    except HTTPException as exc:
        r.actual = f"second launch refused: {exc.detail}"
        r.passed = True


def fairness_rebalance(sb: Sandbox, r: Result):
    from app import fairness
    from app.portal import offers as portal_offers
    c = portal_offers.preview(sb.biz, sb.fresh_customer(), sb.now)[1].best
    members, funded, unfunded = [], [], []
    for i in range(60):
        m = fairness.Member(i, {"city_tier": "Metro"}, replace(c, liability_rs=100, expected_net_rs=50 - i))
        members.append(m)
        (funded if i < 40 else unfunded).append(m)
    for j in range(60):
        m = fairness.Member(1000 + j, {"city_tier": "Smaller town"},
                            replace(c, liability_rs=100, expected_net_rs=10 - j * 0.1) if j < 34 else None)
        members.append(m)
        if j < 4:
            funded.append(m)
        elif m.candidate is not None:
            unfunded.append(m)
    ratio = sb.policy.rules.fairness.min_ratio
    _, rep = fairness.rebalance(members, funded, unfunded, 0, ratio, 20)
    town = next(x for x in rep.rows if x.group == "Smaller town")
    r.actual = f"smaller towns {town.before_ratio:.2f} → {town.rate_ratio:.2f} of the overall rate, same budget"
    r.passed = town.before_ratio < ratio <= town.rate_ratio


def learning(sb: Sandbox, r: Result):
    from app.learning import bandit
    from app.learning.bandit import ArmStats
    from app.portal import offers as portal_offers
    cid = sb.fresh_customer(min_offers=2)
    shortlist = portal_offers.preview(sb.biz, cid, sb.now)[1].eligible
    with session_scope() as s:
        from app.offers.service import _features
        f = _features(s, sb.tid, cid, sb.policy, sb.now)
    winner, loser = shortlist[-1].offer_key, shortlist[0].offer_key
    stats = {(f["segment"], winner): ArmStats(400, 160, 40.0), (f["segment"], loser): ArmStats(400, 4, 40.0)}
    picks = [bandit.score(sb.policy, shortlist, f, stats, random.Random(i))[0].candidate.offer_key for i in range(100)]
    r.actual = f"picked the proven offer {picks.count(winner)}/100 times, the failing one {picks.count(loser)}/100"
    r.passed = picks.count(winner) > 70 and picks.count(loser) < 5


def preference_boost(sb: Sandbox, r: Result):
    from app import preferences
    from app.learning import bandit
    from app.portal import offers as portal_offers
    rules_mode = type(sb.policy)(sb.policy.rules.model_copy(update={"learning": sb.policy.rules.learning.model_copy(
        update={"mode": "rules"})}), sb.policy.catalog, sb.policy.segments)
    # Two allowed offers of equal priority: the rules rank the more valuable first; the customer likes the other.
    cid = sb.fresh_customer()
    base = portal_offers.preview(sb.biz, cid, sb.now)[1].best
    first = replace(base, offer_key="first", label="Store-wide % off", type="percent_off", expected_net_rs=90)
    liked = replace(base, offer_key="liked", label="Free delivery", type="free_delivery", expected_net_rs=40)
    shortlist = [first, liked]
    plain = bandit.rank(rules_mode, sb.tid, shortlist, {"customer_id": cid}, sb.now)
    boosts = preferences.boosts(preferences.Prefs(offer_types=["free_delivery"]), shortlist)
    ranked = bandit.rank(rules_mode, sb.tid, shortlist, {"customer_id": cid}, sb.now, boosts)
    same_set = sorted(x.offer_key for x in ranked) == sorted(x.offer_key for x in shortlist)
    r.actual = (f"without the preference: '{plain[0].label}' first; with it: '{ranked[0].label}' first"
                + ("" if same_set else "; the set of offers changed!"))
    r.passed = same_set and plain[0].offer_key == "first" and ranked[0].offer_key == "liked"


def persona_privacy(sb: Sandbox, r: Result):
    from app.offers import service
    cid = sb.fresh_customer(eligible=False, holdout=None)
    with session_scope() as s:
        s.get(Customer, cid).true_segment = None  # make it a "real" customer
    service.touch_user(sb.tid, 700009, "Stranger", sb.now)
    got = {service.assign_persona(sb.tid, 700009, None) for _ in range(30)}
    r.actual = "a stranger was never given the real customer" if cid not in got else "real customer handed out!"
    r.passed = cid not in got


def rupee_limits_follow_percent(sb: Sandbox, r: Result):
    from app.rules.schema import RulesConfig, check_rules
    data = sb.policy.rules.model_dump(mode="json")
    o = next(x for x in data["offers"] if x["type"] == "percent_off" and x.get("min_order_value"))
    o["value"], o["max_discount_rs"] = 30, max(1, int(0.30 * o["min_order_value"]) - 50)
    problems = check_rules(RulesConfig(**data), sb.policy.catalog.margins, {x.key for x in sb.policy.segments.segments})
    hit = next((p for p in problems if "maximum discount" in p), None)
    r.actual = hit or "accepted"
    r.passed = hit is not None


def mirror_only_demo(sb: Sandbox, r: Result):
    from app.delivery.push import Mirror
    demo_cid, real_cid = sb.fresh_customer(), sb.fresh_customer()
    d1, d2 = sb.send(demo_cid), sb.send(real_cid)
    with session_scope() as s:
        s.get(Customer, real_cid).true_segment = None
    bot = FakeBot()

    async def run():
        m = Mirror(700010)
        await m.copy(bot, d1.decision_id)
        await m.copy(bot, d2.decision_id)
    asyncio.run(run())
    r.actual = f"{len(bot.sent) // 2} copy sent (demo customer), real customer not copied"
    r.passed = len(bot.sent) == 2 and "Demo copy" in bot.sent[0][1]


def delivery_log(sb: Sandbox, r: Result):
    from app.delivery.push import deliver
    res = sb.send(sb.fresh_customer())
    bot = FakeBot(refuse={700012})
    ok = asyncio.run(deliver(bot, 700011, res.decision_id, res.message))
    failed = asyncio.run(deliver(bot, 700012, res.decision_id, res.message))
    with session_scope() as s:
        rows = [(x.status, x.error) for x in s.scalars(select(Delivery).where(Delivery.decision_id == res.decision_id))]
    r.actual = f"logged: {', '.join(st for st, _ in rows)}" + (f" ({rows[-1][1]})" if rows and rows[-1][1] else "")
    r.passed = ok and not failed and [st for st, _ in rows] == ["sent", "failed"]


def automation_cart(sb: Sandbox, r: Result):
    from app.db.models import Cart
    from app.triggers import automation
    cid = sb.fresh_customer()
    with session_scope() as s:
        s.add(Cart(tenant_id=sb.tid, customer_id=cid, created_at=sb.now - timedelta(minutes=45), status="abandoned",
                   items_count=2, value=1500, top_category="fashion", order_id=None))
    sb.set_tenant(automation={"enabled": {"cart": True}, "every_min": 10})
    summary = asyncio.run(automation.run_automation(sb.tid, FakeBot(), sb.now, force=True))
    with session_scope() as s:
        got = s.scalar(select(OfferDecision.outcome).where(OfferDecision.customer_id == cid,
                                                           OfferDecision.trigger == "event:cart_abandoned"))
    again = automation.due_customers(sb.tid, {"cart": True}, sb.now + timedelta(minutes=10))
    sb.set_tenant(automation=None)
    r.actual = f"cart customer → {got}; picked again next run: {'yes' if (cid, 'cart') in again else 'no'}" \
               + (f"; run handled {summary['picked']}" if summary else "")
    r.passed = got is not None and (cid, "cart") not in again


SCENARIOS = [
    ("Guardrails", "A discount above the category's limit is stopped", "Stopped (money)", guard_category_cap),
    ("Guardrails", "A store-wide ₹ deal excludes categories with lower limits", "No coupon usable on Electronics",
     guard_store_wide_exclusion),
    ("Guardrails", "A custom offer above the per-offer maximum is refused", "Refused", guard_per_offer_max),
    ("Guardrails", "Nothing more once today's total spend ceiling is reached", "Stopped (ceiling)", guard_daily_ceiling),
    ("Guardrails", "At most N offers per customer per day, even by hand", "Last one stopped", guard_contact_cap),
    ("Guardrails", "No second live coupon of the same offer", "Second one stopped", guard_live_coupon),
    ("Guardrails", "Pause stops automatic offers but not hand-sent ones", "automatic blocked, by hand sent", guard_pause),
    ("Contact", "Offers during quiet hours wait for the morning", "Queued", quiet_hours),
    ("Preferences", "A customer's own quiet time delays the message", "Queued until 17:00", personal_quiet_time),
    ("Contact", "Customers who turned offers off get nothing", "opted_out", opt_out),
    ("Measurement", "Control-group customers get no automatic offer", "holdout", control_group),
    ("Safety", "The same customer can't be sent twice within 2 minutes", "Refused, then allowed", duplicate_hand_send),
    ("Safety", "A deal can't be launched twice", "Second launch refused", deal_double_launch),
    ("Fairness", "An under-served group is rebalanced within the same budget", "Back above the line", fairness_rebalance),
    ("Learning", "The engine learns to prefer the offer that works", "Proven offer picked most", learning),
    ("Preferences", "A liked offer type moves up, nothing is added or removed", "Moved up, same offers", preference_boost),
    ("Privacy", "Strangers on Telegram never get a real customer's profile", "Never", persona_privacy),
    ("Rules", "₹ limits follow the %: a too-small maximum discount is refused", "Refused", rupee_limits_follow_percent),
    ("Demo phone", "Only demo customers are copied to the demo phone", "1 copy", mirror_only_demo),
    ("Delivery", "Delivered and failed Telegram sends are both logged", "sent, failed", delivery_log),
    ("Automation", "An abandoned cart gets one reminder, not one every run", "Once", automation_cart),
]


def run_all(rules_data: dict | None) -> dict:
    sb = Sandbox(rules_data)
    out = []
    for feature, scenario, expected, fn in SCENARIOS:
        res = Result(feature, scenario, expected)
        try:
            fn(sb, res)
        except Exception as exc:  # a crash is a failed check, with the reason
            res.actual, res.passed = f"error: {exc}", False
            traceback.print_exc()
        out.append(res.as_dict())
    return {"rules": sb.rules_note, "results": out, "passed": sum(r["passed"] for r in out), "total": len(out)}
