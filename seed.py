"""DEMO / TEST DATA for Youth Enterprise & Development Network (YEDN). Run:  flask --app app seed-demo

Everything created here is clearly labelled [DEMO]. Do not run this on a
real organisation's database. All demo accounts use the password printed at
the end.
"""
import io
import struct
import zlib
from datetime import date, datetime, timedelta

import click

from extensions import db
from helpers import utcnow
from models import (Announcement, Complaint, Contribution, Document, IDDocument, Meeting, MeetingAttendance, Member,
                    Payment, Project, ProjectMilestone, ProjectPartner, Reconciliation, Report, Role, SupportTicket,
                    Transaction, User, Vote, VoteBallot)
from permissions import ROLE_LABELS
from services import (contribution_service, distribution_service, file_service, governance_service, ledger_service,
                      payment_service, project_service)
from services.settings_service import set_setting

import os

# Can be changed with the DEMO_PASSWORD environment variable (recommended on a public test site).
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "") or "DemoPass2026!"

ROLE_DESCRIPTIONS = {
    "super_admin": "Full control of the system.",
    "finance_officer": "Financial records, reconciliation and distributions.",
    "project_manager": "Projects, milestones and project reports.",
    "auditor": "Read-only access to financial records, reports and audit logs.",
    "partner": "Access only to assigned projects.",
    "member": "Member self-service.",
}


def ensure_roles():
    for name, label in ROLE_LABELS.items():
        if not Role.query.filter_by(name=name).first():
            db.session.add(Role(name=name, label=label, description=ROLE_DESCRIPTIONS[name]))
    db.session.flush()


def _png():
    """A tiny valid grey PNG used as a placeholder 'ID document'."""
    width = height = 8
    raw = b"".join(b"\x00" + b"\xcc\xcc\xcc" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _pdf(text):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 760, "DEMO DOCUMENT - NOT A REAL RECORD")
    c.drawString(72, 740, text)
    c.save()
    return buf.getvalue()


def _user(email, name, role):
    u = User(email=email, full_name=f"[DEMO] {name}", role=Role.query.filter_by(name=role).one())
    u.set_password(DEMO_PASSWORD)
    db.session.add(u)
    db.session.flush()
    return u


def _days_ago(n, hour=10):
    return datetime.combine(date.today() - timedelta(days=n), datetime.min.time()).replace(hour=hour)


def _backdate_payment(payment, when):
    payment.created_at = when - timedelta(minutes=2)
    payment.verified_at = when
    if payment.transaction:
        payment.transaction.txn_date = when.date()
        payment.transaction.posted_at = when
        payment.transaction.created_at = when


def _backdate_txn(txn, when):
    txn.txn_date, txn.posted_at, txn.created_at = when.date(), when, when


def _sandbox_pay(member, amount_cents, when, approve=True):
    contribution = member.contributions[0]
    payment = payment_service.start_payment(member, contribution, amount_cents, member.phone, None, member.user)
    payment_service.sandbox_complete(payment.provider_checkout_id, approve, member.user)
    db.session.flush()
    if approve:
        _backdate_payment(payment, when)
    else:
        payment.created_at = when
    db.session.flush()
    return payment


