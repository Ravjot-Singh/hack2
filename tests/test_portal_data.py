import csv
import io
import time as clock
from datetime import datetime, time

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.data.generator import IST
from app.db.models import Customer, CustomerFeature, ImportJob, RulesVersion, Tenant
from app.db.session import session_scope
from app.ingest.csv_import import parse_files, template_csv
from app.main import app
from app.portal import routes as portal_routes
from app.portal.auth import reset_failures
from app.rules.policy import current_policy, default_rules, save_rules
from app.tenants.registry import TEMPLATE_DIR, catalog_from_file

SAME = {"Origin": "http://testserver"}
NOW = datetime.combine(datetime.now(IST).date(), time(12), IST)


def _csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def good_files(n_customers: int = 30) -> dict[str, bytes]:
    customers = [{"customer_id": f"C{i}", "name": f"Customer {i}", "signup_date": "2025-01-15", "city": "Pune",
                  "phone": f"98765{i:05d}", "email": f"c{i}@example.com", "birthday": "1990-05-02", "gender": "F"}
                 for i in range(n_customers)]
    products = [{"sku": "TS-1", "name": "T-shirt", "category": "Fashion", "price": "₹599", "margin_pct": "44"},
                {"sku": "TS-2", "name": "Jeans", "category": "Fashion", "price": "1,499", "margin_pct": "46"},
                {"sku": "AT-1", "name": "Atta 5kg", "category": "Grocery", "price": "289", "margin_pct": "12"}]
    orders, items = [], []
    for i in range(n_customers):
        for j in range(1 + i % 4):
            oid = f"O{i}-{j}"
            orders.append({"order_id": oid, "customer_id": f"C{i}", "created_at": f"2026-0{1 + j * 2}-1{j} 18:30",
                           "status": "delivered", "discount_amount": "0"})
            items.append({"order_id": oid, "sku": ["TS-1", "TS-2", "AT-1"][(i + j) % 3], "quantity": "1",
                          "returned": "no"})
    return {"customers": _csv(customers), "products": _csv(products), "orders": _csv(orders),
            "order_items": _csv(items)}


def test_good_files_parse_into_dataset_and_catalog():
    r = parse_files(good_files(), NOW)
    assert r.ok and r.counts["customers"] == 30 and r.counts["categories"] == 2
    assert r.catalog == {"fashion": {"label": "Fashion", "margin_pct": 45.0},
                         "grocery": {"label": "Grocery", "margin_pct": 12.0}}
    c = r.dataset.customers[0]
    assert c["external_ref"] == "C0" and c["age"] > 30 and c["true_segment"] is None
    assert r.dataset.products[0]["price"] == 599 and r.dataset.products[1]["price"] == 1499
    assert all(o["gross_amount"] > 0 and o["net_amount"] == o["gross_amount"] for o in r.dataset.orders)


@pytest.mark.parametrize("change, expected", [
    (lambda f: f.pop("orders"), "orders.csv: this file is required"),
    (lambda f: f.update(customers=b"id,name\nC1,A\n"), "customers.csv row 1: missing column(s) customer_id, signup_date"),
    (lambda f: f.update(customers=f["customers"].replace(b"2025-01-15", b"15th Jan", 1)), "customers.csv row 2: '15th Jan' is not a date"),
    (lambda f: f.update(customers=f["customers"] + b"C1,Dup,2025-01-01,,,,,\n"), "customer_id 'C1' appears twice"),
    (lambda f: f.update(orders=f["orders"] + b"X1,NOPE,2026-01-01,delivered,0\n"), "customer_id 'NOPE' is not in customers.csv"),
    (lambda f: f.update(order_items=f["order_items"] + b"O0-0,GHOST,1,no\n"), "sku 'GHOST' is not in products.csv"),
    (lambda f: f.update(orders=f["orders"] + b"X2,C1,2099-01-01,delivered,0\n"), "created_at is in the future"),
    (lambda f: f.update(products=f["products"] + b"BK-1,Book,Books,299,120\n"), "margin_pct must be between 0 and 100"),
])
def test_problems_are_reported_with_file_and_row(change, expected):
    files = good_files()
    change(files)
    r = parse_files(files, NOW)
    assert not r.ok and any(expected in e for e in r.errors), r.errors


def test_missing_margin_gets_default_with_warning():
    files = good_files()
    files["products"] += b"BK-1,Novel,Books,299,\n"
    r = parse_files(files, NOW)
    assert r.ok and r.catalog["books"]["margin_pct"] == 25.0 and "No margin given for Books" in r.warnings[0]


