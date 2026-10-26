"""What customers tell the bot they like (/preferences in Telegram).

* Categories and offer types are a *soft* boost: among the offers the rules allow, matching ones are ranked
  higher (x BOOST in the learning engine, +1 priority with the fixed ranking). They never unlock an offer the
  rules block, and never hide one.
* Quiet times are strict, like the store's quiet hours: messages wait until the window ends (coupons for
  customers not on Telegram are unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import select

from app.db.models import CustomerPreference
from app.db.session import session_scope
from app.rules.engine import Candidate
from app.rules.schema import QuietHours

BOOST = 1.3
OFFER_TYPES = {"percent_off": "% off", "flat_off": "₹ off", "free_delivery": "Free delivery",
               "bonus_points": "Bonus points"}
QUIET_PRESETS = {
    "evening": ("Not in the evening (after 6 pm)", QuietHours(start=time(18), end=time(9))),
    "workday": ("Not during work hours (9 am–5 pm)", QuietHours(start=time(9), end=time(17))),
}


@dataclass
class Prefs:
    categories: list[str] = field(default_factory=list)
    offer_types: list[str] = field(default_factory=list)
    quiet: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.categories or self.offer_types or self.quiet)

    @property
    def quiet_hours(self) -> QuietHours | None:
        return QUIET_PRESETS[self.quiet][1] if self.quiet in QUIET_PRESETS else None

    def matches(self, c: Candidate) -> bool:
        return c.category in self.categories or c.type in self.offer_types

    def describe(self, category_labels: dict[str, str]) -> list[str]:
        out = []
        if self.categories:
            out.append("Likes " + ", ".join(category_labels.get(k, k) for k in self.categories))
        if self.offer_types:
            out.append("Prefers " + ", ".join(OFFER_TYPES.get(t, t) for t in self.offer_types))
        if self.quiet_hours:
            out.append(QUIET_PRESETS[self.quiet][0])
        return out


def load(customer_ids: list[int]) -> dict[int, Prefs]:
    if not customer_ids:
        return {}
    with session_scope() as s:
        return {p.customer_id: Prefs(list(p.categories or []), list(p.offer_types or []), p.quiet)
                for p in s.scalars(select(CustomerPreference).where(CustomerPreference.customer_id.in_(customer_ids)))}


def get(customer_id: int) -> Prefs:
    return load([customer_id]).get(customer_id, Prefs())


def toggle(tenant_id: int, customer_id: int, kind: str, value: str, now: datetime) -> Prefs:
    """kind: cat | type | quiet. Categories/types toggle on and off; quiet picks one preset (or 'none')."""
    with session_scope() as s:
        p = s.get(CustomerPreference, customer_id)
        if p is None:
            p = CustomerPreference(customer_id=customer_id, tenant_id=tenant_id, categories=[], offer_types=[])
            s.add(p)
        if kind == "cat":
            cats = list(p.categories or [])
            p.categories = [c for c in cats if c != value] if value in cats else cats + [value]
        elif kind == "type" and value in OFFER_TYPES:
            types = list(p.offer_types or [])
            p.offer_types = [t for t in types if t != value] if value in types else types + [value]
        elif kind == "quiet":
            p.quiet = value if value in QUIET_PRESETS else None
        p.updated_at = now
        return Prefs(list(p.categories), list(p.offer_types), p.quiet)


def boosts(prefs: Prefs | None, shortlist: list[Candidate]) -> dict[str, float]:
    if prefs is None or prefs.empty:
        return {}
    return {c.offer_key: BOOST for c in shortlist if prefs.matches(c)}


def quiet_until(prefs: Prefs | None, now: datetime) -> datetime | None:
    """When the customer's own quiet window ends, if it covers `now`."""
    q = prefs.quiet_hours if prefs else None
    if q is None or not q.contains(now.timetz().replace(tzinfo=None)):
        return None
    end = now.replace(hour=q.end.hour, minute=q.end.minute, second=0, microsecond=0)
    return end if end > now else end + timedelta(days=1)
