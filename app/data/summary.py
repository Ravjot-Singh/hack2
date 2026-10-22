"""Plain-text realism report for a generated Dataset."""

from collections import Counter, defaultdict

import numpy as np

from app.data.generator import Dataset


def realism_metrics(ds: Dataset) -> dict:
    seg_of = {c["id"]: c["true_segment"] for c in ds.customers}
    delivered = [o for o in ds.orders if o["status"] == "delivered"]
    spend = defaultdict(int)
    for o in delivered:
        spend[o["customer_id"]] += o["net_amount"]
    spends = np.sort(np.array([spend.get(c["id"], 0) for c in ds.customers]))[::-1]
    top20 = spends[: max(1, len(spends) // 5)].sum() / max(spends.sum(), 1)

    orders_per_cust = Counter(o["customer_id"] for o in ds.orders if o["status"] != "cancelled")
    buyers = len(orders_per_cust)
    repeat = sum(1 for v in orders_per_cust.values() if v >= 2) / max(buyers, 1)

    carts = Counter(c["status"] for c in ds.carts)
    abandon = carts["abandoned"] / max(sum(carts.values()), 1)

    items_by_cat, returned_by_cat = Counter(), Counter()
    for it in ds.order_items:
        items_by_cat[it["category"]] += 1
        returned_by_cat[it["category"]] += it["returned"]
    return_rates = {c: returned_by_cat[c] / items_by_cat[c] for c in items_by_cat}

    by_seg = defaultdict(lambda: {"customers": 0, "orders": 0, "revenue": 0})
    for c in ds.customers:
        by_seg[c["true_segment"]]["customers"] += 1
    for o in delivered:
        s = by_seg[seg_of[o["customer_id"]]]
        s["orders"] += 1
        s["revenue"] += o["net_amount"]

    monthly = Counter(o["created_at"].strftime("%Y-%m") for o in ds.orders)
    pay = Counter(o["payment_method"] for o in ds.orders)
    return {
        "customers": len(ds.customers),
        "orders": len(ds.orders),
        "revenue": int(sum(o["net_amount"] for o in delivered)),
        "aov": float(np.mean([o["net_amount"] for o in delivered])) if delivered else 0.0,
        "top20_revenue_share": float(top20),
        "buyers_share": buyers / max(len(ds.customers), 1),
        "repeat_rate": repeat,
        "cart_abandonment": abandon,
        "return_rates": return_rates,
        "cancel_rate": sum(o["status"] == "cancelled" for o in ds.orders) / max(len(ds.orders), 1),
        "by_segment": dict(by_seg),
        "monthly_orders": dict(sorted(monthly.items())),
        "payment_mix": {k: v / max(len(ds.orders), 1) for k, v in pay.items()},
        "points_outstanding": int(sum(c["points_balance"] for c in ds.customers)),
        "loyalty_tiers": dict(Counter(c["loyalty_tier"] for c in ds.customers)),
    }


def format_report(m: dict) -> str:
    lines = [
        f"Customers {m['customers']:,} | orders {m['orders']:,} | revenue Rs {m['revenue']:,} | AOV Rs {m['aov']:,.0f}",
        f"Bought at least once {m['buyers_share']:.0%} | repeat buyers {m['repeat_rate']:.0%} | "
        f"top 20% of customers = {m['top20_revenue_share']:.0%} of revenue",
        f"Cart abandonment {m['cart_abandonment']:.0%} | cancellations {m['cancel_rate']:.1%} | "
        f"points outstanding {m['points_outstanding']:,}",
        "Payment mix: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(m["payment_mix"].items(), key=lambda x: -x[1])),
        "Return rate by category: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(m["return_rates"].items())),
        "Loyalty tiers: " + ", ".join(f"{k} {v}" for k, v in sorted(m["loyalty_tiers"].items())),
        "",
        f"{'segment':<18}{'customers':>10}{'orders/cust':>13}{'revenue share':>15}",
    ]
    total_rev = max(m["revenue"], 1)
    for seg, s in sorted(m["by_segment"].items(), key=lambda x: -x[1]["revenue"]):
        lines.append(f"{seg:<18}{s['customers']:>10,}{s['orders'] / max(s['customers'], 1):>13.1f}"
                     f"{s['revenue'] / total_rev:>15.0%}")
    lines += ["", "Orders per month:"]
    peak = max(m["monthly_orders"].values())
    for month, n in m["monthly_orders"].items():
        lines.append(f"  {month} {'#' * round(40 * n / peak):<40} {n:,}")
    return "\n".join(lines)
