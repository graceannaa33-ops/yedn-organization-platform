"""Production / Render readiness: WSGI entry point, health check, database file storage,
start-up command, test-mode payments and secrets."""
import os
import subprocess
import sys

import pytest

from conftest import PDF, PNG, login, upload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_wsgi_entry_point_exposes_app():
    out = subprocess.run([sys.executable, "-c", "import wsgi; print(wsgi.app.name)"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "app"


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}


def test_production_refuses_default_secret_key():
    env = dict(os.environ, APP_ENV="production", SECRET_KEY="", DATABASE_URL="sqlite://")
    out = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode != 0 and "SECRET_KEY" in out.stderr


@pytest.fixture()
def db_storage(app):
    app.config["FILE_STORAGE"] = "database"
    return app


def test_uploads_in_database_survive_without_disk(client, db_storage, tmp_path):
    import shutil
    from models import IDDocument, StoredFile
    r = client.post("/register", data={
        "full_name": "Test Person", "id_number": "TEST12345", "phone": "0711111111", "email": "t@example.com",
        "password": "GoodPass2026", "confirm_password": "GoodPass2026", "agree": "yes",
        "id_document": upload(PDF, "id.pdf"), "profile_photo": upload(PNG, "me.png")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    doc = IDDocument.query.one()
    assert StoredFile.query.count() == 2 and StoredFile.query.filter_by(stored_name=doc.stored_name).one().data == PDF
    # Simulate Render wiping the disk on restart: files must still be served from the database
    shutil.rmtree(db_storage.config["UPLOAD_FOLDER"], ignore_errors=True)
    r = client.get("/member/id-document")
    assert r.status_code == 200 and r.data == PDF and r.headers["Cache-Control"] == "private, no-store"
    assert client.get("/member/photo").data == PNG


def test_failed_registration_leaves_no_orphan_files(client, db_storage):
    from models import StoredFile
    r = client.post("/register", data={
        "full_name": "Test Person", "id_number": "TEST12345", "phone": "0711111111", "email": "t@example.com",
        "password": "GoodPass2026", "confirm_password": "GoodPass2026", "agree": "yes",
        "id_document": upload(PDF, "id.pdf"), "profile_photo": upload(b"not an image", "me.png")},
        content_type="multipart/form-data")
    assert r.status_code == 400
    from extensions import db
    db.session.rollback()
    assert StoredFile.query.count() == 0


def test_prepare_deploy_is_repeatable_and_creates_admin(app):
    from models import Member, Role, User
    app.config.update(SEED_DEMO_DATA=True, ADMIN_EMAIL="owner@example.com", ADMIN_PASSWORD="OwnerPass2026",
                      ADMIN_NAME="Owner")
    runner = app.test_cli_runner()
    for _ in range(2):                       # runs on every Render start: must be safe to repeat
        result = runner.invoke(args=["prepare-deploy"])
        assert result.exit_code == 0, result.output
    assert "Ready." in result.output and "already exists" in result.output
    admin = User.query.filter_by(email="owner@example.com").one()
    assert admin.role.name == "super_admin" and admin.check_password("OwnerPass2026")
    assert Member.query.count() == 5         # demo data loaded exactly once
    assert Role.query.count() == 6


def test_prepare_deploy_rejects_weak_admin_password(app):
    from models import User
    app.config.update(ADMIN_EMAIL="weak@example.com", ADMIN_PASSWORD="short")
    result = app.test_cli_runner().invoke(args=["prepare-deploy"])
    assert "too weak" in result.output and User.query.filter_by(email="weak@example.com").first() is None


def test_sandbox_payments_only_when_allowed_in_production(client, app, factory):
    m = factory.member()
    login(client, m.email)
    app.config["APP_ENV"] = "production"
    assert client.get("/payments/new").status_code == 503       # simulator off in production by default
    app.config["ALLOW_SANDBOX_PAYMENTS"] = True
    r = client.get("/payments/new")
    assert r.status_code == 200 and b"Payments are simulated" in r.data
    app.config["APP_ENV"] = "testing"


def test_demo_logins_hidden_unless_enabled(client, app):
    from extensions import db
    from services.settings_service import set_setting
    set_setting("is_demo_data", "1")
    db.session.commit()
    app.config["SHOW_DEMO_LOGINS"] = False
    assert b"Demo accounts" not in client.get("/login").data
    app.config["SHOW_DEMO_LOGINS"] = True
    assert b"Demo accounts" in client.get("/login").data


def test_no_secrets_committed():
    tracked_like = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "__pycache__", ".pytest_cache", "uploads", "instance")]
        for f in files:
            if f == ".env":
                tracked_like.append(os.path.join(folder, f))
    gitignore = open(os.path.join(ROOT, ".gitignore")).read()
    for needed in (".env", "venv/", "instance/", "*.db", "uploads/*", "__pycache__/"):
        assert needed in gitignore
    render = open(os.path.join(ROOT, "render.yaml")).read()
    assert "generateValue: true" in render and "sync: false" in render
    assert "DemoPass2026" not in render


def test_main_pages_work_in_production_mode(client, app):
    app.config["APP_ENV"] = "production"
    for url in ("/", "/login", "/register", "/transparency", "/membership", "/opportunities", "/projects",
                "/programs", "/what-we-do", "/about", "/contact"):
        assert client.get(url).status_code == 200, url
    app.config["APP_ENV"] = "testing"
