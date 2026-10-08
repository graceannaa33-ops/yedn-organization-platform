"""Registration, login, logout, password security, 2FA, rate limiting, CSRF."""
import os

from conftest import PASSWORD, PDF, PNG, login, upload


def _register(client, **over):
    data = {"full_name": "Jane Wanjiru", "id_number": "12345678", "phone": "0712345678",
            "email": "jane@example.com", "password": "GoodPass2026", "confirm_password": "GoodPass2026",
            "agree": "yes", "id_document": upload(PDF, "id.pdf")}
    data.update(over)
    return client.post("/register", data=data, content_type="multipart/form-data")


def test_registration_creates_member_with_id_document(client, app):
    r = _register(client, profile_photo=upload(PNG, "me.png"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/member/")
    from models import Member
    with app.app_context():
        m = Member.query.one()
        assert m.member_number.startswith("MBR-")
        assert m.phone == "254712345678"
        assert m.current_id_document is not None
        assert m.current_id_document.mime_type == "application/pdf"
        assert m.profile_photo
        assert len(m.contributions) == 1 and m.contributions[0].required_cents == 1000000
    page = client.get("/member/").get_data(as_text=True)
    assert m.member_number in page and "Not Paid" in page


def test_registration_validation(client, app):
    assert _register(client, agree="").status_code == 400
    assert _register(client, password="short", confirm_password="short").status_code == 400
    assert _register(client, email="not-an-email").status_code == 400
    r = _register(client, id_document=(upload(b"MZ\x90\x00 fake exe", "id.png")))
    assert r.status_code == 400 and b"does not match" in r.data
    assert _register(client, id_document=upload(b"hello", "id.txt")).status_code == 400
    from models import User
    with app.app_context():
        assert User.query.count() == 0


def test_duplicate_id_number_and_email_rejected(client):
    assert _register(client).status_code == 302
    client.post("/logout")
    r = _register(client, email="other@example.com")
    assert r.status_code == 400 and b"ID number already exists" in r.data
    r = _register(client, id_number="99999999")
    assert r.status_code == 400 and b"email already exists" in r.data


def test_password_is_hashed(client, app):
    _register(client)
    from models import User
    with app.app_context():
        u = User.query.one()
        assert "GoodPass2026" not in u.password_hash
        assert u.check_password("GoodPass2026") and not u.check_password("wrong")


def test_login_and_logout(client, factory):
    m = factory.member()
    r = login(client, m.email)
    assert r.status_code == 302 and r.headers["Location"].endswith("/member/")
    assert client.get("/member/").status_code == 200
    client.post("/logout")
    r = client.get("/member/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_wrong_password_and_rate_limit(client, factory):
    m = factory.member()
    for _ in range(5):
        assert login(client, m.email, "wrong-password").status_code == 401
    r = login(client, m.email)  # even the right password is blocked now
    assert r.status_code == 429


def test_password_change_logs_out_other_sessions(app, factory):
    m = factory.member()
    a, b = app.test_client(), app.test_client()
    login(a, m.email)
    login(b, m.email)
    r = a.post("/account", data={"action": "password", "current_password": PASSWORD,
                                 "new_password": "BrandNew2026x", "confirm_password": "BrandNew2026x"})
    assert r.status_code == 302
    assert a.get("/member/").status_code == 200       # this session stays logged in
    assert b.get("/member/").status_code == 302       # the other one is logged out


def test_session_timeout(app, factory):
    m = factory.member()
    c = app.test_client()
    login(c, m.email)
    with c.session_transaction() as s:
        s["last_seen"] = s["last_seen"] - 3600
    assert c.get("/member/").status_code == 302


def test_staff_two_factor(app, factory):
    from services import twofactor_service
    from extensions import db
    admin = factory.staff("super_admin")
    c = app.test_client()
    login(c, admin.email)
    c.get("/account/two-factor")
    with c.session_transaction() as s:
        secret = s["totp_setup_secret"]
    r = c.post("/account/two-factor", data={"code": twofactor_service.current_code(secret)})
    assert r.status_code == 302
    with app.app_context():
        from models import User
        assert db.session.get(User, admin.id).totp_enabled
    c.post("/logout")
    r = login(c, admin.email)
    assert r.headers["Location"].endswith("/login/2fa")
    assert c.get("/admin/").status_code == 302          # not logged in until the code is entered
    r = c.post("/login/2fa", data={"code": "000000"})
    assert b"not correct" in r.data
    import time
    r = c.post("/login/2fa", data={"code": twofactor_service.current_code(secret, time.time() + 30)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")


def test_csrf_protection_enforced(tmp_path):
    from app import create_app
    from config import TestConfig

    class Csrf(TestConfig):
        WTF_CSRF_ENABLED = True
        UPLOAD_FOLDER = str(tmp_path)
    application = create_app(Csrf)
    with application.app_context():
        from extensions import db
        db.create_all()
        r = application.test_client().post("/login", data={"email": "x@y.z", "password": "x"})
        assert r.status_code == 400
        db.session.remove()
        db.drop_all()


def test_security_headers_block_javascript(client):
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_member_register_upload_stored_privately(client, app):
    _register(client)
    from models import IDDocument
    with app.app_context():
        doc = IDDocument.query.one()
        if app.config["FILE_STORAGE"] == "database":
            from models import StoredFile
            assert StoredFile.query.filter_by(stored_name=doc.stored_name).one().data == PDF
        else:
            path = os.path.join(app.config["UPLOAD_FOLDER"], "id_documents", doc.stored_name)
            assert os.path.isfile(path)
            assert "static" not in path
        assert doc.stored_name != "id.pdf" and len(doc.stored_name) > 30
    assert client.get(f"/static/{doc.stored_name}").status_code == 404
    assert client.get(f"/uploads/id_documents/{doc.stored_name}").status_code == 404
