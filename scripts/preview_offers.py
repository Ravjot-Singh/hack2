"""Dry-run the rules engine over every customer's feature snapshot (nothing is sent).

    python scripts/preview_offers.py --tenant shopkart
    python scripts/preview_offers.py --tenant shopkart --customer 17      # one customer in detail
    python scripts/preview_offers.py --tenant shopkart --at "2026-10-22 23:00"

Shows which offer each segment would get, why offers are blocked, and the total worst-case cost.
Assumes nobody has had an offer yet and the daily budget is unused, unless --budget-used is given.
"""

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.data.generator import IST  # noqa: E402
from app.db.models import Customer, CustomerFeature, Tenant  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.rules.engine import CustomerContext, evaluate  # noqa: E402
from app.rules.policy import current_policy  # noqa: E402
from app.tenants.registry import get_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--customer", type=int, help="local customer number, e.g. 17 for SH00017")
    ap.add_argument("--at", type=datetime.fromisoformat, default=None, help="pretend time (IST), default now")
    ap.add_argument("--budget-used", type=float, default=0)
    args = ap.parse_args()

    policy = current_policy(args.tenant, get_tenants()[args.tenant].path)
    now = args.at.replace(tzinfo=IST) if args.at else datetime.now(IST)

    with get_engine().connect() as conn:
        tenant_id = conn.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        q = select(CustomerFeature.__table__, Customer.external_ref, Customer.name).join(
            Customer, Customer.id == CustomerFeature.customer_id).where(CustomerFeature.tenant_id == tenant_id)
        if args.customer:
            q = q.where(Customer.external_ref == f"{get_tenants()[args.tenant].prefix}{args.customer:05d}")
        rows = conn.execute(q).all()
    if not rows:
        sys.exit("No feature snapshot found. Run scripts/build_features.py first.")

    cols = [c.name for c in CustomerFeature.__table__.columns]
    decisions = []
    for row in rows:
        m = row._mapping
        f = {c: m[c] for c in cols}
        d = evaluate(policy.rules, policy.catalog, CustomerContext(f["customer_id"], f, f["segment"]),
                     args.budget_used, now)
        decisions.append((m["external_ref"], m["name"], f, d))

    if args.customer:
        ref, name, f, d = decisions[0]
        print(f"{ref} {name} - {policy.segments.label(d.segment)} ({f['segment_reason']})")
        print(f"churn {f['churn_risk']:.0%}, buy anyway {f['buy_anyway_prob']:.0%}, deal affinity "
              f"{f['deal_affinity']:.0%}, top category {f['top_category']}\n")
        if d.customer_block:
            print("NO OFFER:", d.customer_block)
        if d.send_after:
            print(f"Quiet hours: would be sent after {d.send_after:%d %b %H:%M}")
        for c in d.eligible:
            print(f"ELIGIBLE  [{c.priority}] {c.label}: {c.headline} ({c.terms})")
            print(f"          worst-case cost Rs {c.liability_rs}, expected net Rs {c.expected_net_rs:+.0f}; "
                  f"{'; '.join(c.reasons) or 'open to all'}")
        for b in d.blocked:
            print(f"blocked   {b.label}: {b.reason}")
        return

    by_seg = defaultdict(Counter)
    block_reasons = Counter()
    liability = 0
    for _, _, _, d in decisions:
        by_seg[d.segment][d.best.label if d.best else "(no offer)"] += 1
        liability += d.best.liability_rs if d.best else 0
        for b in d.blocked:
            if b.reason.startswith("Only for"):  # wrong segment: expected, not interesting
                continue
            block_reasons[f"{b.label}: {b.reason.split(' (')[0].split(':')[0]}"] += 1
    got = sum(1 for *_, d in decisions if d.best)
    print(f"{got:,} of {len(decisions):,} customers would get an offer; worst-case cost if all are used "
          f"Rs {liability:,} (daily budget Rs {policy.rules.limits.daily_budget_rs:,} caps what is actually sent).\n")
    for s in policy.segments.segments:
        if by_seg[s.key]:
            top = ", ".join(f"{k} {v}" for k, v in by_seg[s.key].most_common())
            print(f"{s.label:<18} {top}")
    print("\nMost common reasons offers were blocked:")
    for reason, n in block_reasons.most_common(12):
        print(f"  {n:>5}  {reason}")


if __name__ == "__main__":
    main()
