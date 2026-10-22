import pytest

from app.config import get_settings
from app.db.models import Base
from app.db.session import _session_factory, get_engine


@pytest.fixture(autouse=True)
def sqlite_db(tmp_path, monkeypatch):
    """Tests never touch Neon: point the app at a throwaway SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")  # never start real bots / register webhooks in tests
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    for cached in (get_settings, get_engine, _session_factory):
        cached.cache_clear()
    Base.metadata.create_all(get_engine())
    yield
    get_engine().dispose()
    for cached in (get_settings, get_engine, _session_factory):
        cached.cache_clear()
