"""Project workflow, funding and project finances.

Workflow:  Proposal -> Review -> Financial Review -> Risk Assessment ->
           Approval -> Funding -> Monitoring -> Final Report
Statuses:  Proposed, Under Review, Due Diligence, Approved, Funding Pending,
           Active, Delayed, Completed, Suspended, Cancelled

Net result = verified project revenue - verified project expenses.
Revenue is never called profit, and no result is ever guaranteed.
"""
from datetime import date

from sqlalchemy import func, select

from extensions import db
from helpers import format_money, new_reference, utcnow
from models import Document, Project, ProjectExpense, ProjectRevenue, ProjectUpdate, Transaction
from permissions import has_permission
from services import audit_service, ledger_service


class ProjectError(Exception):
    pass


# action: (allowed from statuses, new status, permission, label)
TRANSITIONS = {
    "submit_review": (("Proposed",), "Under Review", "manage_projects", "Send for review"),
    "start_due_diligence": (("Under Review",), "Due Diligence", "manage_projects", "Start due diligence"),
    "approve": (("Due Diligence",), "Approved", "approve_project", "Approve project"),
    "request_funding": (("Approved",), "Funding Pending", "manage_projects", "Mark funding pending"),
    "mark_delayed": (("Active",), "Delayed", "manage_projects", "Mark delayed"),
    "resume": (("Delayed",), "Active", "manage_projects", "Resume (back to Active)"),
    "suspend": (("Funding Pending", "Active", "Delayed"), "Suspended", "approve_project", "Suspend"),
    "reinstate": (("Suspended",), "Active", "approve_project", "Reinstate"),
    "complete": (("Active", "Delayed"), "Completed", "manage_projects", "Mark completed"),
    "cancel": (("Proposed", "Under Review", "Due Diligence", "Approved", "Funding Pending", "Suspended"),
               "Cancelled", "approve_project", "Cancel project"),
}


def available_actions(project, user):
    return [(key, t[3]) for key, t in TRANSITIONS.items()
            if project.status in t[0] and has_permission(user, t[2])]


def add_timeline(project, kind, title, body="", user=None, progress=None, public=True):
    upd = ProjectUpdate(project_id=project.id, kind=kind, title=title[:200], body=body or "",
                        progress_percent=progress, is_public=public,
                        created_by_id=user.id if user else None)
    db.session.add(upd)
    return upd


def create_project(data, user):
    project = Project(code=new_reference("PRJ")[:20], created_by_id=user.id, status="Proposed", **data)
    db.session.add(project)
    db.session.flush()
    add_timeline(project, "proposal", "Project proposed", project.description[:500], user)
    audit_service.log("project.created", "project", project.code, new={"name": project.name}, user=user)
    return project


def transition(project, action, user, reason=""):
    if action not in TRANSITIONS:
        raise ProjectError("Unknown action.")
    allowed_from, new_status, permission, label = TRANSITIONS[action]
    if not has_permission(user, permission):
        raise ProjectError("You do not have permission for this action.")
    if project.status not in allowed_from:
        raise ProjectError(f"Cannot '{label}' while the project is {project.status}.")
    if action == "start_due_diligence" and not project.review_notes:
        raise ProjectError("Record the review notes first.")
    if action == "approve":
        if not project.financial_review_notes:
            raise ProjectError("The financial review must be recorded before approval.")
        if not project.risk_assessment_notes:
            raise ProjectError("The risk assessment must be recorded before approval.")
        if project.approved_cents <= 0:
            raise ProjectError("Set the approved funding amount before approving.")
        if project.created_by_id == user.id and project.manager_id == user.id:
            raise ProjectError("The person who proposed and manages a project cannot approve it.")
        project.approved_by_id = user.id
        project.approved_at = utcnow()
    if action == "complete":
        has_final = Document.query.filter_by(project_id=project.id, kind="final_report").first()
        if not has_final:
            raise ProjectError("Upload the final report before marking the project completed.")
        project.actual_end_date = date.today()
        project.progress_percent = 100
    if action in ("mark_delayed", "suspend", "cancel") and not (reason or "").strip():
        raise ProjectError("Give a reason.")
    old = project.status
    project.status = new_status
    kind = {"approve": "approval", "mark_delayed": "delay", "complete": "completion"}.get(action, "decision")
    add_timeline(project, kind, f"{label}: {old} → {new_status}", reason, user)
    audit_service.log("project.status_changed", "project", project.code, old={"status": old},
                      new={"status": new_status}, reason=reason, user=user)


