"""Writes a Dataset (generated, or built from uploaded CSVs) into the database for one business, replacing
that business's old data. Dataset rows use 1-based local ids; they are shifted past existing ids here."""

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Connection

from app.data.generator import Dataset
from app.db.models import BrowseDaily, Cart, Customer, CustomerFeature, Deal, OfferDecision, OfferOutcome, Order, \
    CustomerPreference, OrderItem, PointsLedger, Product, TelegramUser

# Insert order (parents first). Value = {column: parent table} for foreign keys to remap.
TABLES = [
    (Customer, "customers", {}),
    (Product, "products", {}),
    (Order, "orders", {"customer_id": Customer}),
    (OrderItem, "order_items", {"order_id": Order, "product_id": Product}),
    (Cart, "carts", {"customer_id": Customer, "order_id": Order}),
    (BrowseDaily, "browse_daily", {"customer_id": Customer}),
    (PointsLedger, "points_ledger", {"customer_id": Customer, "order_id": Order}),
]
CHUNK = 5000


def load_dataset(conn: Connection, tenant_id: int, ds: Dataset, progress=print) -> dict[str, int]:
    if conn.dialect.name == "postgresql":
        # Ids below are computed as max(id) + n, so two businesses importing at once must not interleave.
        conn.execute(text("select pg_advisory_xact_lock(424242)"))
    # Customers are about to be replaced: drop their decisions/deals and unlink chats (users /start again).
    conn.execute(delete(OfferOutcome).where(OfferOutcome.tenant_id == tenant_id))
    conn.execute(delete(OfferDecision).where(OfferDecision.tenant_id == tenant_id))
    conn.execute(delete(Deal).where(Deal.tenant_id == tenant_id))
    conn.execute(update(TelegramUser).where(TelegramUser.tenant_id == tenant_id).values(customer_id=None))
    conn.execute(delete(CustomerFeature).where(CustomerFeature.tenant_id == tenant_id))
    conn.execute(delete(CustomerPreference).where(CustomerPreference.tenant_id == tenant_id))
    for model, _, _ in reversed(TABLES):
        conn.execute(delete(model).where(model.tenant_id == tenant_id))

    # Generated ids are 1-based per dataset; shift them past ids used by other tenants.
    offsets = {model: conn.scalar(select(func.coalesce(func.max(model.id), 0))) for model, _, _ in TABLES}
    counts = {}
    for model, attr, fks in TABLES:
        rows = getattr(ds, attr)
        off = offsets[model]
        batch = []
        for r in rows:
            row = dict(r, id=r["id"] + off, tenant_id=tenant_id)
            for col, parent in fks.items():
                if row[col] is not None:
                    row[col] += offsets[parent]
            batch.append(row)
            if len(batch) == CHUNK:
                conn.execute(model.__table__.insert(), batch)
                batch = []
        if batch:
            conn.execute(model.__table__.insert(), batch)
        counts[attr] = len(rows)
        progress(f"  {attr:<14} {len(rows):>8,} rows")

    if conn.dialect.name == "postgresql":
        # Explicit ids bypass the sequences; move them past the max so later inserts don't collide.
        for model, _, _ in TABLES:
            t = model.__tablename__
            conn.execute(text(
                f"select setval(pg_get_serial_sequence('{t}', 'id'), coalesce((select max(id) from {t}), 1))"
            ))
    return counts
