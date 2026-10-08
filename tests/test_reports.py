"""Reports (HTML/CSV/PDF), member statements, publishing, meetings, voting, support, complaints."""
import datetime

import pytest

from conftest import login


def test_every_report_type_renders_in_all_formats(client, app, factory):
    from services.report_service import REPORT_TYPES
    m = factory.member()
    factory.pay(m, 300000)
    admin = factory.staff("super_admin")
    login(client, admin.email)
    for key in REPORT_TYPES:
        r = client.get(f"/admin/reports/{key}")
        assert r.status_code == 200, key
        csv = client.get(f"/admin/reports/{key}.csv")
        assert csv.status_code == 200 and csv.mimetype == "text/csv", key
        pdf = client.get(f"/admin/reports/{key}.pdf")
        assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF", key


def test_financial_report_figures(app, factory):
    from services import report_service
    m = factory.member()
    factory.pay(m, 300000)
    today = datetime.date.today()
    rep = report_service.build("annual_financial", datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31))
    summary = {k: str(v) for k, v in rep["summary"]}
    assert summary["Total verified income"] == "KSh 3,000"
    assert summary["Calculated closing position"] == "KSh 3,000"
    csv = report_service.to_csv(rep)
    assert "3000.00" in csv


def test_audit_report_needs_audit_permission(client, factory):
    login(client, factory.staff("finance_officer").email)
    assert client.get("/admin/reports/audit").status_code == 403


def test_private_reports_cannot_be_published(client, factory):
    login(client, factory.staff("super_admin").email)
    r = client.post("/admin/reports/contributions/publish", data={}, follow_redirects=True)
    assert b"cannot be published" in r.data
    r = client.post("/admin/reports/annual_financial/publish", data={"year": "2026"}, follow_redirects=True)
    assert b"Report published" in r.data
    assert b"Annual financial report" in client.get("/reports").data


def test_member_statement(client, app, factory):
    m = factory.member()
    factory.pay(m, 200000)
    factory.pay(m, 300000)
    login(client, m.email)
    page = client.get("/member/statement").get_data(as_text=True)
    assert "Opening contribution (required)" in page and "KSh 5,000" in page
    csv = client.get("/member/statement.csv").get_data(as_text=True)
    assert "Running total paid" in csv and "5000.00" in csv
    assert client.get("/member/statement.pdf").data[:4] == b"%PDF"


def test_csv_formula_injection_is_neutralised():
    from services.report_service import _safe_csv
    assert _safe_csv("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _safe_csv("-1500.00") == "-1500.00"


def test_meeting_rsvp_and_minutes(client, app, factory):
    from models import Meeting, MeetingAttendance
    admin = factory.staff("super_admin")
    m = factory.member()
    login(client, admin.email)
    when = (datetime.datetime.now() + datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
    client.post("/admin/meetings", data={"title": "AGM", "meeting_at": when, "agenda": "Accounts"})
    meeting = Meeting.query.one()
    client.post("/logout")
    login(client, m.email)
    client.post("/member/meetings", data={"meeting_id": meeting.id, "rsvp": "Attending"})
    assert MeetingAttendance.query.one().status == "Attending"
    client.post("/logout")
    login(client, admin.email)
    client.post(f"/admin/meetings/{meeting.id}", data={"action": "record", "status": "Held", "minutes": "Done",
                                                       "decisions": "Approved budget", "agenda": "Accounts"})
    client.post(f"/admin/meetings/{meeting.id}", data={"action": "attendance", f"att_{m.id}": "Attended"})
    assert Meeting.query.one().decisions == "Approved budget"
    assert MeetingAttendance.query.one().status == "Attended"


def test_voting(client, app, factory):
    from extensions import db
    from helpers import utcnow
    from models import Vote, VoteBallot
    from services import governance_service as G
    admin = factory.staff("super_admin")
    paid, unpaid = factory.member(), factory.member()
    factory.pay(paid, 1000000)
    v = Vote(title="Buy truck", description="d", opens_at=utcnow() - datetime.timedelta(hours=1),
             closes_at=utcnow() + datetime.timedelta(days=1), eligibility="fully_paid", created_by_id=admin.id)
    db.session.add(v)
    db.session.commit()
    login(client, unpaid.email)
    client.post(f"/member/votes/{v.id}", data={"choice": "yes"})
    assert VoteBallot.query.count() == 0                 # not eligible
    client.post("/logout")
    login(client, paid.email)
    client.post(f"/member/votes/{v.id}", data={"choice": "yes"})
    client.post(f"/member/votes/{v.id}", data={"choice": "no"})   # cannot vote twice
    assert VoteBallot.query.count() == 1
    G.close_vote(v, admin)
    assert (v.yes_count, v.eligible_count, v.result) == (1, 1, "Passed")


def test_support_ticket_and_complaint(client, app, factory):
    from models import Complaint, SupportTicket
    m = factory.member()
    staff = factory.staff("project_manager")
    login(client, m.email)
    client.post("/member/support", data={"subject": "Receipt", "category": "Payments",
                                         "description": "I did not get my SMS receipt."})
    t = SupportTicket.query.one()
    client.post("/logout")
    login(client, staff.email)
    client.post(f"/admin/support/{t.id}", data={"assigned_to_id": staff.id, "status": "Resolved",
                                                "body": "Resent the receipt.", "resolution": "Resent"})
    assert t.status == "Resolved" and t.assigned_to_id == staff.id
    client.post("/logout")
    r = client.post("/complaints", data={"name": "Visitor", "email": "v@example.com", "subject": "Slow",
                                         "category": "Other", "description": "Nobody answered the phone."})
    c = Complaint.query.one()
    assert c.complaint_number.encode() in r.data
    r = client.post("/complaints/status", data={"reference": c.complaint_number, "email": "v@example.com"})
    assert b"Submitted" in r.data
    r = client.post("/complaints/status", data={"reference": c.complaint_number, "email": "x@example.com"})
    assert b"No complaint matches" in r.data