def run_seed():
    if User.query.filter_by(email="superadmin@demo.org").first():
        click.echo("Demo data is already loaded. Nothing to do.")
        return
    ensure_roles()
    today = date.today()
    deadline = today - timedelta(days=100)
    settings = {
        # Identity: the real YEDN name and tagline. Contact details are left EMPTY on purpose -
        # administrators enter the official ones in Settings (they are never hard-coded).
        "organization_name": "Youth Enterprise & Development Network", "organization_short_name": "YEDN",
        "is_demo_data": "1", "contribution_amount_cents": "1000000",
        "contribution_period": "one-off", "contribution_period_label": "Membership contribution",
        "contribution_due_date": deadline.isoformat(), "min_payment_cents": "1000",
        "require_two_person_approval": "1",
        "dist_eligibility_basis": "fully_paid", "dist_allocation_method": "equal", "dist_late_policy": "future_only",
        "dist_rules_notes": "DEMO RULES ONLY. A real organisation must replace these with the rules its members "
                            "have approved (for example in the constitution or at a general meeting).",
    }
    for k, v in settings.items():
        set_setting(k, v)

    admin = _user("superadmin@demo.org", "Grace Super Admin", "super_admin")
    finance = _user("finance@demo.org", "Peter Finance Officer", "finance_officer")
    pm = _user("pm@demo.org", "Amina Project Manager", "project_manager")
    _user("auditor@demo.org", "Joseph Auditor", "auditor")
    partner_org = ProjectPartner(name="[DEMO] Greenfields Farmers Cooperative",
                                 description="Demo partner organisation that runs agricultural projects.",
                                 contact_person="Demo Contact", contact_email="partner@demo.org",
                                 contact_phone="254700000099", website="", is_public=True)
    db.session.add(partner_org)
    db.session.flush()
    partner_user = _user("partner@demo.org", "Samuel Partner", "partner")
    partner_user.partner_id = partner_org.id

    # ---- members
    people = [("Wanjiku Kamau", 260), ("Otieno Odhiambo", 250), ("Chebet Langat", 240),
              ("Mwangi Njoroge", 230), ("Akinyi Achieng", 220)]
    members = []
    for i, (name, joined_days) in enumerate(people, start=1):
        u = User(email=f"member{i}@demo.org", full_name=f"[DEMO] {name}", role=Role.query.filter_by(name="member").one())
        u.set_password(DEMO_PASSWORD)
        db.session.add(u)
        db.session.flush()
        m = Member(user_id=u.id, id_number=f"DEMO{i:05d}", phone=f"2547000000{i:02d}", status="active",
                   is_demo=True, joined_at=_days_ago(joined_days), admitted_at=_days_ago(joined_days),
                   terms_accepted_at=_days_ago(joined_days))
        db.session.add(m)
        db.session.flush()
        m.member_number = f"MBR-{m.id:05d}"
        saved = file_service.save_bytes(_png(), "id_documents", "png")
        db.session.add(IDDocument(member_id=m.id, original_name="demo-id.png", uploaded_at=_days_ago(joined_days), **saved))
        c = contribution_service.ensure_current_contribution(m)
        c.created_at = _days_ago(joined_days)
        members.append(m)
    db.session.flush()
    m1, m2, m3, m4, m5 = members

    # ---- contributions (the spec's example: 2,000 + 3,000 + 5,000 = 10,000)
    _sandbox_pay(m1, 200000, _days_ago(200))
    _sandbox_pay(m1, 300000, _days_ago(170))
    _sandbox_pay(m1, 500000, _days_ago(150))
    bank = payment_service.record_manual_payment(m2, m2.contributions[0], 1000000, "bank", "DEMO-SLIP-0001",
                                                 finance, "Demo bank deposit")
    payment_service.verify_manual_payment(bank, admin)
    _backdate_payment(bank, _days_ago(160))
    _sandbox_pay(m3, 400000, _days_ago(120))
    _sandbox_pay(m4, 300000, _days_ago(30), approve=False)  # a declined attempt: m4 remains unpaid
    _sandbox_pay(m5, 600000, _days_ago(95))
    _sandbox_pay(m5, 400000, _days_ago(90))                  # completed AFTER the deadline (late)
    db.session.flush()

    fee = ledger_service.create_transaction("bank_fee", 15000, "[DEMO] Monthly bank charges", finance,
                                            cash_account="CASH_BANK", txn_date=_days_ago(140).date())
    ledger_service.approve(fee, admin, "Demo bank statement")
    _backdate_txn(fee, _days_ago(140))

    # ---- project A: full workflow, active, with a distribution
    a = project_service.create_project({
        "name": "[DEMO] Poultry Farm Expansion", "category": "Agriculture", "location": "Kericho (demo)",
        "description": "Demo project: expand a layer-poultry unit from 500 to 1,500 birds and sell eggs to local schools.",
        "risks": "Disease outbreaks, feed price increases, egg price changes.", "partner_id": partner_org.id,
        "manager_id": pm.id, "requested_cents": 3000000, "budget_cents": 2500000,
        "start_date": (today - timedelta(days=135)), "expected_end_date": today + timedelta(days=200),
        "is_public": True, "is_demo": True}, pm)
    a.created_at = _days_ago(180)
    project_service.record_review(a, "review", "Demo review: proposal is complete and fits our objectives.", pm)
    project_service.transition(a, "submit_review", pm)
    project_service.transition(a, "start_due_diligence", pm)
    project_service.record_review(a, "financial", "Demo financial review: budget checked; recommend KSh 25,000.",
                                  finance, approved_cents=2500000)
    project_service.record_review(a, "risk", "Demo risk assessment: medium risk, vaccination plan in place.", pm, "Medium")
    project_service.transition(a, "approve", admin)
    project_service.transition(a, "request_funding", pm)
    rel = project_service.release_funding(a, 2000000, "CASH_MPESA", finance, "First tranche")
    ledger_service.approve(rel, admin, "Demo funding approval minute 3/2026")
    _backdate_txn(rel, _days_ago(130))
    for title, days, status in (("Buy 1,000 chicks", -125, "Completed"), ("Build second housing unit", -80, "Completed"),
                                ("First full laying cycle", 60, "In Progress")):
        db.session.add(ProjectMilestone(project_id=a.id, title=title, due_date=today + timedelta(days=days),
                                        status=status, updated_by_id=partner_user.id,
                                        completed_at=_days_ago(-days) if status == "Completed" else None))
    for desc, amount, days in (("Chicks and vaccines", 1200000, 125), ("Housing materials", 300000, 85)):
        e = project_service.report_expense(a, {"description": f"[DEMO] {desc}", "amount_cents": amount,
                                               "expense_date": today - timedelta(days=days), "category": "Inputs"},
                                           partner_user)
        project_service.verify_expense(e, finance)
    r = project_service.report_revenue(a, {"description": "[DEMO] Egg sales to schools", "amount_cents": 2200000,
                                           "revenue_date": today - timedelta(days=20), "source": "Egg sales"},
                                       partner_user)
    project_service.verify_revenue(r, finance, "CASH_BANK")
    project_service.add_timeline(a, "progress", "Progress update (70%)", "Demo: second unit built, laying started.",
                                 partner_user, 70)
    a.progress_percent = 70

    run = distribution_service.create_run(a, 600000, today, "[DEMO] Poultry first distribution", finance,
                                          "Demo: shows the future-only rule excluding a late completer.")
    distribution_service.approve_run(run, admin)
    first = run.distributions[0]
    distribution_service.mark_paid(first, "mpesa", "DEMOMPESA01", "CASH_BANK", finance)

    # ---- project B: completed
    b = project_service.create_project({
        "name": "[DEMO] Community Water Kiosk", "category": "Retail", "location": "Litein (demo)",
        "description": "Demo project: a solar-pumped water kiosk selling clean water by the jerrycan.",
        "risks": "Pump breakdown, low demand in the rainy season.", "partner_id": None, "manager_id": pm.id,
        "requested_cents": 500000, "budget_cents": 500000, "start_date": today - timedelta(days=70),
        "expected_end_date": today - timedelta(days=10), "is_public": True, "is_demo": True}, pm)
    project_service.record_review(b, "review", "Demo review: small, low-risk pilot.", pm)
    project_service.transition(b, "submit_review", pm)
    project_service.transition(b, "start_due_diligence", pm)
    project_service.record_review(b, "financial", "Demo financial review: approve KSh 5,000.", finance, approved_cents=500000)
    project_service.record_review(b, "risk", "Demo risk assessment: low risk.", pm, "Low")
    project_service.transition(b, "approve", admin)
    rel_b = project_service.release_funding(b, 500000, "CASH_BANK", finance, "Full funding")
    ledger_service.approve(rel_b, admin, "Demo approval")
    _backdate_txn(rel_b, _days_ago(60))
    e = project_service.report_expense(b, {"description": "[DEMO] Pump, tank and fittings", "amount_cents": 480000,
                                           "expense_date": today - timedelta(days=55), "category": "Equipment"}, pm)
    project_service.verify_expense(e, finance)
    rv = project_service.report_revenue(b, {"description": "[DEMO] Water sales", "amount_cents": 600000,
                                            "revenue_date": today - timedelta(days=12), "source": "Water sales"}, pm)
    project_service.verify_revenue(rv, finance, "CASH_BANK")
    saved = file_service.save_bytes(_pdf("Water kiosk final report (demo)"), "project_documents", "pdf")
    db.session.add(Document(project_id=b.id, kind="final_report", title="[DEMO] Final report", is_public=True,
                            original_name="final-report-demo.pdf", uploaded_by_id=pm.id, **saved))
    db.session.flush()
    project_service.transition(b, "complete", pm)

    # ---- reconciliation, governance, support, reports
    db.session.flush()
    bal = ledger_service.account_balance("CASH_BANK")
    db.session.add(Reconciliation(account="CASH_BANK", statement_date=today, statement_balance_cents=bal,
                                  ledger_balance_cents=bal, difference_cents=0, notes="[DEMO] Matches demo statement.",
                                  reconciled_by_id=finance.id))
    set_setting("last_reconciled_at", utcnow().isoformat(timespec="minutes"))

    held = Meeting(title="[DEMO] Annual General Meeting", meeting_at=_days_ago(45, 14), location="Demo Hall",
                   agenda="1. Accounts\n2. Poultry project update\n3. AOB", status="Held",
                   minutes="Demo minutes: members reviewed the accounts.",
                   decisions="Approved the water kiosk pilot.", follow_up_actions="Finance to publish quarterly report.",
                   created_by_id=admin.id)
    upcoming = Meeting(title="[DEMO] Quarterly members' meeting", meeting_at=_days_ago(-14, 14),
                       location="Demo Hall", online_link="https://meet.example.org/demo",
                       agenda="1. Distribution report\n2. New proposals", created_by_id=admin.id)
    db.session.add_all([held, upcoming])
    db.session.flush()
    for m, status in ((m1, "Attended"), (m2, "Attended"), (m3, "Absent")):
        db.session.add(MeetingAttendance(meeting_id=held.id, member_id=m.id, status=status))

    vote = Vote(title="[DEMO] Fund a boda boda transport pool?", description="Demo proposal to buy two motorcycles.",
                budget_cents=30000000, risks="Accidents, theft, licensing.", eligibility="fully_paid",
                opens_at=_days_ago(3), closes_at=_days_ago(-7), quorum_percent=50, pass_percent=50,
                created_by_id=admin.id)
    db.session.add(vote)
    db.session.flush()
    db.session.add(VoteBallot(vote_id=vote.id, member_id=m1.id, choice="yes"))

    db.session.add(Announcement(title="[DEMO] Welcome to the demo site",
                                body="This website is filled with demo data so you can explore every feature.",
                                is_public=True, created_by_id=admin.id))
    governance_service.open_ticket(m3.user, "[DEMO] When is the next deadline?", "Contributions",
                                   "Demo ticket: I would like to know when I must finish my contribution.")
    governance_service.open_complaint("[DEMO] Visitor", "visitor@demo.org", "", "[DEMO] Slow reply",
                                      "General enquiry", "Demo complaint: I waited a week for a reply to my email.")
    for rtype in ("annual_financial", "project_performance"):
        start, end = (date(today.year, 1, 1), date(today.year, 12, 31)) if rtype == "annual_financial" \
            else (date(today.year, 1, 1), today)
        db.session.add(Report(report_type=rtype, title=f"[DEMO] {rtype.replace('_', ' ').title()} {today.year}",
                              period_start=start, period_end=end, published_by_id=finance.id))
    db.session.commit()

    click.echo("Demo data loaded. All accounts use the password: " + DEMO_PASSWORD)
    for email in ("superadmin@demo.org", "finance@demo.org", "pm@demo.org", "auditor@demo.org", "partner@demo.org",
                  "member1@demo.org (fully paid)", "member2@demo.org (fully paid, bank)",
                  "member3@demo.org (partially paid)", "member4@demo.org (unpaid)", "member5@demo.org (paid late)"):
        click.echo("  " + email)


if __name__ == "__main__":
    # Lets you run `python seed.py` as well as `flask --app app seed-demo`.
    from app import app as flask_app
    with flask_app.app_context():
        run_seed()
