"""Role-based access, private ID documents, partner isolation, privacy, audit log, no JavaScript."""
import os
import re

import pytest

from conftest import PDF, login, upload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_anonymous_redirected_from_private_areas(client):
    for url in ("/member/", "/admin/", "/partner/", "/admin/finance/ledger", "/account"):
        r = client.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url


def test_member_cannot_use_staff_or_partner_pages(client, factory):
    m = factory.member()
    login(client, m.email)
    for url in ("/admin/", "/admin/members", "/admin/finance/ledger", "/admin/audit", "/partner/", "/admin/settings"):
        assert client.get(url).status_code == 403, url


def test_auditor_is_read_only(client, factory):
    au = factory.staff("auditor")
    login(client, au.email)
    for url in ("/admin/", "/admin/finance/ledger", "/admin/finance/", "/admin/audit", "/admin/reports/",
                "/admin/distributions/", "/admin/members"):
        assert client.get(url).status_code == 200, url
    for url, data in (("/admin/finance/transactions/new", {"type": "opening_balance", "amount": "1"}),
                      ("/admin/finance/payments/manual", {}), ("/admin/projects/new", {}),
                      ("/admin/settings", {"section": "general"}), ("/admin/distributions/new", {})):
        assert client.post(url, data=data).status_code == 403, url


def test_project_manager_cannot_touch_finance(client, factory):
    pm = factory.staff("project_manager")
    login(client, pm.email)
    assert client.get("/admin/finance/ledger").status_code == 403
    assert client.get("/admin/members").status_code == 403
    assert client.get("/admin/projects/").status_code == 200


def test_id_documents_are_private(client, app, factory):
    from extensions import db
    from models import IDDocument
    from services import file_service
    m = factory.member()
    other = factory.member()
    with app.test_request_context():
        saved = file_service.save_bytes(PDF, "id_documents", "pdf")
    doc = IDDocument(member_id=m.id, original_name="id.pdf", **saved)
    db.session.add(doc)
    db.session.commit()
    url = f"/admin/members/{m.id}/id-document/{doc.id}"
    assert client.get(url).status_code == 302                        # anonymous
    login(client, other.email)
    assert client.get(url).status_code == 403                        # another member
    client.post("/logout")
    login(client, m.email)
    r = client.get("/member/id-document")                            # the owner
    assert r.status_code == 200 and r.data == PDF
    assert r.headers["Cache-Control"] == "private, no-store" and "noindex" in r.headers["X-Robots-Tag"]
    client.post("/logout")
    login(client, factory.staff("finance_officer").email)
    assert client.get(url).status_code == 403                        # finance cannot open ID documents
    client.post("/logout")
    login(client, factory.staff("super_admin").email)
    r = client.get(url)
    assert r.status_code == 200 and r.data == PDF                    # super admin can, and it is logged
    from models import AuditLog
    assert AuditLog.query.filter_by(action="member.id_document_viewed").count() == 1


def test_partner_sees_only_assigned_projects(client, app, factory):
    from models import Project
    from services import project_service as P
    org_a, user_a = factory.partner("A")
    org_b, user_b = factory.partner("B")
    pm = factory.staff("project_manager")
    pa = P.create_project({"name": "Project A", "description": "Assigned to partner A for testing.",
                           "category": "Retail", "partner_id": org_a.id}, pm)
    pb = P.create_project({"name": "Project B", "description": "Assigned to partner B for testing.",
                           "category": "Retail", "partner_id": org_b.id}, pm)
    from extensions import db
    db.session.commit()
    login(client, user_a.email)
    assert client.get(f"/partner/projects/{pa.id}").status_code == 200
    assert client.get(f"/partner/projects/{pb.id}").status_code == 404
    assert client.post(f"/partner/projects/{pb.id}/action", data={"kind": "update", "body": "x"}).status_code == 404
    assert b"Project B" not in client.get("/partner/").data
    for url in ("/admin/members", "/admin/finance/", "/member/", f"/admin/projects/{pa.id}"):
        assert client.get(url).status_code == 403, url
    # documents of other partners' projects are hidden too
    with app.test_request_context():
        from services import file_service
        saved = file_service.save_bytes(PDF, "project_documents", "pdf")
    from models import Document
    d = Document(project_id=pb.id, kind="report", title="Secret", **saved)
    db.session.add(d)
    db.session.commit()
    assert client.get(f"/documents/{d.id}").status_code == 404


def test_public_pages_never_show_private_member_data(client, factory):
    m = factory.member(email="private.person@example.com", phone="254799887766")
    factory.pay(m, 200000)
    for url in ("/", "/transparency", "/projects", "/reports", "/partners", "/about"):
        body = client.get(url).get_data(as_text=True)
        for secret in ("private.person@example.com", "254799887766", m.id_number, m.member_number):
            assert secret not in body, (url, secret)


def test_published_report_hides_member_identity(client, app, factory):
    from datetime import date
    from extensions import db
    from models import Report
    m = factory.member()
    factory.pay(m, 200000)
    year = date.today().year
    r = Report(report_type="annual_financial", title="Annual", period_start=date(year, 1, 1), period_end=date(year, 12, 31))
    db.session.add(r)
    db.session.commit()
    for fmt in ("", ".csv"):
        body = client.get(f"/reports/{r.id}{fmt}").get_data(as_text=True)
        assert "Member contribution" in body and m.member_number not in body
    assert client.get(f"/reports/{r.id}.pdf").data[:4] == b"%PDF"


def test_audit_log_records_admin_actions(client, app, factory):
    from models import AuditLog
    admin = factory.staff("super_admin")
    login(client, admin.email)
    client.post("/admin/settings", data={"section": "contributions", "contribution_amount": "12,000",
                                         "min_payment": "50", "contribution_period": "one-off",
                                         "contribution_period_label": "Membership", "contribution_due_date": ""})
    entry = AuditLog.query.filter_by(action="settings.updated").one()
    assert entry.user_email == admin.email and entry.role == "super_admin"
    assert "1200000" in entry.new_value and entry.ip_address
    assert client.get("/admin/audit").status_code == 200


def test_maintenance_mode(client, factory):
    from extensions import db
    from services.settings_service import set_setting
    set_setting("maintenance_mode", "1")
    db.session.commit()
    assert client.get("/").status_code == 503
    assert client.get("/login").status_code == 200
    login(client, factory.staff("super_admin").email)
    assert client.get("/admin/").status_code == 200


def test_error_pages(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and b"Page not found" in r.data


def test_no_javascript_or_php_anywhere():
    bad_files, script_tags = [], []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "__pycache__", ".pytest_cache", "uploads", "instance")]
        for f in files:
            if f.endswith((".js", ".mjs", ".php", ".ts", ".jsx", ".tsx")):
                bad_files.append(os.path.join(folder, f))
            if f.endswith(".html"):
                text = open(os.path.join(folder, f), encoding="utf-8").read().lower()
                if "<script" in text or re.search(r"\son[a-z]+\s*=", text) or "javascript:" in text:
                    script_tags.append(f)
    assert not bad_files, bad_files
    assert not script_tags, script_tags
    assert not os.path.exists(os.path.join(ROOT, "package.json"))
