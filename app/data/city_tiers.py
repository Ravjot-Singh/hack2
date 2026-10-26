"""Indian city tiers (1 = metro, 2 = large city, 3 = smaller towns), for fairness checks on uploaded data.

Covers the demo generator's cities plus common ones; anything unknown is "unknown" and left out of the checks
rather than guessed.
"""

from __future__ import annotations

from app.data.generator import CITIES

_EXTRA = {
    1: ["new delhi", "gurugram", "gurgaon", "noida", "navi mumbai", "thane", "bangalore", "secunderabad"],
    2: ["surat", "vadodara", "nagpur", "bhopal", "coimbatore", "kochi", "visakhapatnam", "nashik", "faridabad",
        "ghaziabad", "kanpur", "patna", "agra", "varanasi", "rajkot", "srinagar", "jammu", "panchkula",
        "bhubaneswar", "mysuru", "mysore", "madurai", "vijayawada", "raipur", "ranchi", "guwahati", "thiruvananthapuram"],
    3: ["firozpur", "ferozepur", "kapurthala", "sangrur", "barnala", "mansa", "faridkot", "fazilka", "muktsar",
        "sri muktsar sahib", "gurdaspur", "rupnagar", "ropar", "nawanshahr", "tarn taran", "fatehgarh sahib",
        "khanna", "phagwara", "rajpura", "zirakpur", "malerkotla", "abohar", "batala", "kurukshetra", "panipat",
        "sirsa", "hisar", "rohtak", "yamunanagar", "shimla", "solan", "una", "bilaspur"],
}

TIERS: dict[str, int] = {name.lower(): tier for name, _state, tier, _w in CITIES}
for _tier, _names in _EXTRA.items():
    for _n in _names:
        TIERS.setdefault(_n, _tier)


def tier_for(city: str | None) -> int | None:
    return TIERS.get((city or "").strip().lower()) if city else None
