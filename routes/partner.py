"""Project Partner portal.

Partners see ONLY projects assigned to their partner organisation. They can
report progress, milestones, expenses, revenue and upload documents. They can
never see member data, organisation-wide finances, approve funding, or
verify their own expense/revenue reports (finance staff do that).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from extensions import db
from helpers import MoneyError, parse_date, parse_money
from models import Document, Project, ProjectExpense, ProjectMilestone, ProjectRevenue, ProjectUpdate
from permissions import role_required
from routes.projects import DOC_KINDS, MILESTONE_STATUSES, _save_milestone, _save_update, save_project_document
from services import file_service, project_service
from services.project_service import ProjectError

bp = Blueprint("partner", __name__, url_prefix="/partner")

SECTIONS = {"updates": "Project updates", "milestones": "Milestones", "expenses": "Expenses",
            "revenue": "Revenue", "reports": "Reports", "documents": "Documents"}
OPEN_STATUSES = ("Approved", "Funding Pending", "Active", "Delayed")


def _projects():
    if not g.user.partner_id:
        return []
    return Project.query.filter_by(partner_id=g.user.partner_id).order_by(Project.name).all()


def _own_project(project_id):
    project = db.session.get(Project, project_id)
    if project is None or not g.user.partner_id or project.partner_id != g.user.partner_id:
        abort(404)  # 404, not 403: do not reveal that other projects exist
    return project


@bp.route("/")
@role_required("partner")
def dashboard():
    rows = [(p, project_service.financials(p)) for p in _projects()]
    return render_template("partner/dashboard.html", rows=rows)


@bp.route("/projects/<int:project_id>")
@role_required("partner")
def project(project_id):
    p = _own_project(project_id)
    return render_template("partner/project.html", p=p, fin=project_service.financials(p), doc_kinds=DOC_KINDS,
                           milestone_statuses=MILESTONE_STATUSES, can_edit=p.status in OPEN_STATUSES)


@bp.route("/projects/<int:project_id>/action", methods=["POST"])
@role_required("partner")
def action(project_id):
    p = _own_project(project_id)
    kind = request.form.get("kind")
    try:
        if p.status not in OPEN_STATUSES:
            raise ProjectError(f"Updates are closed while the project is {p.status}.")
        if kind == "update":
            _save_update(p, request.form)
            flash("Update submitted.", "success")
        elif kind == "milestone":
            _save_milestone(p, request.form)
            flash("Milestone saved.", "success")
        elif kind in ("expense", "revenue"):
            amount = parse_money(request.form.get("amount"))
            when = parse_date(request.form.get("date"))
            description = (request.form.get("description") or "").strip()
            if not when or not description:
                raise ProjectError("Enter the date and a description.")
            receipt = None
            if file_service.has_file(request.files.get("receipt")):
                receipt = save_project_document(p, {"doc_kind": "receipt", "title": f"Receipt: {description[:150]}"},
                                                request.files["receipt"], allowed_public=False)
            data = {"description": description[:255], "amount_cents": amount,
                    "receipt_document_id": receipt.id if receipt else None}
            if kind == "expense":
                data.update(expense_date=when, category=(request.form.get("category") or "")[:80])
                project_service.report_expense(p, data, g.user)
            else:
                data.update(revenue_date=when, source=(request.form.get("source") or "")[:120])
                project_service.report_revenue(p, data, g.user)
            flash("Recorded. Finance staff will verify it before it reaches the ledger.", "success")
        elif kind == "document":
            save_project_document(p, request.form, request.files.get("file"), allowed_public=False)
            flash("Document uploaded.", "success")
        else:
            abort(400)
        db.session.commit()
    except (ProjectError, MoneyError, file_service.UploadError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("partner.project", project_id=p.id))


@bp.route("/<section>")
@role_required("partner")
def section(section):
    if section not in SECTIONS:
        abort(404)
    ids = [p.id for p in _projects()] or [-1]
    if section == "updates":
        items = ProjectUpdate.query.filter(ProjectUpdate.project_id.in_(ids)).order_by(ProjectUpdate.created_at.desc()).all()
    elif section == "milestones":
        items = ProjectMilestone.query.filter(ProjectMilestone.project_id.in_(ids)).order_by(ProjectMilestone.due_date).all()
    elif section == "expenses":
        items = ProjectExpense.query.filter(ProjectExpense.project_id.in_(ids)).order_by(ProjectExpense.expense_date.desc()).all()
    elif section == "revenue":
        items = ProjectRevenue.query.filter(ProjectRevenue.project_id.in_(ids)).order_by(ProjectRevenue.revenue_date.desc()).all()
    elif section == "reports":
        items = Document.query.filter(Document.project_id.in_(ids), Document.kind.in_(["report", "final_report"])) \
            .order_by(Document.uploaded_at.desc()).all()
    else:
        items = Document.query.filter(Document.project_id.in_(ids)).order_by(Document.uploaded_at.desc()).all()
    return render_template("partner/section.html", section=section, title=SECTIONS[section], items=items,
                           doc_kinds=DOC_KINDS)