def record_review(project, kind, notes, user, risk_level=None, approved_cents=None):
    notes = (notes or "").strip()
    if not notes:
        raise ProjectError("Notes are required.")
    now = utcnow()
    if kind == "review":
        if project.status not in ("Proposed", "Under Review"):
            raise ProjectError("Review notes can only be recorded before due diligence.")
        project.review_notes, project.reviewed_by_id, project.reviewed_at = notes, user.id, now
        title = "Project review recorded"
    elif kind == "financial":
        if project.status != "Due Diligence":
            raise ProjectError("The financial review happens during due diligence.")
        if approved_cents is not None:
            if approved_cents <= 0:
                raise ProjectError("Recommended funding must be greater than zero.")
            project.approved_cents = approved_cents
        project.financial_review_notes, project.financial_reviewed_by_id, project.financial_reviewed_at = notes, user.id, now
        title = "Financial review recorded"
    elif kind == "risk":
        if project.status != "Due Diligence":
            raise ProjectError("The risk assessment happens during due diligence.")
        if risk_level not in ("Low", "Medium", "High"):
            raise ProjectError("Choose a risk level.")
        project.risk_assessment_notes, project.risk_level = notes, risk_level
        project.risk_assessed_by_id, project.risk_assessed_at = user.id, now
        title = f"Risk assessment recorded (risk: {risk_level})"
    else:
        raise ProjectError("Unknown review type.")
    add_timeline(project, "decision", title, notes, user, public=False)
    audit_service.log(f"project.{kind}_review", "project", project.code, new={"notes": notes[:200]}, user=user)


# ---------------------------------------------------------------- money
def _sum(model, project_id, status):
    q = select(func.coalesce(func.sum(model.amount_cents), 0)).where(model.project_id == project_id,
                                                                     model.status == status)
    return int(db.session.execute(q).scalar_one())


def pending_releases(project):
    q = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        Transaction.project_id == project.id, Transaction.type == "project_funding",
        Transaction.approval_status == "pending")
    return int(db.session.execute(q).scalar_one())


def financials(project):
    released = ledger_service.total_by_type("project_funding", project_id=project.id)
    spent = _sum(ProjectExpense, project.id, "Verified")
    revenue = _sum(ProjectRevenue, project.id, "Verified")
    from services.distribution_service import distributed_for_project
    distributed = distributed_for_project(project.id)
    return {
        "budget": project.budget_cents,
        "requested": project.requested_cents,
        "approved": project.approved_cents,
        "released": released,
        "pending_release": pending_releases(project),
        "unreleased": max(project.approved_cents - released, 0),
        "spent": spent,
        "remaining_funds": released - spent,
        "revenue": revenue,
        "expenses": spent,
        "net": revenue - spent,
        "budget_variance": project.budget_cents - spent,
        "reported_expenses": _sum(ProjectExpense, project.id, "Reported"),
        "reported_revenue": _sum(ProjectRevenue, project.id, "Reported"),
        "distributed": distributed,
    }


def release_funding(project, amount_cents, cash_account, user, note=""):
    if project.status not in ("Approved", "Funding Pending", "Active", "Delayed"):
        raise ProjectError(f"Funds cannot be released while the project is {project.status}.")
    fin = financials(project)
    room = project.approved_cents - fin["released"] - fin["pending_release"]
    if amount_cents > room:
        raise ProjectError(f"This exceeds approved funding still available to release ({format_money(room)}).")
    txn = ledger_service.create_transaction(
        "project_funding", amount_cents, f"Funding release to {project.name}" + (f" - {note}" if note else ""),
        user, cash_account=cash_account, project=project, reference=project.code, category="Project funding",
        auto_approve=not _two_person(), approver=user)
    audit_service.log("project.funding_requested", "project", project.code,
                      new={"amount_cents": amount_cents, "txn": txn.txn_number}, user=user)
    return txn


def _two_person():
    from services.settings_service import get_bool
    return get_bool("require_two_person_approval")


