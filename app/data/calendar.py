"""Indian e-commerce shopping calendar: sale events, festivals and seasonal dips.

Each event multiplies how likely people are to shop on that day, overall and per category.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

# Diwali moves every year (lunar calendar).
DIWALI = {
    2024: date(2024, 11, 1),
    2025: date(2025, 10, 20),
    2026: date(2026, 11, 8),
    2027: date(2027, 10, 29),
}


@dataclass(frozen=True)
class Event:
    name: str
    start: date
    end: date  # inclusive
    multiplier: float  # overall shopping lift
    is_sale: bool = True  # platform-wide discounts running
    category_boost: dict[str, float] = field(default_factory=dict)


def events_for_year(year: int) -> list[Event]:
    ev = [
        Event("Republic Day Sale", date(year, 1, 17), date(year, 1, 22), 1.7),
        Event("Holi", date(year, 3, 1), date(year, 3, 4), 1.2, is_sale=False,
              category_boost={"beauty": 1.6, "grocery": 1.4}),
        Event("End of Season Sale", date(year, 6, 20), date(year, 7, 5), 1.4,
              category_boost={"fashion": 1.8}),
        Event("Monsoon lull", date(year, 7, 10), date(year, 8, 31), 0.88, is_sale=False),
        Event("Independence Day Sale", date(year, 8, 7), date(year, 8, 12), 1.7),
        Event("Big Festive Sale", date(year, 9, 23), date(year, 10, 2), 2.4,
              category_boost={"electronics": 1.6, "home_kitchen": 1.3}),
        Event("Year End Sale", date(year, 12, 20), date(year, 12, 31), 1.3),
    ]
    if year in DIWALI:
        d = DIWALI[year]
        ev.append(Event("Diwali Dhamaka", d - timedelta(days=18), d + timedelta(days=1), 1.8,
                        category_boost={"home_kitchen": 1.5, "grocery": 1.4, "fashion": 1.3,
                                        "electronics": 1.3}))
        ev.append(Event("Post-Diwali lull", d + timedelta(days=2), d + timedelta(days=12), 0.75,
                        is_sale=False))
    return ev


@dataclass
class Calendar:
    days: list[date]
    multiplier: np.ndarray  # (n_days,) overall demand lift
    is_sale: np.ndarray  # (n_days,) bool
    sale_name: list[str | None]
    category_boost: np.ndarray  # (n_days, n_categories)
    weekday_factor: np.ndarray  # (n_days,)


def build_calendar(start: date, n_days: int, categories: list[str]) -> Calendar:
    days = [start + timedelta(days=i) for i in range(n_days)]
    mult = np.ones(n_days)
    is_sale = np.zeros(n_days, dtype=bool)
    names: list[str | None] = [None] * n_days
    boost = np.ones((n_days, len(categories)))
    cat_idx = {c: i for i, c in enumerate(categories)}

    events = [e for y in {d.year for d in days} for e in events_for_year(y)]
    for i, d in enumerate(days):
        for e in events:
            if e.start <= d <= e.end:
                mult[i] *= e.multiplier
                if e.is_sale:
                    is_sale[i] = True
                    names[i] = e.name
                for cat, b in e.category_boost.items():
                    if cat in cat_idx:
                        boost[i, cat_idx[cat]] *= b

    # Weekends busier; first days of the month (salary credited) busier.
    wk = np.array([1.15 if d.weekday() >= 5 else 0.94 for d in days])
    payday = np.array([1.12 if d.day <= 5 else 0.98 for d in days])
    return Calendar(days, mult, is_sale, names, boost, wk * payday)
