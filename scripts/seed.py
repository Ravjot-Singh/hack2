"""Generate synthetic data for a tenant and load it into the database.

    python scripts/seed.py --tenant shopkart                 # 5,000 customers, seed 42, history up to yesterday
    python scripts/seed.py --tenant shopkart --dry-run       # generate + print the realism report only
    python scripts/seed.py --tenant shopkart --as-of 2026-10-22 --customers 1000
"""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.data.generator import generate  # noqa: E402
from app.data.loader import load_dataset  # noqa: E402
from app.data.summary import format_report, realism_metrics  # noqa: E402
from app.db.models import Tenant  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.tenants.registry import TEMPLATE_DIR, get_tenants, sync_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--customers", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="history ends the day before (default today)")
    ap.add_argument("--dry-run", action="store_true", help="do not write to the database")
    args = ap.parse_args()

    tenants = get_tenants()
    if args.tenant not in tenants:
        sys.exit(f"Unknown tenant '{args.tenant}'. Known: {', '.join(tenants)}")
    cfg = tenants[args.tenant]

    t0 = time.time()
    ds = generate((cfg.path or TEMPLATE_DIR) / "catalog.yaml", args.customers, args.seed, args.as_of,
                  ref_prefix=cfg.prefix)
    last_day = ds.as_of - timedelta(days=1)
    print(f"Generated in {time.time() - t0:.1f}s (history {ds.start} .. {last_day}, seed {ds.seed})\n")
    print(format_report(realism_metrics(ds)))
    if args.dry_run:
        return

    with session_scope() as s:
        sync_tenants(s, tenants)
    t0 = time.time()
    print(f"\nLoading into {get_engine().dialect.name} ...")
    with get_engine().begin() as conn:
        tenant_id = conn.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        load_dataset(conn, tenant_id, ds)
    print(f"Loaded in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