def on_funding_posted(txn, user):
    project = txn.project or db.session.get(Project, txn.project_id)
    if txn.is_reversal:
        add_timeline(project, "funding", f"Funding release reversed ({format_money(txn.amount_cents)})",
                     txn.description, user)
        return
    add_timeline(project, "funding", f"Funds released: {format_money(txn.amount_cents)}", txn.txn_number, user)
    if project.status in ("Approved", "Funding Pending"):
        old = project.status
        project.status = "Active"
        if not project.start_date:
            project.start_date = date.today()
        add_timeline(project, "decision", f"{old} → Active (first funds released)", "", user)
        audit_service.log("project.status_changed", "project", project.code, old={"status": old},
                          new={"status": "Active"}, reason="Funds released", user=user)


def first_funding_datetime(project_id):
    q = select(func.min(Transaction.posted_at)).where(Transaction.project_id == project_id,
                                                      Transaction.type == "project_funding",
                                                      Transaction.approval_status == "approved",
                                                      Transaction.is_reversal.is_(False))
    return db.session.execute(q).scalar_one()


def report_expense(project, data, user):
    exp = ProjectExpense(project_id=project.id, reported_by_id=user.id, status="Reported", **data)
    db.session.add(exp)
    db.session.flush()
    audit_service.log("project.expense_reported", "project_expense", exp.id,
                      new={"amount_cents": exp.amount_cents, "project": project.code}, user=user)
    return exp


def report_revenue(project, data, user):
    rev = ProjectRevenue(project_id=project.id, reported_by_id=user.id, status="Reported", **data)
    db.session.add(rev)
    db.session.flush()
    audit_service.log("project.revenue_reported", "project_revenue", rev.id,
                      new={"amount_cents": rev.amount_cents, "project": project.code}, user=user)
    return rev


def verify_expense(expense, user):
    if expense.status != "Reported":
        raise ProjectError("Only reported expenses can be verified.")
    if expense.reported_by_id == user.id:
        raise ProjectError("You cannot verify an expense you reported yourself.")
    fin = financials(expense.project)
    if expense.amount_cents > fin["remaining_funds"]:
        raise ProjectError(f"Expense exceeds the project's unspent released funds "
                           f"({format_money(fin['remaining_funds'])}). Release more funding first.")
    txn = ledger_service.create_transaction(
        "project_expense", expense.amount_cents, f"{expense.project.name}: {expense.description}", user,
        project=expense.project, reference=f"EXP-{expense.id}", category=expense.category or "Project expense",
        txn_date=expense.expense_date, auto_approve=True, verification_status="verified", approver=user)
    expense.status, expense.verified_by_id, expense.verified_at = "Verified", user.id, utcnow()
    expense.transaction_id = txn.id
    audit_service.log("project.expense_verified", "project_expense", expense.id, new={"txn": txn.txn_number}, user=user)


def verify_revenue(revenue, user, cash_account="CASH_BANK"):
    if revenue.status != "Reported":
        raise ProjectError("Only reported revenue can be verified.")
    if revenue.reported_by_id == user.id:
        raise ProjectError("You cannot verify revenue you reported yourself.")
    txn = ledger_service.create_transaction(
        "project_revenue", revenue.amount_cents, f"{revenue.project.name}: {revenue.description}", user,
        cash_account=cash_account, project=revenue.project, reference=f"REV-{revenue.id}",
        category=revenue.source or "Project revenue", txn_date=revenue.revenue_date,
        auto_approve=True, verification_status="verified", approver=user)
    revenue.status, revenue.verified_by_id, revenue.verified_at = "Verified", user.id, utcnow()
    revenue.cash_account = cash_account
    revenue.transaction_id = txn.id
    audit_service.log("project.revenue_verified", "project_revenue", revenue.id, new={"txn": txn.txn_number}, user=user)


def reject_item(item, user, reason):
    if item.status != "Reported":
        raise ProjectError("Only reported items can be rejected.")
    if not (reason or "").strip():
        raise ProjectError("Give a reason.")
    item.status, item.rejection_reason = "Rejected", reason.strip()[:255]
    item.verified_by_id, item.verified_at = user.id, utcnow()
    audit_service.log(f"project.{type(item).__name__.lower()}_rejected", type(item).__name__, item.id,
                      reason=reason, user=user)
