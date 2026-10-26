"""Rules ranking vs the learning engine (bandit), on identical synthetic customers.

    python scripts/compare_policies.py                      # 2,000 customers, 14 daily campaigns, 3 worlds
    python scripts/compare_policies.py --customers 5000 --days 21 --repeats 5

Runs in a throwaway SQLite database (never Neon, Telegram or Groq). Two copies of the same customers get the
same daily campaigns and the same budget; the only difference is how the offer is picked among the ones the rules
allow: the rules' fixed ranking, or the bandit that learns from each day's (simulated) results.

Scored on the TRUE expected extra profit of every offer sent (the simulator knows each customer's hidden
response), so the comparison isn't decided by luck; repeated over several random customer populations.
"""

import argparse
import os
import statistics
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

DB = Path(tempfile.gettempdir()) / "offer_engine_compare.db"
os.environ.update(DATABASE_URL=f"sqlite:///{DB}", PUBLIC_BASE_URL="", LLM_ENABLED="false", GROQ_API_KEY="")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import TENANTS_DIR  # noqa: E402
from app.data.generator import IST, generate  # noqa: E402
from app.data.loader import load_dataset  # noqa: E402
from app.db.models import Base, Customer, CustomerFeature, OfferDecision, Tenant  # noqa: E402
from app.db.session import _session_factory, get_engine, session_scope  # noqa: E402
from app.features.store import refresh_features  # noqa: E402
from app.learning import bandit  # noqa: E402
from app.measurement.outcomes import expected_gain  # noqa: E402
from app.offers.campaign import run_simulated_campaign  # noqa: E402
from app.rules.policy import load_policy  # noqa: E402
from app.tenants.registry import catalog_from_file  # noqa: E402

SHOPKART = TENANTS_DIR / "shopkart"
MODES = ("rules", "bandit")


def _policies():
    base = load_policy(SHOPKART)
    return {m: type(base)(base.rules.model_copy(update={"learning": base.rules.learning.model_copy(
        update={"mode": m})}), base.catalog, base.segments) for m in MODES}


def run_world(customers: int, days: int, seed: int) -> dict[str, dict]:
    get_engine().dispose()
    for cached in (get_engine, _session_factory):
        cached.cache_clear()
    DB.unlink(missing_ok=True)
    Base.metadata.create_all(get_engine())
    bandit.clear_cache()
    policies = _policies()
    start = datetime.now(IST).date()
    ds = generate(SHOPKART / "catalog.yaml", n_customers=customers, seed=seed, as_of=start)
    ids = {}
    with session_scope() as s:
        for m in MODES:
            t = Tenant(slug=f"compare-{m}", name=m, coupon_prefix=m[:2].upper(),
                       catalog=catalog_from_file(SHOPKART / "catalog.yaml"),
                       segments_yaml=(SHOPKART / "segments.yaml").read_text(encoding="utf-8"))
            s.add(t)
            s.flush()
            ids[m] = t.id
    first = datetime.combine(start, dtime(11), IST)
    for tid in ids.values():
        with get_engine().begin() as conn:
            load_dataset(conn, tid, ds, progress=lambda *_: None)
            refresh_features(conn, tid, first, policies["rules"].segments)
    for day in range(days):
        for m, tid in ids.items():
            with session_scope() as s:  # same slug: same control group and the same random draws per customer
                run_simulated_campaign(s, f"compare-{seed}", tid, policies[m], "ShopKart", "SK",
                                       first + timedelta(days=day), seed=day)
    return {m: _score(tid, policies[m]) for m, tid in ids.items()}


def _score(tenant_id: int, policy) -> dict:
    lim = policy.rules.limits
    cols = [c.name for c in CustomerFeature.__table__.columns]
    gain = cost = 0.0
    sent = 0
    with session_scope() as s:
        for d, feat, true_seg, sens in s.execute(
                select(OfferDecision, CustomerFeature, Customer.true_segment, Customer.price_sensitivity)
                .join(CustomerFeature, CustomerFeature.customer_id == OfferDecision.customer_id)
                .join(Customer, Customer.id == OfferDecision.customer_id)
                .where(OfferDecision.tenant_id == tenant_id, OfferDecision.outcome == "sent")):
            f = {c: getattr(feat, c) for c in cols}
            offer = {"key": d.offer_key, "type": _type(d), "percent": d.percent, "amount_rs": d.amount_rs,
                     "points": d.points, "max_discount_rs": d.max_discount_rs, "min_order_value": d.min_order_value}
            window = max(1.0, (d.valid_until - d.created_at).total_seconds() / 3600)
            margin = policy.catalog.margins.get(d.category) or policy.catalog.avg_margin
            gain += expected_gain(features=f, true_segment=true_seg, price_sensitivity=sens or 0.5,
                                  window_hours=window, offer=offer, margin_pct=margin,
                                  delivery_fee=lim.delivery_fee_rs, point_value=lim.point_value_rs)
            cost += d.liability_rs
            sent += 1
    return {"gain": gain, "sent": sent, "liability": cost}


def _type(d) -> str:
    if d.percent is not None:
        return "percent_off"
    if d.amount_rs:
        return "flat_off"
    if d.points:
        return "bonus_points"
    return "free_delivery"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", type=int, default=2000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    print(f"{args.customers:,} customers x {args.days} daily campaigns at the real budget, "
          f"{args.repeats} random worlds\n")
    diffs = []
    print(f"{'world':>6}{'rules':>14}{'bandit':>14}{'difference':>14}   (true expected extra profit, Rs)")
    for i in range(args.repeats):
        r = run_world(args.customers, args.days, seed=100 + i)
        diff = r["bandit"]["gain"] - r["rules"]["gain"]
        diffs.append((diff, r))
        print(f"{i + 1:>6}{r['rules']['gain']:>14,.0f}{r['bandit']['gain']:>14,.0f}{diff:>+14,.0f}"
              f"   sent {r['rules']['sent']:,} vs {r['bandit']['sent']:,}")
    mean_rules = statistics.mean(x[1]["rules"]["gain"] for x in diffs)
    mean_diff = statistics.mean(d for d, _ in diffs)
    print(f"\nLearning engine vs fixed rules: {mean_diff:+,.0f} per world on average "
          f"({100 * mean_diff / mean_rules:+.0f}%), better in {sum(d > 0 for d, _ in diffs)} of {len(diffs)} worlds.")


if __name__ == "__main__":
    main()
