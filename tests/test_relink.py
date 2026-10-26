"""Connected Telegram chats stay connected when a business's data is regenerated or re-uploaded."""

from datetime import datetime, time

from sqlalchemy import select

from app.config import TENANTS_DIR
from app.data.generator import IST, generate
from app.data.loader import load_dataset
from app.db.models import Customer, Tenant, TelegramUser
from app.db.session import get_engine, session_scope
from app.ingest.csv_import import parse_files
from tests.test_portal_data import good_files

NOW = datetime.combine(datetime.now(IST).date(), time(12), IST)


def _tenant():
    with get_engine().begin() as conn:
        conn.execute(Tenant.__table__.insert(), [{"slug": "shopkart", "name": "ShopKart", "coupon_prefix": "SH"}])
    with session_scope() as s:
        return s.scalar(select(Tenant.id))


def _load(tid, ds):
    with get_engine().begin() as conn:
        return load_dataset(conn, tid, ds, progress=lambda *_: None)


def _demo(n=60, seed=1):
    return generate(TENANTS_DIR / "shopkart" / "catalog.yaml", n_customers=n, seed=seed, as_of=NOW.date())


def _link(tid, chat_id, ref):
    with session_scope() as s:
        cid = s.scalar(select(Customer.id).where(Customer.tenant_id == tid, Customer.external_ref == ref))
        s.add(TelegramUser(tenant_id=tid, chat_id=chat_id, customer_id=cid, opted_out=False))


def _linked(chat_id):
    with session_scope() as s:
        row = s.execute(select(Customer.external_ref, Customer.true_segment)
                        .join(TelegramUser, TelegramUser.customer_id == Customer.id)
                        .where(TelegramUser.chat_id == chat_id)).first()
        return tuple(row) if row else None


def test_demo_regeneration_keeps_the_chat_connected():
    tid = _tenant()
    _load(tid, _demo(seed=1))
    _link(tid, 111, "SK00007")
    counts = _load(tid, _demo(seed=2))  # brand-new synthetic shoppers, same refs
    assert counts["relinked_chats"] == 1
    ref, true_segment = _linked(111)
    assert ref == "SK00007" and true_segment is not None


def test_smaller_dataset_gives_another_demo_shopper():
    tid = _tenant()
    _load(tid, _demo(n=60))
    _link(tid, 222, "SK00050")
    _load(tid, _demo(n=20, seed=3))  # SK00050 no longer exists
    ref, true_segment = _linked(222)
    assert ref != "SK00050" and true_segment is not None


def test_real_customers_are_never_handed_to_a_demo_chat():
    tid = _tenant()
    _load(tid, _demo())
    _link(tid, 333, "SK00001")
    real = parse_files(good_files(), NOW)
    counts = _load(tid, real.dataset)  # the business uploads its own customers
    assert counts["relinked_chats"] == 0 and _linked(333) is None


def test_invite_linked_real_customer_stays_linked_after_reupload():
    tid = _tenant()
    real = parse_files(good_files(), NOW).dataset
    _load(tid, real)
    _link(tid, 444, "C5")
    _load(tid, parse_files(good_files(), NOW).dataset)
    assert _linked(444) == ("C5", None)
