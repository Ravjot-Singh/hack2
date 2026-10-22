import pytest

from app.config import get_settings
from app.db.models import Base
from app.db.session import _session_factory, get_engine
from app.rules import policy as policy_module
from app.tenants.registry import clear_tenant_cache


@pytest.fixture(autouse=True)
def sqlite_db(tmp_path, monkeypatch):
    """Tests never touch Neon: point the app at a throwaway SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")  # never start real bots / register webhooks in tests
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("LLM_ENABLED", "false")  # tests never call Groq, whatever .env says
    monkeypatch.setenv("GROQ_API_KEY", "")
    for cached in (get_settings, get_engine, _session_factory):
        cached.cache_clear()
    policy_module._cache.clear()  # rules saved in one test's DB must not leak into the next
    clear_tenant_cache()
    Base.metadata.create_all(get_engine())
    yield
    get_engine().dispose()
    for cached in (get_settings, get_engine, _session_factory):
        cached.cache_clear()
