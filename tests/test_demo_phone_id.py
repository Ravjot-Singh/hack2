"""Setting the demo phone by typing a Telegram ID: verified by a confirmation message from the business's bot."""

from telegram.error import Forbidden

from app.db.models import Tenant
from app.db.session import session_scope
from app.portal import settings_routes
from tests.test_hardening import SAME, c, seeded  # noqa: F401


class Bot:
    def __init__(self, refuse=False):
        self.sent, self.refuse = [], refuse

    async def send_message(self, chat_id, text, **kw):
        if self.refuse:
            raise Forbidden("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


def _use(monkeypatch, bot):
    async def fake_with_bot(running, slug, token, fn):
        if bot is None:
            raise LookupError("no bot connected")
        return await fn(bot)
    monkeypatch.setattr(settings_routes, "with_bot", fake_with_bot)


def _demo_chat(tid):
    with session_scope() as s:
        return s.get(Tenant, tid).demo_chat_id


def test_valid_id_gets_a_confirmation_and_becomes_the_demo_phone(seeded, monkeypatch):
    c, tid = seeded
    bot = Bot()
    _use(monkeypatch, bot)
    r = c.post("/app/settings/demo-phone-id", data={"telegram_id": " 987654321 "}, headers=SAME)
    assert "Demo phone set" in r.text and _demo_chat(tid) == 987654321
    assert bot.sent[0][0] == 987654321 and "Demo phone connected" in bot.sent[0][1]
    with session_scope() as s:  # the Demo phone card is shown once the business has a bot
        s.get(Tenant, tid).bot_username = "kesar_bot"
    from app.tenants.registry import clear_tenant_cache
    clear_tenant_cache()
    page = c.get("/app/settings").text
    assert "Telegram ID 987654321" in page and "t.me/kesar_bot" in page and "Or enter your Telegram ID" in page


def test_id_that_never_pressed_start_is_refused(seeded, monkeypatch):
    c, tid = seeded
    _use(monkeypatch, Bot(refuse=True))
    r = c.post("/app/settings/demo-phone-id", data={"telegram_id": "123456789"}, headers=SAME)
    assert "message that ID yet" in r.text and "press Start" in r.text and _demo_chat(tid) is None


def test_bad_input_and_missing_bot(seeded, monkeypatch):
    c, tid = seeded
    _use(monkeypatch, Bot())
    for bad in ("@someone", "12ab", "1"):
        assert "look like a Telegram ID" in c.post("/app/settings/demo-phone-id", data={"telegram_id": bad},
                                                           headers=SAME).text
    _use(monkeypatch, None)
    assert "Connect your Telegram bot first" in c.post("/app/settings/demo-phone-id", data={"telegram_id": "123456789"},
                                                        headers=SAME).text
    assert _demo_chat(tid) is None
