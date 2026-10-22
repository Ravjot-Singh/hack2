"""Turn a business's CSV exports into a Dataset (the same shape the synthetic generator produces).

Nothing is written here: files are parsed and checked, and every problem is reported with its file and
spreadsheet row number (the header is row 1). Only a fully valid set of files produces a Dataset.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.data.generator import IST, LOYALTY_TIERS, Dataset

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_CUSTOMERS = 50_000
MAX_ERRORS = 50
DEFAULT_MARGIN = 25.0


@dataclass(frozen=True)
class FileSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    mandatory_file: bool
    help: str
    example: tuple[dict, ...]


FILES: dict[str, FileSpec] = {
    "customers": FileSpec(
        ("customer_id", "name", "signup_date"), ("city", "phone", "email", "birthday", "gender"), True,
        "One row per customer. Dates as YYYY-MM-DD.",
        ({"customer_id": "C1001", "name": "Asha Verma", "signup_date": "2024-03-15", "city": "Ludhiana",
          "phone": "9876500001", "email": "asha@example.com", "birthday": "1994-08-21", "gender": "F"},
         {"customer_id": "C1002", "name": "Rohit Sharma", "signup_date": "2025-11-02", "city": "Delhi",
          "phone": "", "email": "", "birthday": "", "gender": "M"})),
    "products": FileSpec(
        ("sku", "name", "category", "price"), ("margin_pct", "brand"), True,
        "One row per product. margin_pct is your gross margin in % (used to cap discounts).",
        ({"sku": "TSHIRT-01", "name": "Cotton T-shirt", "category": "Fashion", "price": "599", "margin_pct": "45",
          "brand": "Urban Thread"},
         {"sku": "ATTA-5KG", "name": "Atta 5kg", "category": "Grocery", "price": "289", "margin_pct": "12",
          "brand": ""})),
    "orders": FileSpec(
        ("order_id", "customer_id", "created_at"), ("status", "discount_amount", "points_redeemed", "payment_method"),
        True, "One row per order. created_at as YYYY-MM-DD or YYYY-MM-DD HH:MM. status: delivered, cancelled or returned.",
        ({"order_id": "O5001", "customer_id": "C1001", "created_at": "2026-09-28 19:40", "status": "delivered",
          "discount_amount": "50", "points_redeemed": "0", "payment_method": "upi"},
         {"order_id": "O5002", "customer_id": "C1002", "created_at": "2026-10-03", "status": "delivered",
          "discount_amount": "0", "points_redeemed": "0", "payment_method": "cod"})),
    "order_items": FileSpec(
        ("order_id", "sku", "quantity"), ("unit_price", "returned"), True,
        "One row per product in an order. unit_price defaults to the product price; returned: yes/no.",
        ({"order_id": "O5001", "sku": "TSHIRT-01", "quantity": "2", "unit_price": "549", "returned": "no"},
         {"order_id": "O5002", "sku": "ATTA-5KG", "quantity": "1", "unit_price": "", "returned": "no"})),
    "carts": FileSpec(
        ("customer_id", "created_at", "value"), ("category", "status", "items_count"), False,
        "Optional. Carts customers left without buying (status abandoned), so cart reminders work.",
        ({"customer_id": "C1002", "created_at": "2026-10-20 21:05", "value": "1299", "category": "Fashion",
          "status": "abandoned", "items_count": "2"},)),
    "points": FileSpec(
        ("customer_id", "created_at", "points"), ("expires_at",), False,
        "Optional. Reward points earned (positive) or spent/expired (negative).",
        ({"customer_id": "C1001", "created_at": "2026-09-28", "points": "11", "expires_at": "2027-03-28"},)),
}


def template_csv(name: str) -> str:
    spec = FILES[name]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[*spec.required, *spec.optional])
    w.writeheader()
    for row in spec.example:
        w.writerow(row)
    return buf.getvalue()


def category_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")[:30] or "other"


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.total = 0

    def add(self, file: str, row: int | None, text: str) -> None:
        self.total += 1
        if len(self.items) < MAX_ERRORS:
            self.items.append(f"{file}.csv{f' row {row}' if row else ''}: {text}")


def _parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"'{raw}' is not a date (use YYYY-MM-DD)")


def _parse_dt(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return datetime.combine(_parse_date(raw), time(12), IST)


def _money(raw: str, name: str, allow_negative: bool = False) -> int:
    cleaned = raw.replace("₹", "").replace(",", "").replace("Rs", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"{name} '{raw}' is not a number") from None
    if value < 0 and not allow_negative:
        raise ValueError(f"{name} can't be negative")
    return int(round(value))


def _bool(raw: str) -> bool:
    return raw.strip().lower() in ("yes", "y", "true", "1", "returned")


def _read(name: str, data: bytes, errors: _Errors) -> list[tuple[int, dict]] | None:
    if len(data) > MAX_BYTES:
        errors.add(name, None, f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in FILES[name].required if c not in header]
    if missing:
        errors.add(name, 1, f"missing column(s) {', '.join(missing)}. Download the template to see the expected columns.")
        return None
    rows = []
    for i, raw in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            errors.add(name, None, f"more than {MAX_ROWS:,} rows")
            return None
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue  # blank line
        rows.append((i, row))
    return rows


@dataclass
class ImportReport:
    errors: list[str] = field(default_factory=list)
    error_total: int = 0
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    dataset: Dataset | None = None
    catalog: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.dataset is not None


def parse_files(files: dict[str, bytes], now: datetime, existing_catalog: dict | None = None) -> ImportReport:
    """files: name -> raw bytes for the files the operator uploaded (empty uploads should be left out)."""
    errors = _Errors()
    report = ImportReport()
    existing_catalog = existing_catalog or {}
    for name, spec in FILES.items():
        if spec.mandatory_file and not files.get(name):
            errors.add(name, None, "this file is required")
    parsed = {name: _read(name, data, errors) for name, data in files.items() if data and name in FILES}
    if errors.total:
        report.errors, report.error_total = errors.items, errors.total
        return report

    today = now.date()
    # ---- products & categories ----
    products, sku_idx = [], {}
    cat_labels: dict[str, str] = {}
    margins: dict[str, list[float]] = defaultdict(list)
    for row_no, r in parsed["products"]:
        try:
            if not r["sku"] or not r["name"] or not r["category"]:
                raise ValueError("sku, name and category can't be empty")
            if r["sku"] in sku_idx:
                raise ValueError(f"sku '{r['sku']}' appears twice")
            price = _money(r["price"], "price")
            if price <= 0:
                raise ValueError("price must be above zero")
            key = category_key(r["category"])
            cat_labels.setdefault(key, r["category"].strip()[:40])
            if r.get("margin_pct"):
                m = float(r["margin_pct"].replace("%", ""))
                if not 0 < m < 100:
                    raise ValueError("margin_pct must be between 0 and 100")
                margins[key].append(m)
        except ValueError as exc:
            errors.add("products", row_no, str(exc))
            continue
        sku_idx[r["sku"]] = len(products) + 1
        products.append({"id": len(products) + 1, "sku": r["sku"][:20], "name": r["name"][:120],
                         "brand": (r.get("brand") or "")[:40], "category": key, "price": price,
                         "margin_pct": 0.0})
    catalog = {}
    for key, label in cat_labels.items():
        if margins[key]:
            margin = round(sum(margins[key]) / len(margins[key]), 1)
        elif key in existing_catalog:
            margin = float(existing_catalog[key]["margin_pct"])
        else:
            margin = DEFAULT_MARGIN
            report.warnings.append(f"No margin given for {label}: assumed {DEFAULT_MARGIN:g}%. Set the real one in "
                                   "Settings so discounts stay profitable.")
        catalog[key] = {"label": label, "margin_pct": margin}
    for p in products:
        p["margin_pct"] = catalog[p["category"]]["margin_pct"]

    # ---- customers ----
    customers, cust_idx = [], {}
    for row_no, r in parsed["customers"]:
        try:
            if not r["customer_id"] or not r["name"]:
                raise ValueError("customer_id and name can't be empty")
            if r["customer_id"] in cust_idx:
                raise ValueError(f"customer_id '{r['customer_id']}' appears twice")
            signup = _parse_date(r["signup_date"])
            if signup > today:
                raise ValueError("signup_date is in the future")
            birthday = _parse_date(r["birthday"]) if r.get("birthday") else None
            if birthday and birthday > today:
                raise ValueError("birthday is in the future")
        except ValueError as exc:
            errors.add("customers", row_no, str(exc))
            continue
        gender = (r.get("gender") or "").strip().upper()[:1]
        age = None
        if birthday:
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
        cust_idx[r["customer_id"]] = len(customers) + 1
        customers.append({
            "id": len(customers) + 1, "external_ref": r["customer_id"][:40], "name": r["name"][:80],
            "phone": (r.get("phone") or "")[:20] or None, "email": (r.get("email") or "")[:160] or None,
            "gender": gender if gender in ("M", "F") else None, "age": age, "birthday": birthday,
            "city": (r.get("city") or "")[:40] or None, "state": None, "city_tier": None, "device": None,
            "signup_date": signup, "loyalty_tier": "bronze", "points_balance": 0,
            "price_sensitivity": None, "true_segment": None,
        })
    if len(customers) > MAX_CUSTOMERS:
        errors.add("customers", None, f"more than {MAX_CUSTOMERS:,} customers")

    # ---- orders ----
    orders, order_idx = [], {}
    for row_no, r in parsed["orders"]:
        try:
            if r["order_id"] in order_idx:
                raise ValueError(f"order_id '{r['order_id']}' appears twice")
            if r["customer_id"] not in cust_idx:
                raise ValueError(f"customer_id '{r['customer_id']}' is not in customers.csv")
            created = _parse_dt(r["created_at"])
            if created.date() > today:
                raise ValueError("created_at is in the future")
            status = (r.get("status") or "delivered").lower()
            if status not in ("delivered", "cancelled", "returned"):
                raise ValueError(f"status '{status}' should be delivered, cancelled or returned")
            discount = _money(r["discount_amount"], "discount_amount") if r.get("discount_amount") else 0
            redeemed = _money(r["points_redeemed"], "points_redeemed") if r.get("points_redeemed") else 0
        except ValueError as exc:
            errors.add("orders", row_no, str(exc))
            continue
        order_idx[r["order_id"]] = len(orders) + 1
        orders.append({"id": len(orders) + 1, "customer_id": cust_idx[r["customer_id"]], "created_at": created,
                       "status": status, "items_count": 0, "gross_amount": 0, "discount_amount": discount,
                       "points_redeemed": redeemed, "net_amount": 0,
                       "payment_method": (r.get("payment_method") or "other")[:10].lower(), "coupon_code": None,
                       "sale_event": None})

    # ---- order items ----
    items = []
    for row_no, r in parsed["order_items"]:
        try:
            if r["order_id"] not in order_idx:
                raise ValueError(f"order_id '{r['order_id']}' is not in orders.csv")
            if r["sku"] not in sku_idx:
                raise ValueError(f"sku '{r['sku']}' is not in products.csv")
            qty = int(float(r["quantity"]))
            if qty < 1:
                raise ValueError("quantity must be at least 1")
            product = products[sku_idx[r["sku"]] - 1]
            price = _money(r["unit_price"], "unit_price") if r.get("unit_price") else product["price"]
        except ValueError as exc:
            errors.add("order_items", row_no, str(exc))
            continue
        o = orders[order_idx[r["order_id"]] - 1]
        o["items_count"] += qty
        o["gross_amount"] += qty * price
        items.append({"id": len(items) + 1, "order_id": o["id"], "product_id": product["id"],
                      "category": product["category"], "quantity": qty, "unit_price": price,
                      "returned": _bool(r.get("returned") or "") or o["status"] == "returned"})
    empty = [o for o in orders if o["items_count"] == 0]
    if empty:
        report.warnings.append(f"{len(empty):,} order(s) have no items in order_items.csv and were left out.")
        keep = {o["id"] for o in orders if o["items_count"]}
        orders = [o for o in orders if o["id"] in keep]
    for o in orders:
        o["discount_amount"] = min(o["discount_amount"], o["gross_amount"])
        o["points_redeemed"] = min(o["points_redeemed"], o["gross_amount"] - o["discount_amount"])
        o["net_amount"] = o["gross_amount"] - o["discount_amount"] - o["points_redeemed"]

    # ---- optional: carts, points ----
    carts = []
    for row_no, r in parsed.get("carts") or []:
        try:
            if r["customer_id"] not in cust_idx:
                raise ValueError(f"customer_id '{r['customer_id']}' is not in customers.csv")
            created = _parse_dt(r["created_at"])
            value = _money(r["value"], "value")
            status = (r.get("status") or "abandoned").lower()
            if status not in ("abandoned", "converted"):
                raise ValueError("status should be abandoned or converted")
        except ValueError as exc:
            errors.add("carts", row_no, str(exc))
            continue
        cat = category_key(r["category"]) if r.get("category") else None
        carts.append({"id": len(carts) + 1, "customer_id": cust_idx[r["customer_id"]], "created_at": created,
                      "status": status, "items_count": int(float(r.get("items_count") or 1)), "value": value,
                      "top_category": cat if cat in catalog else next(iter(catalog), "other"), "order_id": None})
    ledger = []
    balances: Counter = Counter()
    for row_no, r in parsed.get("points") or []:
        try:
            if r["customer_id"] not in cust_idx:
                raise ValueError(f"customer_id '{r['customer_id']}' is not in customers.csv")
            created = _parse_dt(r["created_at"])
            pts = _money(r["points"], "points", allow_negative=True)
            expires = _parse_dt(r["expires_at"]) if r.get("expires_at") else None
        except ValueError as exc:
            errors.add("points", row_no, str(exc))
            continue
        cid = cust_idx[r["customer_id"]]
        balances[cid] += pts
        ledger.append({"id": len(ledger) + 1, "customer_id": cid, "created_at": created,
                       "kind": "earn" if pts >= 0 else "redeem", "delta": pts, "order_id": None,
                       "expires_at": expires})

    if errors.total:
        report.errors, report.error_total = errors.items, errors.total
        return report

    spend = Counter()
    for o in orders:
        if o["status"] == "delivered" and o["created_at"] >= datetime.combine(today - timedelta(days=365), time(0), IST):
            spend[o["customer_id"]] += o["net_amount"]
    for c in customers:
        c["loyalty_tier"] = next(name for floor, name in LOYALTY_TIERS if spend[c["id"]] >= floor)
        c["points_balance"] = max(0, balances[c["id"]])

    report.dataset = Dataset(as_of=today, start=today - timedelta(days=365), seed=0, customers=customers,
                             products=products, orders=orders, order_items=items, carts=carts, browse_daily=[],
                             points_ledger=ledger)
    report.catalog = catalog
    report.counts = {"customers": len(customers), "products": len(products), "orders": len(orders),
                     "order_items": len(items), "carts": len(carts), "points": len(ledger),
                     "categories": len(catalog)}
    return report
