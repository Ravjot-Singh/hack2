import pytest

from app.config import Settings
from app.tenants.registry import load_tenants


def _write_tenant(root, folder, slug, enabled=True):
    d = root / folder
    d.mkdir()
    (d / "tenant.yaml").write_text(
        f"slug: {slug}\nname: Test {slug}\nenabled: {str(enabled).lower()}\nbot_token_env: X_TOKEN\n",
        encoding="utf-8",
    )


def test_template_and_disabled_tenants_are_skipped(tmp_path):
    _write_tenant(tmp_path, "_template", "your-slug")
    _write_tenant(tmp_path, "alpha", "alpha")
    _write_tenant(tmp_path, "beta", "beta", enabled=False)
    assert list(load_tenants(tmp_path)) == ["alpha"]


def test_slug_must_match_folder(tmp_path):
    _write_tenant(tmp_path, "alpha", "other")
    with pytest.raises(ValueError, match="must match the folder name"):
        load_tenants(tmp_path)


def test_bot_token_read_from_env(tmp_path, monkeypatch):
    _write_tenant(tmp_path, "alpha", "alpha")
    monkeypatch.setenv("X_TOKEN", "123:abc")
    assert load_tenants(tmp_path)["alpha"].bot_token == "123:abc"


@pytest.mark.parametrize("raw", ["postgres://u:p@h/db", "postgresql://u:p@h/db"])
def test_postgres_url_uses_psycopg3(raw):
    assert Settings(database_url=raw).database_url == "postgresql+psycopg://u:p@h/db"
