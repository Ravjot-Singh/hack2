"""Fairness across customer groups: who gets offers, and how generous they are.

Groups (only where the business's data has the value; unknown is left out, never guessed):
  city tier    metro / large city / smaller town (small towns shouldn't be left out)
  gender       women / men
  age band     under 25 / 25–40 / over 40
  tenure       new (under 3 months) / long-time

Two measures per group, as a share of the overall figure (1.0 = same as everyone):
  offer rate   offered ÷ customers in the audience
  generosity   average discount as a % of their usual order

The engine never uses these traits to decide *whether* an offer is allowed. They are used only to rebalance a
deal's or campaign's budget: when a group's offer rate is below `fairness.min_ratio` (default 80%, the
"four-fifths rule"), the least valuable funded offers of over-served groups are swapped for the best allowed but
unfunded offers in the under-served group, until it reaches the threshold or no allowed offers are left (then
the gap is shown as "limited by rules"). Groups smaller than `min_group_size` are not judged.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from app.data.city_tiers import tier_for
from app.rules.engine import Candidate
from app.rules.policy import TenantPolicy

KINDS = {"city_tier": "City", "gender": "Gender", "age_band": "Age", "tenure": "Customer for"}
TIER_LABELS = {1: "Metro", 2: "Large city", 3: "Smaller town"}


def attributes(city: str | None, city_tier: int | None, gender: str | None, age: int | None,
               tenure_days: int | None) -> dict[str, str | None]:
    tier = city_tier or tier_for(city)
    return {
        "city_tier": TIER_LABELS.get(tier),
        "gender": {"F": "Women", "M": "Men"}.get((gender or "").upper()),
        "age_band": None if age is None else "Under 25" if age < 25 else "25–40" if age <= 40 else "Over 40",
        "tenure": None if tenure_days is None else "New (under 3 months)" if tenure_days < 90 else "Long-time",
    }


def discount_share(policy: TenantPolicy, c: Candidate, f: dict) -> float:
    """Estimated discount as a % of the customer's usual order."""
    lim = policy.rules.limits
    order = max(f.get("aov") or 600, c.min_order_value or 0, 1)
    if c.type == "percent_off":
        rs = min(order * (c.percent or 0) / 100, c.max_discount_rs or lim.max_discount_rs_per_offer)
    elif c.type == "flat_off":
        rs = c.amount_rs or 0
    elif c.type == "free_delivery":
        rs = lim.delivery_fee_rs
    else:
        rs = (c.points or 0) * lim.point_value_rs
    return 100 * rs / order


@dataclass
class Member:
    """One customer in the audience of a deal or campaign."""
    key: int  # customer id
    attrs: dict[str, str | None]
    candidate: Candidate | None = None  # their allowed offer (None = the rules allow none)
    share: float = 0.0  # discount share of that offer
    payload: object = None  # caller's own item


@dataclass
class GroupRow:
    kind: str
    group: str
    audience: int
    offered: int
    rate_ratio: float  # this group's offer rate ÷ overall
    generosity_ratio: float | None
    before_ratio: float
    status: str  # ok | rebalanced | limited | too small

    def as_dict(self) -> dict:
        return self.__dict__ | {"kind_label": KINDS[self.kind]}


@dataclass
class FairnessReport:
    rows: list[GroupRow] = field(default_factory=list)
    swaps: int = 0
    added: int = 0

    @property
    def gaps(self) -> list[GroupRow]:
        return [r for r in self.rows if r.status == "limited"]

    def as_dict(self) -> dict:
        return {"rows": [r.as_dict() for r in self.rows], "swaps": self.swaps, "added": self.added,
                "gaps": len(self.gaps)}


def _rates(members: list[Member], funded: set[int]) -> tuple[float, dict]:
    n, k = len(members), sum(1 for m in members if m.key in funded)
    overall = k / n if n else 0.0
    groups: dict[tuple[str, str], list[Member]] = defaultdict(list)
    for m in members:
        for kind, g in m.attrs.items():
            if g:
                groups[(kind, g)].append(m)
    return overall, groups


def rebalance(members: list[Member], funded: list[Member], unfunded: list[Member], budget_left: float,
              min_ratio: float, min_group_size: int, accept: Callable[[Member], bool] = lambda m: True,
              on_drop: Callable[[Member], None] = lambda m: None,
              on_add: Callable[[Member], None] = lambda m: None) -> tuple[list[Member], FairnessReport]:
    """Return the new funded list (same budget) and the report. `accept` runs the guardrail on a newcomer."""
    report = FairnessReport()
    funded_keys = {m.key for m in funded}
    overall0, groups = _rates(members, funded_keys)
    before = {gk: (sum(1 for m in ms if m.key in funded_keys) / len(ms)) / overall0 if overall0 else 1.0
              for gk, ms in groups.items()}
    funded = list(funded)
    pool = sorted([m for m in unfunded if m.candidate is not None], key=lambda m: -m.candidate.expected_net_rs)

    def ratio(gk, keys, overall) -> float:
        ms = groups[gk]
        return (sum(1 for m in ms if m.key in keys) / len(ms)) / overall if overall else 1.0

    for gk in sorted(groups, key=lambda g: before[g]):
        if len(groups[gk]) < min_group_size or overall0 == 0:
            continue
        kind, g = gk
        while True:
            keys = {m.key for m in funded}
            overall = len(keys) / len(members)
            if ratio(gk, keys, overall) >= min_ratio:
                break
            newcomer = next((m for m in pool if m.attrs.get(kind) == g), None)
            if newcomer is None:
                break  # the rules allow no more offers in this group
            pool.remove(newcomer)
            if not accept(newcomer):
                continue
            # Make room: drop the least valuable funded offers whose groups stay at or above the threshold.
            need = newcomer.candidate.liability_rs - budget_left
            dropped = []
            if need > 0:
                for donor in sorted(funded, key=lambda m: m.candidate.expected_net_rs):
                    if need <= 0:
                        break
                    if donor.attrs.get(kind) == g:
                        continue
                    trial = keys - {donor.key} - {d.key for d in dropped}
                    trial_overall = (len(trial) + 1) / len(members)
                    if any(d_g and len(groups[(d_kind, d_g)]) >= min_group_size
                           and ratio((d_kind, d_g), trial, trial_overall) < min_ratio
                           and before[(d_kind, d_g)] >= min_ratio
                           for d_kind, d_g in donor.attrs.items()):
                        continue  # would push the donor's own group below the line
                    dropped.append(donor)
                    need -= donor.candidate.liability_rs
                if need > 0:  # not enough room without hurting another group
                    break
            for d in dropped:
                funded.remove(d)
                budget_left += d.candidate.liability_rs
                on_drop(d)
                report.swaps += 1
            funded.append(newcomer)
            budget_left -= newcomer.candidate.liability_rs
            on_add(newcomer)
            report.added += 1

    keys = {m.key for m in funded}
    overall = len(keys) / len(members) if members else 0
    shares = [m.share for m in funded]
    avg_share = sum(shares) / len(shares) if shares else 0
    for gk, ms in sorted(groups.items()):
        kind, g = gk
        got = [m for m in ms if m.key in keys]
        r = ratio(gk, keys, overall)
        gen = (sum(m.share for m in got) / len(got)) / avg_share if got and avg_share else None
        if len(ms) < min_group_size:
            status = "too small"
        elif r >= min_ratio:
            status = "rebalanced" if before[gk] < min_ratio else "ok"
        else:
            status = "limited"
        report.rows.append(GroupRow(kind, g, len(ms), len(got), round(r, 2),
                                    None if gen is None else round(gen, 2), round(before[gk], 2), status))
    return funded, report

