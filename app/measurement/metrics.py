"""Did the offers work? Compare the treatment group with the holdout group.

Treatment = customers who were sent an offer. Holdout = customers randomly held back who would have been sent one
by the same rule (see app/offers/campaign.py), so the two groups are like-for-like. Per offer, customers are
grouped by the best offer the rules found for them, which is known for the holdout too.

  uplift            = conversion(treatment) - conversion(holdout), with a 95% confidence interval
  incremental profit = (margin - discounts) per customer, treatment minus holdout, times treatment size
  return per Rs      = incremental profit / discounts paid
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.db.models import OfferDecision, OfferOutcome


@dataclass
class Group:
    n: int = 0
    buyers: int = 0
    revenue: int = 0
    margin: int = 0
    discounts: int = 0
    sent: int = 0
    redeemed: int = 0
    expected_net: float = 0.0

    def add(self, purchased, order_value, margin, discount, sent, redeemed, expected_net):
        self.n += 1
        self.buyers += purchased
        self.revenue += order_value
        self.margin += margin
        self.discounts += discount
        self.sent += sent
        self.redeemed += redeemed
        self.expected_net += expected_net if sent else 0

    @property
    def conv(self) -> float:
        return self.buyers / self.n if self.n else 0.0

    @property
    def net_per_customer(self) -> float:
        return (self.margin - self.discounts) / self.n if self.n else 0.0


@dataclass
class Comparison:
    name: str
    treat: Group = field(default_factory=Group)
    hold: Group = field(default_factory=Group)

    @property
    def uplift(self) -> float:
        return self.treat.conv - self.hold.conv

    @property
    def ci95(self) -> float:
        t, h = self.treat, self.hold
        if not t.n or not h.n:
            return float("nan")
        return 1.96 * math.sqrt(t.conv * (1 - t.conv) / t.n + h.conv * (1 - h.conv) / h.n)

    @property
    def incremental_profit(self) -> float:
        return (self.treat.net_per_customer - self.hold.net_per_customer) * self.treat.n

    @property
    def return_per_rs(self) -> float:
        return self.incremental_profit / self.treat.discounts if self.treat.discounts else float("nan")

    @property
    def significant(self) -> bool:
        return not math.isnan(self.ci95) and abs(self.uplift) > self.ci95


@dataclass
class Report:
    overall: Comparison
    by_offer: dict[str, Comparison]
    by_segment: dict[str, Comparison]
    live: dict


def measure(conn: Connection, tenant_id: int, since: datetime | None = None) -> Report:
    q = (select(OfferDecision.outcome, OfferDecision.shortlist, OfferDecision.segment, OfferDecision.expected_net_rs,
                OfferOutcome.purchased, OfferOutcome.redeemed, OfferOutcome.order_value, OfferOutcome.margin_rs,
                OfferOutcome.discount_cost)
         .join(OfferOutcome, OfferOutcome.decision_id == OfferDecision.id)
         .where(OfferDecision.tenant_id == tenant_id))
    if since:
        q = q.where(OfferDecision.created_at >= since)
    overall = Comparison("all offers")
    by_offer: dict[str, Comparison] = defaultdict(lambda: Comparison(""))
    by_seg: dict[str, Comparison] = defaultdict(lambda: Comparison(""))
    for outcome, shortlist, seg, exp_net, bought, redeemed, value, margin, cost in conn.execute(q):
        best = (shortlist or ["?"])[0]
        args = (bool(bought), value, margin, cost, outcome == "sent", bool(redeemed), exp_net or 0.0)
        for comp in (overall, by_offer[best], by_seg[seg]):
            (comp.hold if outcome == "holdout" else comp.treat).add(*args)
    for k, c in by_offer.items():
        c.name = k
    for k, c in by_seg.items():
        c.name = k

    live_q = select(OfferDecision.outcome, OfferDecision.response, OfferDecision.chosen_by,
                    OfferDecision.ai_rejected_reason).where(OfferDecision.tenant_id == tenant_id,
                                                            OfferDecision.simulated.is_(False))
    live = defaultdict(int)
    for outcome, response, chosen_by, rejected in conn.execute(live_q):
        live["decisions"] += 1
        live[f"outcome_{outcome}"] += 1
        if outcome == "sent":
            live[f"chosen_by_{chosen_by}"] += 1
            live["claimed"] += response == "claimed"
            live["dismissed"] += response == "dismissed"
            live["ai_rejected"] += bool(rejected and rejected.startswith("AI answer rejected"))
    return Report(overall, dict(by_offer), dict(by_seg), dict(live))


def _row(c: Comparison, label: str) -> str:
    sig = "*" if c.significant else " "
    rpr = "" if math.isnan(c.return_per_rs) else f"{c.return_per_rs:>7.2f}"
    ci = "  n/a" if math.isnan(c.ci95) else f"{c.ci95:>5.1%}"
    return (f"{label:<20}{c.treat.n:>7,}{c.hold.n:>7,}{c.treat.conv:>8.1%}{c.hold.conv:>8.1%}"
            f"{c.uplift:>+8.1%}{sig}±{ci}{c.incremental_profit:>14,.0f}{c.treat.discounts:>11,}{rpr:>8}")


def format_report(r: Report, offer_labels: dict[str, str], segment_labels: dict[str, str]) -> str:
    head = (f"{'':<20}{'treated':>7}{'hold':>7}{'buy %':>8}{'hold %':>8}{'uplift':>9}{'±95%':>7}"
            f"{'extra profit':>14}{'discounts':>11}{'Rs/Rs':>8}")
    o = r.overall
    lines = [
        "WHAT THE OFFERS ADDED (treatment vs holdout; * = uplift is statistically clear)",
        head, _row(o, "All offers"), "",
        "By the offer the rules picked:", head,
        *[_row(c, offer_labels.get(k, k)) for k, c in sorted(r.by_offer.items(), key=lambda x: -x[1].treat.n)],
        "", "By segment:", head,
        *[_row(c, segment_labels.get(k, k)) for k, c in sorted(r.by_segment.items(), key=lambda x: -x[1].treat.n)],
        "",
        f"Offers sent {o.treat.sent:,}; redeemed {o.treat.redeemed:,} "
        f"({o.treat.redeemed / max(o.treat.sent, 1):.0%}); discounts paid Rs {o.treat.discounts:,}.",
        f"Engine's expected profit for sent offers: Rs {o.treat.expected_net:,.0f}; "
        f"measured extra profit: Rs {o.incremental_profit:,.0f}.",
    ]
    if r.live.get("decisions"):
        lv = r.live
        sent = lv.get("outcome_sent", 0)
        lines += ["", "REAL TELEGRAM USERS",
                  f"Requests {lv['decisions']}, offers sent {sent}, claimed {lv.get('claimed', 0)} "
                  f"({lv.get('claimed', 0) / max(sent, 1):.0%}), dismissed {lv.get('dismissed', 0)}; "
                  f"written by AI {lv.get('chosen_by_ai', 0)}, AI answers rejected by validator "
                  f"{lv.get('ai_rejected', 0)}; held back for holdout {lv.get('outcome_holdout', 0)}."]
    return "\n".join(lines)
