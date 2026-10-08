"""Shared test fixtures.

Run all tests:            pytest
Run against PostgreSQL:   TEST_DATABASE_URL=postgresql://user:pass@localhost/testdb pytest
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TestConfig  # noqa: E402

PASSWORD = "TestPass12345"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.fixture()
def app(tmp_path):
    class Cfg(TestConfig):
        UPLOAD_FOLDER = str(tmp_path / "uploads")
    from app import create_app
    from extensions import db
    application = create_app(Cfg)
    with application.app_context():
        db.drop_all()
        db.create_all()
        from seed import ensure_roles
        ensure_roles()
        from services.settings_service import set_setting
        set_setting("contribution_amount_cents", "1000000")  # KSh 10,000
        set_setting("min_payment_cents", "1000")
        db.session.commit()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def factory(app):
    return Factory(app)


class Factory:
    def __init__(self, app):
        self.app = app
        self.n = 0

    def staff(self, role, email=None):
        from extensions import db
        from models import Role, User
        self.n += 1
        u = User(email=email or f"{role}{self.n}@test.org", full_name=f"{role.title()} {self.n}",
                 role=Role.query.filter_by(name=role).one())
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u

    def member(self, email=None, phone=None, status="active"):
        from extensions import db
        from models import Member, Role, User
        from services import contribution_service
        self.n += 1
        u = User(email=email or f"member{self.n}@test.org", full_name=f"Member {self.n}",
                 role=Role.query.filter_by(name="member").one())
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        m = Member(user_id=u.id, id_number=f"ID{self.n:06d}", phone=phone or f"2547100000{self.n:02d}", status=status)
        db.session.add(m)
        db.session.flush()
        m.member_number = f"MBR-{m.id:05d}"
        contribution_service.ensure_current_contribution(m)
        db.session.commit()
        return m

    def pay(self, member, amount_cents, approve=True):
        """A full sandbox payment through the real service code path."""
        from extensions import db
        from services import payment_service
        p = payment_service.start_payment(member, member.contributions[0], amount_cents, member.phone, None, member.user)
        payment_service.sandbox_complete(p.provider_checkout_id, approve, member.user)
        db.session.commit()
        return p

    def partner(self, name="Partner Org"):
        from extensions import db
        from models import ProjectPartner
        p = ProjectPartner(name=name)
        db.session.add(p)
        db.session.commit()
        user = self.staff("partner")
        user.partner_id = p.id
        db.session.commit()
        return p, user

    def active_project(self, pm, approver, finance, finance2, partner=None, approved=2000000, release=1000000):
        """A project taken through the whole approval workflow and funded."""
        from extensions import db
        from services import ledger_service, project_service
        p = project_service.create_project({
            "name": "Test Farm", "description": "A test project description long enough.", "category": "Agriculture",
            "location": "Kericho", "partner_id": partner.id if partner else None, "manager_id": pm.id,
            "requested_cents": approved, "budget_cents": approved, "is_public": True}, pm)
        project_service.record_review(p, "review", "Looks fine", pm)
        project_service.transition(p, "submit_review", pm)
        project_service.transition(p, "start_due_diligence", pm)
        project_service.record_review(p, "financial", "OK", finance, approved_cents=approved)
        project_service.record_review(p, "risk", "Low", pm, "Low")
        project_service.transition(p, "approve", approver)
        if release:
            txn = project_service.release_funding(p, release, "CASH_MPESA", finance)
            ledger_service.approve(txn, finance2)
        db.session.commit()
        return p


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


def upload(data, name):
    return (io.BytesIO(data), name)
