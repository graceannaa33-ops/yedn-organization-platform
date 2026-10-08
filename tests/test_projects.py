"""Projects: creation, workflow, funding, partner updates, expenses, revenue, profit/loss."""
import datetime

import pytest

from conftest import PDF, login, upload


@pytest.fixture()
def team(factory):
    return {"admin": factory.staff("super_admin"), "pm": factory.staff("project_manager"),
            "f1": factory.staff("finance_officer"), "f2": factory.staff("finance_officer")}


def _fund_org(factory, amount=3000000):
    m = factory.member()
    m.contributions[0].required_cents = 10000000
    factory.pay(m, amount)
    return m


def test_project_creation_via_form(client, team):
    login(client, team["pm"].email)
    r = client.post("/admin/projects/new", data={
        "name": "Water Kiosk", "description": "Selling clean water by the jerrycan in the market.",
        "category": "Retail", "location": "Litein", "requested": "50,000", "budget": "45000", "is_public": "on"})
    assert r.status_code == 302
    from models import Project, ProjectUpdate
    p = Project.query.one()
    assert p.status == "Proposed" and p.requested_cents == 5000000 and p.budget_cents == 4500000
    assert ProjectUpdate.query.filter_by(kind="proposal").count() == 1


def test_workflow_rules(app, team):
    from services import project_service as P
    p = P.create_project({"name": "Shop", "description": "A small retail shop project for testing.",
                          "category": "Retail", "requested_cents": 100000, "budget_cents": 100000}, team["pm"])
    P.transition(p, "submit_review", team["pm"])
    with pytest.raises(P.ProjectError, match="review notes"):
        P.transition(p, "start_due_diligence", team["pm"])
    P.record_review(p, "review", "ok", team["pm"])
    P.transition(p, "start_due_diligence", team["pm"])
    with pytest.raises(P.ProjectError, match="permission"):
        P.transition(p, "approve", team["pm"])          # project managers cannot approve
    with pytest.raises(P.ProjectError, match="financial review"):
        P.transition(p, "approve", team["admin"])
    P.record_review(p, "financial", "ok", team["f1"], approved_cents=90000)
    P.record_review(p, "risk", "ok", team["pm"], "Medium")
    P.transition(p, "approve", team["admin"])
    assert p.status == "Approved" and p.approved_cents == 90000
    with pytest.raises(P.ProjectError, match="Cannot 'Mark completed'"):
        P.transition(p, "complete", team["pm"])     # only an Active/Delayed project can complete


def test_funding_release_needs_second_approval_and_activates(app, factory, team):
    from services import ledger_service as L, project_service as P
    _fund_org(factory)
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"], release=0)
    with pytest.raises(P.ProjectError, match="exceeds approved"):
        P.release_funding(p, 2500000, "CASH_MPESA", team["f1"])
    txn = P.release_funding(p, 1500000, "CASH_MPESA", team["f1"])
    assert txn.approval_status == "pending" and p.status == "Approved"
    assert P.financials(p)["released"] == 0
    L.approve(txn, team["f2"])
    assert p.status == "Active" and P.financials(p)["released"] == 1500000
    assert L.account_balance("PROJECT_FUNDS") == 1500000


def test_partner_updates_expenses_revenue_and_net_result(client, app, factory, team):
    from extensions import db
    from models import ProjectExpense, ProjectRevenue
    from services import ledger_service as L, project_service as P
    _fund_org(factory)
    org, partner_user = factory.partner()
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"], partner=org)
    login(client, partner_user.email)
    assert client.get(f"/partner/projects/{p.id}").status_code == 200
    client.post(f"/partner/projects/{p.id}/action", data={"kind": "update", "update_kind": "progress",
                                                           "progress_percent": "40", "body": "Chicks delivered"})
    client.post(f"/partner/projects/{p.id}/action", data={"kind": "milestone", "title": "Build coop", "status": "Planned"})
    client.post(f"/partner/projects/{p.id}/action", data={
        "kind": "expense", "description": "Feed", "amount": "4,000", "date": "2026-05-01", "category": "Inputs",
        "receipt": upload(PDF, "receipt.pdf")}, content_type="multipart/form-data")
    client.post(f"/partner/projects/{p.id}/action", data={"kind": "revenue", "description": "Egg sales",
                                                           "amount": "6500", "date": "2026-06-01", "source": "Sales"})
    client.post(f"/partner/projects/{p.id}/action", data={"kind": "document", "doc_kind": "report", "title": "May report",
                                                           "file": upload(PDF, "r.pdf")}, content_type="multipart/form-data")
    db.session.refresh(p)
    assert p.progress_percent == 40 and len(p.milestones) == 1 and len(p.documents) == 2
    exp, rev = ProjectExpense.query.one(), ProjectRevenue.query.one()
    assert exp.status == rev.status == "Reported" and exp.receipt is not None
    assert P.financials(p)["net"] == 0      # unverified items do not count
    # finance verifies; the partner never can
    P.verify_expense(exp, team["f1"])
    P.verify_revenue(rev, team["f1"], "CASH_BANK")
    fin = P.financials(p)
    assert (fin["revenue"], fin["expenses"], fin["net"]) == (650000, 400000, 250000)
    assert fin["remaining_funds"] == 1000000 - 400000
    assert L.account_balance("PROJECT_REVENUE") == 650000 and L.ledger_is_balanced()


def test_expense_cannot_exceed_released_funds(app, factory, team):
    from services import project_service as P
    _fund_org(factory)
    org, partner_user = factory.partner()
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"], partner=org, release=100000)
    exp = P.report_expense(p, {"description": "Truck", "amount_cents": 500000, "expense_date": datetime.date.today()},
                           partner_user)
    with pytest.raises(P.ProjectError, match="exceeds"):
        P.verify_expense(exp, team["f1"])


def test_cannot_verify_own_report(app, factory, team):
    from services import project_service as P
    _fund_org(factory)
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"])
    exp = P.report_expense(p, {"description": "x", "amount_cents": 100, "expense_date": datetime.date.today()}, team["f1"])
    with pytest.raises(P.ProjectError, match="yourself"):
        P.verify_expense(exp, team["f1"])


def test_completion_and_loss_reporting(app, factory, team):
    from extensions import db
    from models import Document
    from services import project_service as P
    _fund_org(factory)
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"])
    exp = P.report_expense(p, {"description": "x", "amount_cents": 300000, "expense_date": datetime.date.today()}, team["pm"])
    P.verify_expense(exp, team["f1"])
    assert P.financials(p)["net"] == -300000     # a loss is shown as a loss
    db.session.add(Document(project_id=p.id, kind="final_report", title="Final", stored_name="x.pdf",
                            mime_type="application/pdf", size_bytes=1, sha256="0"))
    P.transition(p, "complete", team["pm"])
    assert p.status == "Completed" and p.progress_percent == 100


def test_project_pages_render(client, app, factory, team):
    _fund_org(factory)
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"])
    db_page = client.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Net result" in db_page and "Revenue is not profit" in db_page
    login(client, team["pm"].email)
    assert client.get(f"/admin/projects/{p.id}").status_code == 200
    r = client.post(f"/admin/projects/{p.id}/action", data={"kind": "transition", "action": "mark_delayed",
                                                            "reason": "Rains"}, follow_redirects=True)
    assert b"Delayed" in r.data
