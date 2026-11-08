"""Desktop launcher: settings file and photo folder (no PostgreSQL started here)."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_launcher():
    spec = importlib.util.spec_from_file_location("aviationlog_desktop", ROOT / "desktop" / "aviationlog_desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_created_once_and_kept(tmp_path):
    d = load_launcher()
    first = d.load_config(tmp_path)
    assert first["port"] == d.DEFAULT_PORT
    assert len(first["secret_key"]) >= 32 and first["db_password"] and first["admin_password"]
    second = d.load_config(tmp_path)
    assert second == first  # secrets must not change between starts


def test_missing_keys_are_added(tmp_path):
    d = load_launcher()
    (tmp_path / "config.json").write_text(json.dumps({"port": 18190, "open_browser": False}))
    cfg = d.load_config(tmp_path)
    assert cfg["port"] == 18190 and cfg["open_browser"] is False
    assert json.loads((tmp_path / "config.json").read_text())["secret_key"] == cfg["secret_key"]


def test_photo_environment(tmp_path):
    d = load_launcher()
    assert d.photo_environment(tmp_path, {"photo_mode": "upload"}) == {
        "PHOTO_MODE": "upload", "PHOTO_DIR": str(tmp_path / "photos")}
    archive = str(tmp_path / "archive")
    assert d.photo_environment(tmp_path, {"photo_mode": "nas", "photo_dir": archive}) == {
        "PHOTO_MODE": "nas", "PHOTO_DIR": archive}
    # archive mode without a folder falls back to uploads
    assert d.photo_environment(tmp_path, {"photo_mode": "nas", "photo_dir": ""})["PHOTO_MODE"] == "upload"


def test_data_home_override(tmp_path, monkeypatch):
    d = load_launcher()
    monkeypatch.setenv("AVIATIONLOG_HOME", str(tmp_path / "x"))
    assert d.data_home() == tmp_path / "x"


def test_filename_from_disposition():
    d = load_launcher()
    assert d.filename_from_disposition('attachment; filename="aviationlog.csv"') == "aviationlog.csv"
    assert d.filename_from_disposition("attachment; filename*=UTF-8''L%C3%BCnen.ARW") == "Lünen.ARW"
    assert d.filename_from_disposition('attachment; filename="../../evil.sh"') == "evil.sh"
    assert d.filename_from_disposition(None) is None


def test_login_page_shows_desktop_start_password(db, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        assert "admin-password" not in c.get("/login").text  # server version: never
        monkeypatch.setenv("AVIATIONLOG_DESKTOP", "1")
        assert "admin-password" in c.get("/login").text
        # once the password was changed, the hint is gone
        import models
        import security

        admin = db.query(models.User).filter_by(username="admin").one()
        admin.password_hash = security.hash_password("something-else")
        db.commit()
        assert "admin-password" not in c.get("/login").text
