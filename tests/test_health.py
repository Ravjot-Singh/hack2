from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Tenant
from app.db.session import session_scope
from app.main import app


def test_health_lists_tenants():
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "shopkart" in body["tenants"]


def test_health_db_and_tenant_sync():
    with TestClient(app) as client:
        assert client.get("/health/db").json()["database"] == "ok"
    with session_scope() as s:
        assert s.scalar(select(Tenant.name).where(Tenant.slug == "shopkart")) == "ShopKart"
