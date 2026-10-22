"""Synthetic customer population with 12 months of behaviour for one tenant.

How it works
------------
Every customer gets a hidden persona ("true segment") that sets how often they buy, how price-sensitive
they are, how strongly sales pull them in, whether they use coupons/points, and when they stop buying
(at-risk and dormant customers decay away). Each day, a customer's chance of ordering is

    base rate x calendar (sales, festivals, monsoon) x weekday/payday x activity (signup, churn)

Orders are drawn from that, and carts, browsing and the points ledger are all derived from the same
daily demand, so the tables tell one consistent story. Everything is seeded: same seed + as_of date
=> identical data. History covers the 365 days before `as_of` (nothing is in the future).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml
from faker import Faker

from app.data.calendar import Calendar, build_calendar

IST = timezone(timedelta(hours=5, minutes=30))
N_DAYS = 365
POINTS_EXPIRY_DAYS = 180


@dataclass(frozen=True)
class Segment:
    name: str
    share: float
    orders_per_year: float
    sensitivity_beta: tuple[float, float]  # price sensitivity ~ Beta(a, b)
    sale_affinity: float  # exponent on the calendar lift: >1 = shops mostly in sales
    off_sale_factor: float  # demand outside sales (bargain hunters wait for sales)
    coupon_prob: float
    redeem_prob: float
    abandon_ratio: float  # share of carts never checked out
    sessions_per_order: float
    extra_sessions_per_year: float
    category_prior: dict[str, float] = field(default_factory=dict)


SEGMENTS: list[Segment] = [
    Segment("loyal_high_value", 0.08, 15, (2, 5), 0.6, 1.0, 0.08, 0.50, 0.50, 6, 15,
            {"electronics": 1.2, "fashion": 1.5, "grocery": 3, "home_kitchen": 1.2, "beauty": 1.2, "books": 0.6}),
    Segment("regular", 0.20, 7, (3, 4), 0.9, 1.0, 0.12, 0.35, 0.62, 6, 10,
            {"electronics": 1, "fashion": 1.5, "grocery": 2.5, "home_kitchen": 1, "beauty": 1.2, "books": 0.6}),
    Segment("bargain_hunter", 0.15, 6, (6, 2), 2.0, 0.55, 0.45, 0.40, 0.78, 8, 20,
            {"electronics": 1.4, "fashion": 1.8, "grocery": 1.2, "home_kitchen": 1.2, "beauty": 1, "books": 0.5}),
    Segment("occasional", 0.25, 1.4, (3, 3), 1.0, 1.0, 0.10, 0.15, 0.68, 6, 4,
            {"electronics": 1.5, "fashion": 1.5, "grocery": 0.8, "home_kitchen": 1.2, "beauty": 0.8, "books": 0.6}),
    Segment("new", 0.10, 7, (3, 3), 1.0, 1.0, 0.20, 0.10, 0.70, 7, 10,
            {"electronics": 1.2, "fashion": 1.5, "grocery": 1.2, "home_kitchen": 1, "beauty": 1, "books": 0.6}),
    Segment("at_risk", 0.10, 7, (3, 3), 0.9, 1.0, 0.12, 0.30, 0.68, 6, 8,
            {"electronics": 1, "fashion": 1.5, "grocery": 2, "home_kitchen": 1, "beauty": 1, "books": 0.6}),
    Segment("dormant", 0.07, 5, (3, 3), 1.0, 1.0, 0.10, 0.20, 0.70, 6, 4,
            {"electronics": 1.2, "fashion": 1.5, "grocery": 1, "home_kitchen": 1, "beauty": 1, "books": 0.6}),
    Segment("window_shopper", 0.05, 0.7, (4, 3), 1.2, 1.0, 0.10, 0.05, 0.93, 25, 60,
            {"electronics": 2, "fashion": 2, "grocery": 0.4, "home_kitchen": 1, "beauty": 1, "books": 0.6}),
]

# (city, state, tier, weight). Punjab-heavy, with major metros.
CITIES = [
    ("Ludhiana", "Punjab", 2, 6), ("Amritsar", "Punjab", 2, 5), ("Jalandhar", "Punjab", 2, 4),
    ("Patiala", "Punjab", 3, 3), ("Mohali", "Punjab", 2, 3), ("Bathinda", "Punjab", 3, 2),
    ("Hoshiarpur", "Punjab", 3, 1.5), ("Moga", "Punjab", 3, 1), ("Pathankot", "Punjab", 3, 1),
    ("Chandigarh", "Chandigarh", 2, 4), ("Ambala", "Haryana", 3, 1.5), ("Karnal", "Haryana", 3, 1),
    ("Delhi", "Delhi", 1, 8), ("Mumbai", "Maharashtra", 1, 7), ("Bengaluru", "Karnataka", 1, 6),
    ("Hyderabad", "Telangana", 1, 4), ("Pune", "Maharashtra", 1, 3), ("Chennai", "Tamil Nadu", 1, 3),
    ("Kolkata", "West Bengal", 1, 3), ("Ahmedabad", "Gujarat", 1, 3), ("Jaipur", "Rajasthan", 2, 3),
    ("Lucknow", "Uttar Pradesh", 2, 3), ("Indore", "Madhya Pradesh", 2, 2), ("Dehradun", "Uttarakhand", 2, 1.5),
]
PUNJABI_STATES = {"Punjab", "Chandigarh"}
PUNJABI_FIRST_M = ["Gurpreet", "Harpreet", "Jaspreet", "Amandeep", "Navdeep", "Arjun", "Karan", "Simranjit",
                   "Hardeep", "Sukhwinder", "Rajveer", "Ishaan", "Rohit", "Gurdeep", "Manjot", "Tejinder"]
PUNJABI_FIRST_F = ["Simran", "Harleen", "Jasleen", "Navneet", "Gurleen", "Manpreet", "Priya", "Ritika",
                   "Amrit", "Sukhmani", "Kirandeep", "Tanvi", "Mehak", "Rupinder", "Avneet", "Jasmeen"]
PUNJABI_LAST = ["Singh", "Sandhu", "Gill", "Dhillon", "Sidhu", "Grewal", "Bajwa", "Brar", "Arora",
                "Malhotra", "Kapoor", "Khanna", "Sethi", "Chawla", "Bedi", "Ahluwalia"]

AGE_BANDS = [((18, 24), 0.22), ((25, 34), 0.38), ((35, 44), 0.22), ((45, 54), 0.12), ((55, 70), 0.06)]
HOUR_WEIGHTS = np.array([1, .6, .3, .2, .2, .3, .8, 1.5, 2.5, 3, 3.2, 3.5, 4, 4, 3.5, 3.2, 3.2, 3.5, 4, 5, 6, 6.5, 5, 3])
PAYMENT_BY_TIER = {  # UPI, card, COD, wallet
    1: [0.55, 0.22, 0.12, 0.11],
    2: [0.55, 0.13, 0.22, 0.10],
    3: [0.50, 0.08, 0.35, 0.07],
}
PAYMENT_METHODS = ["upi", "card", "cod", "wallet"]
TIER_PRICE_SHIFT = {1: -0.3, 2: 0.0, 3: 0.3}  # metro customers lean to pricier products
LOYALTY_TIERS = [(75_000, "platinum"), (25_000, "gold"), (5_000, "silver"), (0, "bronze")]
VARIANTS = ["Classic", "Plus", "Pro", "Lite", "Select", "Premium", "Value", "Max"]


@dataclass
class Dataset:
    as_of: date
    start: date
    seed: int
    customers: list[dict] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    order_items: list[dict] = field(default_factory=list)
    carts: list[dict] = field(default_factory=list)
    browse_daily: list[dict] = field(default_factory=list)
    points_ledger: list[dict] = field(default_factory=list)


def load_catalog(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _price(rng: np.random.Generator, median: float, sigma: float) -> int:
    p = rng.lognormal(np.log(median), sigma)
    if p < 100:
        return max(19, int(round(p / 10) * 10) - 1)  # 49, 99
    return int(round(p / 50) * 50) - 1  # 499, 1299


def _make_products(rng: np.random.Generator, catalog: dict) -> list[dict]:
    products = []
    per_cat = catalog.get("products_per_category", 50)
    for cat, spec in catalog["categories"].items():
        for i in range(per_cat):
            brand = spec["brands"][rng.integers(len(spec["brands"]))]
            item = spec["items"][rng.integers(len(spec["items"]))]
            variant = VARIANTS[rng.integers(len(VARIANTS))]
            products.append({
                "id": len(products) + 1,
                "sku": f"{cat[:3].upper()}-{i + 1:04d}",
                "name": f"{brand} {item} ({variant})",
                "brand": brand,
                "category": cat,
                "price": _price(rng, spec["median_price"], spec["price_sigma"]),
                "margin_pct": float(spec["margin_pct"]),
            })
    return products


class _ProductPicker:
    """Picks products in a category, skewed cheaper for price-sensitive shoppers."""

    def __init__(self, rng: np.random.Generator, products: list[dict], categories: list[str]):
        self.rng = rng
        self.by_cat = {c: [p for p in products if p["category"] == c] for c in categories}
        self.prices = {c: np.array([p["price"] for p in ps], dtype=float) for c, ps in self.by_cat.items()}
        self._cache: dict[tuple[str, float], np.ndarray] = {}

    def pick(self, cat: str, exponent: float) -> dict:
        key = (cat, round(exponent, 1))
        if key not in self._cache:
            prices = self.prices[cat]
            w = (prices / np.median(prices)) ** (-key[1])
            self._cache[key] = np.cumsum(w / w.sum())
        idx = int(np.searchsorted(self._cache[key], self.rng.random()))
        return self.by_cat[cat][min(idx, len(self.by_cat[cat]) - 1)]


def _ts(rng: np.random.Generator, day: date) -> datetime:
    hour = int(rng.choice(24, p=HOUR_WEIGHTS / HOUR_WEIGHTS.sum()))
    return datetime.combine(day, time(hour, int(rng.integers(60)), int(rng.integers(60))), IST)


def _birthday(rng: np.random.Generator, age: int, as_of: date) -> date:
    d = date(2001, 1, 1) + timedelta(days=int(rng.integers(365)))  # non-leap reference year
    b = date(as_of.year - age, d.month, d.day)
    return b.replace(year=b.year - 1) if (d.month, d.day) > (as_of.month, as_of.day) else b


def generate(catalog_path: Path, n_customers: int = 5000, seed: int = 42, as_of: date | None = None,
             ref_prefix: str = "SK") -> Dataset:
    rng = np.random.default_rng(seed)
    fake = Faker("en_IN")
    fake.seed_instance(seed)
    as_of = as_of or date.today()
    start = as_of - timedelta(days=N_DAYS)  # history = start .. as_of - 1
    catalog = load_catalog(catalog_path)
    cats = list(catalog["categories"])
    k = len(cats)
    cal = build_calendar(start, N_DAYS, cats)
    ds = Dataset(as_of=as_of, start=start, seed=seed)
    ds.products = _make_products(rng, catalog)
    picker = _ProductPicker(rng, ds.products, cats)
    return_rate = np.array([catalog["categories"][c]["return_rate"] for c in cats])
    repeat_qty = [catalog["categories"][c].get("repeat_qty", 1) for c in cats]

    # ---- customers & hidden traits -------------------------------------------------------------
    n = n_customers
    seg_idx = rng.choice(len(SEGMENTS), size=n, p=[s.share for s in SEGMENTS])
    segs = [SEGMENTS[i] for i in seg_idx]
    city_w = np.array([c[3] for c in CITIES], dtype=float)
    city_idx = rng.choice(len(CITIES), size=n, p=city_w / city_w.sum())

    signup_idx = np.zeros(n, dtype=int)  # day index relative to start; negative = before the window
    churn_idx = np.full(n, 10 * N_DAYS, dtype=float)
    churn_tau = np.ones(n)
    for i, s in enumerate(segs):
        if s.name == "new":
            signup_idx[i] = N_DAYS - rng.integers(3, 61)
        elif s.name == "dormant":
            signup_idx[i] = -rng.integers(30, 1100)
            churn_idx[i], churn_tau[i] = rng.integers(0, 181), 8
        elif s.name == "at_risk":
            signup_idx[i] = -rng.integers(60, 1100)
            churn_idx[i], churn_tau[i] = N_DAYS - rng.integers(45, 121), 25
        elif s.name == "loyal_high_value":
            signup_idx[i] = -rng.integers(200, 1500)
        else:
            signup_idx[i] = rng.integers(-1400, 250)

    # Individual heterogeneity around the segment mean (mean-preserving lognormal).
    hetero = rng.lognormal(-0.245, 0.7, size=n)
    base_rate = np.array([s.orders_per_year for s in segs]) * hetero
    sensitivity = np.array([rng.beta(*s.sensitivity_beta) for s in segs])
    return_mult = rng.lognormal(-0.18, 0.6, size=n) * np.where(rng.random(n) < 0.02, 4.0, 1.0)

    ages, affinity = np.zeros(n, dtype=int), np.zeros((n, k))
    for i, s in enumerate(segs):
        band = AGE_BANDS[rng.choice(len(AGE_BANDS), p=[b[1] for b in AGE_BANDS])][0]
        ages[i] = rng.integers(band[0], band[1] + 1)
        prior = np.array([s.category_prior.get(c, 1.0) for c in cats])
        if "books" in cats and ages[i] < 25:
            prior[cats.index("books")] *= 1.6
        if "home_kitchen" in cats and ages[i] >= 30:
            prior[cats.index("home_kitchen")] *= 1.4
        affinity[i] = rng.dirichlet(prior / prior.sum() * 4 + 0.05)
    aff_cum = np.cumsum(affinity, axis=1)

    # ---- daily demand ---------------------------------------------------------------------------
    d = np.arange(N_DAYS)
    activity = (d[None, :] >= signup_idx[:, None]).astype(float)
    welcome = activity.astype(bool) & (d[None, :] < signup_idx[:, None] + 14)
    activity[welcome] *= 1.6
    days_since_churn = np.maximum(0, d[None, :] - churn_idx[:, None])
    activity *= np.exp(-days_since_churn / churn_tau[:, None])

    sale_aff = np.array([s.sale_affinity for s in segs])
    shape = cal.multiplier[None, :] ** sale_aff[:, None]
    off_sale = np.array([s.off_sale_factor for s in segs])
    shape[:, ~cal.is_sale] *= off_sale[:, None]
    shape *= cal.weekday_factor[None, :]
    shape /= shape.mean(axis=1, keepdims=True)  # so base_rate stays "orders per year at full activity"
    demand = shape * activity  # relative daily intensity

    order_lambda = base_rate[:, None] / N_DAYS * demand
    order_counts = np.minimum(rng.poisson(order_lambda), 2)
    browse_rate = base_rate * np.array([s.sessions_per_order for s in segs]) + \
        np.array([s.extra_sessions_per_year for s in segs])
    browse_lambda = browse_rate[:, None] / N_DAYS * demand
    must_browse = np.zeros((n, N_DAYS), dtype=bool)

    for i in range(n):
        s = segs[i]
        city, state, tier, _ = CITIES[city_idx[i]]
        gender = "F" if rng.random() < 0.48 else "M"
        if state in PUNJABI_STATES and rng.random() < 0.75:
            first = (PUNJABI_FIRST_F if gender == "F" else PUNJABI_FIRST_M)[rng.integers(16)]
            last = PUNJABI_LAST[rng.integers(len(PUNJABI_LAST))]
            if last == "Singh" and gender == "F":
                last = "Kaur"
        else:
            first = fake.first_name_female() if gender == "F" else fake.first_name_male()
            last = fake.last_name()
        ios_share = 0.22 if tier == 1 else 0.10
        device = rng.choice(["android", "ios", "web"], p=[0.82 - ios_share, ios_share, 0.18])
        cust = {
            "id": i + 1,
            "external_ref": f"{ref_prefix}{i + 1:05d}",
            "name": f"{first} {last}",
            "gender": gender,
            "age": int(ages[i]),
            "birthday": _birthday(rng, int(ages[i]), as_of),
            "city": city, "state": state, "city_tier": tier,
            "device": str(device),
            "signup_date": start + timedelta(days=int(signup_idx[i])),
            "price_sensitivity": round(float(sensitivity[i]), 3),
            "true_segment": s.name,
        }
        exponent = 2.0 * (sensitivity[i] - 0.45) + TIER_PRICE_SHIFT[tier]

        # ---- orders ----
        cust_orders = []
        first_order = True
        for day_i in np.nonzero(order_counts[i])[0]:
            for _ in range(order_counts[i, day_i]):
                day = cal.days[day_i]
                p = affinity[i] * cal.category_boost[day_i]
                primary = int(rng.choice(k, p=p / p.sum()))
                n_lines = 2 + rng.poisson(2.0) if cats[primary] == "grocery" else 1 + rng.poisson(0.35)
                lines, gross = [], 0
                for line in range(min(n_lines, 10)):
                    c = primary if line == 0 or rng.random() < 0.6 else \
                        int(np.searchsorted(aff_cum[i], rng.random() * aff_cum[i, -1]))
                    c = min(c, k - 1)
                    prod = picker.pick(cats[c], exponent)
                    qty = int(rng.integers(1, repeat_qty[c] + 1))
                    lines.append({"product_id": prod["id"], "category": cats[c], "quantity": qty,
                                  "unit_price": prod["price"], "cat_i": c})
                    gross += qty * prod["price"]

                discount, coupon, sale_name = 0, None, None
                if cal.is_sale[day_i]:
                    sale_name = cal.sale_name[day_i]
                    if rng.random() < 0.85:
                        discount = int(gross * rng.uniform(0.08, 0.30))
                elif first_order and signup_idx[i] >= day_i - 30 and rng.random() < 0.6:
                    coupon, discount = "WELCOME15", min(200, int(gross * 0.15))
                elif rng.random() < s.coupon_prob:
                    coupon = ["SAVE10", "SK100", "EXTRA5"][rng.integers(3)]
                    if coupon == "SAVE10":
                        discount = min(150, int(gross * 0.10))
                    elif coupon == "SK100":
                        discount = 100 if gross >= 999 else 0
                        coupon = coupon if discount else None
                    else:
                        discount = int(gross * 0.05)
                first_order = False

                payment = PAYMENT_METHODS[rng.choice(4, p=PAYMENT_BY_TIER[tier])]
                cancelled = rng.random() < (0.08 if payment == "cod" else 0.03)
                for ln in lines:
                    ln["returned"] = (not cancelled) and \
                        rng.random() < min(0.9, return_rate[ln.pop("cat_i")] * return_mult[i])
                status = "cancelled" if cancelled else \
                    ("returned" if all(ln["returned"] for ln in lines) else "delivered")
                order = {
                    "id": len(ds.orders) + 1, "customer_id": i + 1, "created_at": _ts(rng, day),
                    "status": status, "items_count": sum(ln["quantity"] for ln in lines),
                    "gross_amount": gross, "discount_amount": discount, "points_redeemed": 0,
                    "net_amount": gross - discount, "payment_method": payment, "coupon_code": coupon,
                    "sale_event": sale_name, "_lines": lines, "_primary": cats[primary],
                }
                ds.orders.append(order)
                cust_orders.append(order)
                must_browse[i, day_i] = True
        cust_orders.sort(key=lambda o: o["created_at"])

        # ---- carts: one converted cart per order, plus abandoned carts on browsing days ----
        for o in cust_orders:
            ds.carts.append({
                "id": len(ds.carts) + 1, "customer_id": i + 1,
                "created_at": o["created_at"] - timedelta(minutes=int(rng.integers(3, 120))),
                "status": "converted", "items_count": o["items_count"], "value": o["gross_amount"],
                "top_category": o["_primary"], "order_id": o["id"],
            })
        row = browse_lambda[i]
        if row.sum() > 0:
            r = s.abandon_ratio
            n_abandoned = rng.poisson(len(cust_orders) * r / (1 - r) + row.sum() * 0.02)
            for day_i in rng.choice(N_DAYS, size=n_abandoned, p=row / row.sum()):
                c = min(int(np.searchsorted(aff_cum[i], rng.random() * aff_cum[i, -1])), k - 1)
                n_items = 1 + rng.poisson(0.6)
                value = sum(picker.pick(cats[c], exponent)["price"] for _ in range(n_items))
                ds.carts.append({
                    "id": len(ds.carts) + 1, "customer_id": i + 1, "created_at": _ts(rng, cal.days[day_i]),
                    "status": "abandoned", "items_count": int(n_items), "value": int(value),
                    "top_category": cats[c], "order_id": None,
                })
                must_browse[i, day_i] = True

        # ---- points: opening balance, redeem on orders, earn on kept value, expire after 180 days ----
        balance = _points_for_customer(ds, rng, i + 1, s, cust_orders, signup_idx[i], start, as_of)
        spend = sum(o["net_amount"] for o in cust_orders if o["status"] == "delivered")
        cust["loyalty_tier"] = next(name for floor, name in LOYALTY_TIERS if spend >= floor)
        cust["points_balance"] = balance
        ds.customers.append(cust)

    for o in ds.orders:
        for ln in o.pop("_lines"):
            ds.order_items.append({"id": len(ds.order_items) + 1, "order_id": o["id"], **ln})
        o.pop("_primary")

    _browsing(ds, rng, cal, browse_lambda, must_browse, aff_cum, cats)
    return ds


def _points_for_customer(ds: Dataset, rng: np.random.Generator, cust_id: int, s: Segment,
                         orders: list[dict], signup_i: int, start: date, as_of: date) -> int:
    lots: list[list] = []  # [amount, expires_at], oldest first
    expiry = timedelta(days=POINTS_EXPIRY_DAYS)
    window_start = datetime.combine(start, time(0), IST)

    def add(kind: str, delta: int, ts: datetime, order_id=None, expires_at=None):
        ds.points_ledger.append({"id": len(ds.points_ledger) + 1, "customer_id": cust_id, "created_at": ts,
                                 "kind": kind, "delta": delta, "order_id": order_id, "expires_at": expires_at})

    def expire_until(ts: datetime):
        while lots and lots[0][1] <= ts:
            amt, exp = lots.pop(0)
            if amt > 0:
                add("expire", -amt, exp)

    if signup_i < 0 and rng.random() < 0.65:
        earned = window_start - timedelta(days=int(rng.integers(0, min(170, -signup_i) + 1)))
        amt = int(rng.lognormal(np.log(250), 0.7))
        lots.append([amt, earned + expiry])
        add("opening", amt, earned, expires_at=earned + expiry)

    for o in orders:
        expire_until(o["created_at"])
        balance = sum(lot[0] for lot in lots)
        if o["status"] != "cancelled" and balance >= 100 and rng.random() < s.redeem_prob:
            redeem = min(balance, int(0.10 * (o["gross_amount"] - o["discount_amount"])))
            if redeem >= 50:
                o["points_redeemed"] = redeem
                o["net_amount"] -= redeem
                add("redeem", -redeem, o["created_at"], o["id"])
                left = redeem
                for lot in lots:
                    take = min(lot[0], left)
                    lot[0] -= take
                    left -= take
                lots[:] = [lot for lot in lots if lot[0] > 0]
        if o["status"] == "delivered":
            gross_kept = sum(ln["quantity"] * ln["unit_price"] for ln in o["_lines"] if not ln["returned"])
            earn = int(o["net_amount"] * gross_kept / max(o["gross_amount"], 1)) // 100
            if earn > 0:
                lots.append([earn, o["created_at"] + expiry])
                add("earn", earn, o["created_at"], o["id"], o["created_at"] + expiry)
    expire_until(datetime.combine(as_of, time(0), IST))
    return sum(lot[0] for lot in lots)


def _browsing(ds: Dataset, rng: np.random.Generator, cal: Calendar, browse_lambda: np.ndarray,
              must_browse: np.ndarray, aff_cum: np.ndarray, cats: list[str]) -> None:
    sessions = np.maximum(rng.poisson(browse_lambda), must_browse.astype(int))
    ci, di = np.nonzero(sessions)
    sess = sessions[ci, di]
    views = sess * rng.integers(2, 9, size=len(sess))
    u = rng.random(len(ci))[:, None] * aff_cum[ci, -1:]
    top = np.minimum((aff_cum[ci] < u).sum(axis=1), len(cats) - 1)
    for j in range(len(ci)):
        ds.browse_daily.append({
            "id": j + 1, "customer_id": int(ci[j]) + 1, "day": cal.days[di[j]],
            "sessions": int(sess[j]), "product_views": int(views[j]), "top_category": cats[top[j]],
        })