def test_templates_are_valid_examples():
    files = {n: template_csv(n).encode() for n in ("customers", "products", "orders", "order_items", "carts", "points")}
    r = parse_files(files, NOW)
    assert r.ok, r.errors
    assert r.counts["carts"] == 1 and r.counts["points"] == 1


# ---- through the portal ----

@pytest.fixture
def client(monkeypatch):
    reset_failures()
    monkeypatch.setattr(portal_routes, "_now", lambda: NOW)
    with TestClient(app) as c:
        c.post("/app/register", data={"business_name": "Kesar Fashions", "email": "k@kesar.example",
                                      "password": "sup3r-secret", "confirm": "sup3r-secret"}, headers=SAME)
        yield c


def _tid(slug="kesar-fashions"):
    with session_scope() as s:
        return s.scalar(select(Tenant.id).where(Tenant.slug == slug))


def _wait_job(timeout=60):
    end = clock.time() + timeout
    while clock.time() < end:
        with session_scope() as s:
            job = s.scalars(select(ImportJob).order_by(ImportJob.id.desc())).first()
            if job and job.status != "running":
                return job.status, job.summary, job.error
        clock.sleep(0.2)
    pytest.fail("job did not finish")


def _upload(c, files, confirm=True):
    data = {"confirm": "yes"} if confirm else {}
    return c.post("/app/data/upload", data=data, headers=SAME,
                  files={k: (f"{k}.csv", v, "text/csv") for k, v in files.items()})


def test_upload_imports_data_and_adapts_rules(client):
    tid = _tid()
    # A saved rules version mentioning categories this business won't have after the import.
    save_rules("kesar-fashions", tid, None, default_rules(catalog_from_file(TEMPLATE_DIR / "catalog.yaml")),
               "owner", "first", NOW)
    r = _upload(client, good_files())
    assert "Files look good" in r.text
    status, summary, error = _wait_job()
    assert status == "done", error
    assert summary["customers"] == 30 and summary["categories"] == 2 and sum(summary["groups"].values()) == 30
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(CustomerFeature).where(CustomerFeature.tenant_id == tid)) == 30
        latest = s.scalars(select(RulesVersion).where(RulesVersion.tenant_id == tid)
                           .order_by(RulesVersion.id.desc())).first()
    assert latest.note == "Adjusted to your new product categories"
    caps = yaml.safe_load(latest.content)["limits"]["max_discount_pct"]
    assert set(caps) == {"fashion", "grocery", "any"} and caps["grocery"] <= 12
    assert current_policy("kesar-fashions").catalog.labels == {"fashion": "Fashion", "grocery": "Grocery"}
    page = client.get("/app/customers?q=customer 7").text
    assert "Customer 7" in page
    assert "Customer 1" in client.get("/app/data").text or "30" in client.get("/app/data").text


def test_bad_upload_changes_nothing(client):
    files = good_files()
    files["orders"] += b"X1,NOPE,2026-01-01,delivered,0\n"
    r = _upload(client, files)
    assert "Nothing was imported" in r.text and "NOPE" in r.text
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ImportJob)) == 0
        assert s.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == _tid())) == 0


def test_upload_needs_confirmation(client):
    assert "Please tick the box" in _upload(client, good_files(), confirm=False).text


def test_demo_data(client):
    client.post("/app/data/demo", data={"size": "500", "confirm": "yes"}, headers=SAME)
    status, summary, error = _wait_job(timeout=120)
    assert status == "done", error
    assert summary["customers"] == 500
    with session_scope() as s:
        refs = s.scalars(select(Customer.external_ref).where(Customer.tenant_id == _tid()).limit(3)).all()
    assert all(r.startswith("KF") for r in refs)  # the business's own prefix


def test_template_download_and_isolation(client):
    r = client.get("/app/data/templates/orders.csv")
    assert r.status_code == 200 and r.text.startswith("order_id,customer_id,created_at")
    _upload(client, good_files(5))
    _wait_job()
    client.post("/app/logout", headers=SAME)
    client.post("/app/register", data={"business_name": "Beta Bazaar", "email": "b@beta.example",
                                       "password": "sup3r-secret", "confirm": "sup3r-secret"}, headers=SAME)
    assert "Customer 3" not in client.get("/app/customers").text
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == _tid())) == 5
