"""Compute customer features, scores and segments for a tenant and save the snapshot.

    python scripts/build_features.py --tenant shopkart

Also prints how well the rule-based segments recover the generator's hidden personas
(evaluation only: the offer engine never sees the hidden persona).
"""

import argparse
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.data.generator import IST  # noqa: E402
from app.db.models import Customer, Tenant  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.features.segments import load_segment_config  # noqa: E402
from app.features.store import refresh_features  # noqa: E402
from app.tenants.registry import get_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--as-of", type=datetime.fromisoformat, default=None, help="default: now (IST)")
    args = ap.parse_args()

    cfg_t = get_tenants()[args.tenant]
    seg_cfg = load_segment_config(cfg_t.path / "segments.yaml")
    as_of = (args.as_of.replace(tzinfo=IST) if args.as_of else datetime.now(IST))

    t0 = time.time()
    with get_engine().begin() as conn:
        tenant_id = conn.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        rows = refresh_features(conn, tenant_id, as_of, seg_cfg)
        truth = dict(conn.execute(select(Customer.id, Customer.true_segment).where(Customer.tenant_id == tenant_id)).all())
    print(f"Saved features for {len(rows):,} customers in {time.time() - t0:.1f}s (as of {as_of:%Y-%m-%d %H:%M})\n")

    counts = Counter(r["segment"] for r in rows)
    print(f"{'segment':<20}{'customers':>10}{'avg churn':>11}{'buy anyway':>12}{'deal aff.':>11}")
    by_seg = defaultdict(list)
    for r in rows:
        by_seg[r["segment"]].append(r)
    for s in seg_cfg.segments:
        rs = by_seg.get(s.key, [])
        if rs:
            avg = lambda k: sum(r[k] for r in rs) / len(rs)  # noqa: E731
            print(f"{s.label:<20}{counts[s.key]:>10,}{avg('churn_risk'):>11.0%}{avg('buy_anyway_prob'):>12.0%}"
                  f"{avg('deal_affinity'):>11.0%}")

    keys = [s.key for s in seg_cfg.segments]
    matrix = defaultdict(Counter)
    for r in rows:
        matrix[truth[r["customer_id"]]][r["segment"]] += 1
    agree = sum(matrix[k][k] for k in keys)
    print(f"\nHidden persona (rows) vs assigned segment (columns) - agreement {agree / len(rows):.0%}")
    short = [k[:7] for k in keys]
    print(f"{'':<18}" + "".join(f"{s:>8}" for s in short))
    for true_seg in keys:
        if matrix[true_seg]:
            print(f"{true_seg:<18}" + "".join(f"{matrix[true_seg][k]:>8}" for k in keys))


if __name__ == "__main__":
    main()
