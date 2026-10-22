"""Compute features + segments for a tenant and save them as the customer_features snapshot."""

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.engine import Connection

from app.db.models import CustomerFeature
from app.features.customer import compute_features
from app.features.segments import SegmentConfig, assign_segment

COLUMNS = {c.name for c in CustomerFeature.__table__.columns}


def refresh_features(conn: Connection, tenant_id: int, as_of: datetime, cfg: SegmentConfig,
                     customer_ids: list[int] | None = None) -> list[dict]:
    feats = compute_features(conn, tenant_id, as_of, customer_ids)
    rows = []
    for f in feats.values():
        segment, reason = assign_segment(f, cfg)
        row = {k: v for k, v in f.items() if k in COLUMNS}
        row.update(tenant_id=tenant_id, computed_at=as_of, segment=segment, segment_reason=reason)
        rows.append(row)

    stmt = delete(CustomerFeature).where(CustomerFeature.tenant_id == tenant_id)
    if customer_ids is not None:
        stmt = stmt.where(CustomerFeature.customer_id.in_(customer_ids))
    conn.execute(stmt)
    for i in range(0, len(rows), 2000):
        conn.execute(CustomerFeature.__table__.insert(), rows[i:i + 2000])
    return rows
