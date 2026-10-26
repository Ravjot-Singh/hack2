"""Delivery log, demo-phone mirror and the Messages page."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from telegram.error import BadRequest

from app.db.models import Customer, Delivery, OfferDecision, TelegramUser, Tenant
from app.db.session import session_scope
from app.delivery import push
from app.delivery.push import Mirror, deliver
from app.offers import service
from app.portal import offers as portal_offers
from tests.test_hardening import NOW, SAME, biz_of, c, customers_where, seeded  # noqa: F401


class FakeBot:
    """Records what would go to Telegram; can be told to refuse a chat."""

    def __init__(self, refuse: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.refuse = refuse or set()

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.refuse:
            raise BadRequest("Chat not found")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


def _offer(tid, biz, trigger="operator"):
    cid = next(x for x in customers_where(tid) if portal_offers.preview(biz, x, NOW)[1].eligible)
    r = service.request_offer(tid, cid, biz.policy, "Kesar", "KF", trigger, NOW, llm=None)
    assert r.kind == "offer"
    return cid, r


def _deliveries(decision_id):
    with session_scope() as s:
        return [(x.kind, x.status, x.error) for x in
                s.scalars(select(Delivery).where(Delivery.decision_id == decision_id).order_by(Delivery.id))]


def test_successful_and_failed_pushes_are_logged(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    _, r1 = _offer(tid, biz)
    bot = FakeBot(refuse={999})
    assert asyncio.run(deliver(bot, 555, r1.decision_id, r1.message)) is True
    assert _deliveries(r1.decision_id) == [("customer", "sent", None)]
    with session_scope() as s:
        assert s.get(OfferDecision, r1.decision_id).telegram_message_id == 1
    assert asyncio.run(deliver(bot, 999, r1.decision_id, r1.message)) is False  # never raises
    assert _deliveries(r1.decision_id)[-1][:2] == ("customer", "failed") and "Chat not found" in _deliveries(r1.decision_id)[-1][2]


def _set_demo_phone(tid, chat):
    with session_scope() as s:
        s.get(Tenant, tid).demo_chat_id = chat


def test_mirror_copies_demo_customers_with_who_it_was_for(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    cid, r = _offer(tid, biz)
    bot = FakeBot()
    asyncio.run(Mirror(4242).copy(bot, r.decision_id))
    with session_scope() as s:
        name = s.get(Customer, cid).name
    lead, message = bot.sent[0][1], bot.sent[1][1]
    assert bot.sent[0][0] == 4242 and "Demo copy" in lead and name in lead and "coupon only" in lead
    assert message == r.message
    assert _deliveries(r.decision_id) == [("mirror", "sent", None)]


def test_mirror_skips_real_customers_and_the_phones_own_shopper(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    cid, r = _offer(tid, biz)
    with session_scope() as s:
        s.add(TelegramUser(tenant_id=tid, chat_id=4242, customer_id=cid, opted_out=False))
    bot = FakeBot()
    asyncio.run(Mirror(4242).copy(bot, r.decision_id))  # it's the phone's own shopper: no duplicate
    assert bot.sent == []
    with session_scope() as s:
        s.get(Customer, cid).true_segment = None  # a real customer
    asyncio.run(Mirror(5555).copy(bot, r.decision_id))
    assert bot.sent == []


def test_batch_mirror_sends_ten_then_a_summary(seeded, monkeypatch):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    monkeypatch.setattr(push, "MIRROR_GAP_SECONDS", 0)
    ids = []
    for cid in customers_where(tid)[:60]:
        r = service.request_offer(tid, cid, biz.policy, "Kesar", "KF", "operator", NOW, llm=None)
        if r.kind == "offer":
            ids.append(r.decision_id)
    assert len(ids) > 10
    bot = FakeBot()
    mirror = Mirror(4242, 10, "Deal #1")

    async def run():
        for i in ids:
            await mirror.copy(bot, i)
        await mirror.finish(bot)
    asyncio.run(run())
    assert len(bot.sent) == 10 * 2 + 1 and f"…and {len(ids) - 10} more" in bot.sent[-1][1]


def test_demo_phone_can_claim_a_mirrored_offer(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    _, r = _offer(tid, biz)
    assert service.respond(tid, 4242, r.decision_id, "claimed", NOW)[0] == "not_yours"
    _set_demo_phone(tid, 4242)
    assert service.respond(tid, 4242, r.decision_id, "claimed", NOW)[0] == "ok"


def test_messages_page_shows_message_status_and_timeline(seeded):
    c, tid = seeded
    biz = biz_of("kesar-fashions")
    _, r = _offer(tid, biz)
    asyncio.run(Mirror(4242).copy(FakeBot(), r.decision_id))
    page = c.get("/app/messages").text
    assert "Coupon only + copy on demo phone" in page and r.coupon in page
    detail = c.get(f"/app/messages/{r.decision_id}").text
    assert "Coupon created" in detail and "Copy sent to your demo phone" in detail and "tg-bubble" in detail
    assert c.get("/app/messages?status=coupon").text.count(r.coupon) == 1
    assert r.coupon not in c.get("/app/messages?status=delivered").text


def test_other_business_cannot_open_my_messages(seeded):
    c, tid = seeded
    _, r = _offer(tid, biz_of("kesar-fashions"))
    c.post("/app/logout", headers=SAME)
    c.cookies.clear()
    c.post("/app/register", data={"business_name": "Rival", "email": "r@rival.example", "password": "sup3r-secret",
                                  "confirm": "sup3r-secret"}, headers=SAME)
    assert c.get(f"/app/messages/{r.decision_id}").status_code == 404
    assert r.coupon not in c.get("/app/messages").text


def test_queued_offer_that_fails_is_not_retried_forever(seeded):
    _, tid = seeded
    biz = biz_of("kesar-fashions")
    _, r = _offer(tid, biz)
    with session_scope() as s:
        d = s.get(OfferDecision, r.decision_id)
        d.outcome, d.send_after = "deferred", NOW - timedelta(minutes=1)
    service.record_delivery(r.decision_id, 777, "customer", False, error="Forbidden: bot was blocked")
    with session_scope() as s:
        assert s.get(OfferDecision, r.decision_id).send_after is None


def test_message_preview_keeps_telegram_tags_only():
    from app.portal.messages import telegram_html
    out = telegram_html('<b>Hi</b> <i>there</i>\n<code>KF-1</code><script>alert(1)</script><img src=x onerror=a()>')
    assert "<b>Hi</b>" in out and "<i>there</i>" in out and "<code>KF-1</code>" in out and "<br>" in out
    assert "<script" not in out and "<img" not in out and "&lt;script&gt;" in out


def test_preview_shows_ampersands_once():
    from app.portal.messages import telegram_html
    out = telegram_html("<b>15% off Home &amp; Kitchen</b> &lt;script&gt;")
    assert "Home &amp; Kitchen" in out and "&amp;amp;" not in out and "<script" not in out
