"""Simulate daily offer campaigns over synthetic customers and measure them against the holdout group.

    python scripts/simulate_campaign.py --tenant shopkart                     # 7 days at the real daily budget
    python scripts/simulate_campaign.py --tenant shopkart --budget 2000000    # fund every eligible offer
    python scripts/simulate_campaign.py --tenant shopkart --report-only
    python scripts/simulate_campaign.py --tenant shopkart --reset             # delete simulated decisions first

Simulated decisions are flagged and never count toward real users' limits or the live budget. Groq is never
called. Customers linked to real Telegram chats are left out.
"""

import argparse
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.data.generator import IST  # noqa: E402
from app.db.models import OfferDecision, OfferOutcome, Tenant  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.measurement.metrics import format_report, measure  # noqa: E402
from app.offers.campaign import run_simulated_campaign  # noqa: E402
from app.rules.policy import current_policy  # noqa: E402
from app.tenants.registry import get_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="first campaign day (default today)")
    ap.add_argument("--segment", default=None)
    ap.add_argument("--budget", type=int, default=None, help="override the daily budget for this simulation")
    ap.add_argument("--reset", action="store_true", help="delete earlier simulated decisions first")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    cfg = get_tenants()[args.tenant]
    policy = current_policy(args.tenant, cfg.path)  # includes rules saved from the dashboard
    with session_scope() as s:
        tenant_id = s.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        if args.reset:
            sim_ids = select(OfferDecision.id).where(OfferDecision.tenant_id == tenant_id,
                                                     OfferDecision.simulated.is_(True))
            s.execute(delete(OfferOutcome).where(OfferOutcome.decision_id.in_(sim_ids)))
            s.execute(delete(OfferDecision).where(OfferDecision.tenant_id == tenant_id,
                                                  OfferDecision.simulated.is_(True)))
            print("Deleted earlier simulated decisions.")

    if not args.report_only:
        start = args.start or datetime.now(IST).date()
        budget = args.budget or policy.rules.limits.daily_budget_rs
        print(f"Simulating {args.days} daily campaigns from {start} (11:00 IST), budget Rs {budget:,}/day, "
              f"holdout {policy.rules.measurement.holdout_pct:g}%\n")
        for i in range(args.days):
            now = datetime.combine(start + timedelta(days=i), dtime(11), IST)
            t0 = time.time()
            with session_scope() as s:
                r = run_simulated_campaign(s, args.tenant, tenant_id, policy, cfg.name, cfg.prefix,
                                           now, args.segment, seed=i, daily_budget=args.budget)
            top = ", ".join(f"{k} {v}" for k, v in r.by_offer.most_common(4))
            print(f"{now:%a %d %b}: {r.eligible:,} eligible, sent {r.sent:,} (worst-case Rs {r.liability:,}), "
                  f"holdout {r.holdout}, budget ran out for {r.budget_stopped:,} [{time.time() - t0:.0f}s]  {top}")
        print()

    with get_engine().connect() as conn:
        report = measure(conn, tenant_id)
    print(format_report(report, {o.key: o.label for o in policy.rules.offers},
                        {s.key: s.label for s in policy.segments.segments}))


if __name__ == "__main__":
    main()
