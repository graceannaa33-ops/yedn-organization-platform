"""Project management for staff: proposals, review workflow, funding,
milestones, documents, updates and the project financial dashboard."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from extensions import db
from helpers import MoneyError, Page, parse_date, parse_money, utcnow
from models import PROJECT_STATUSES, Document, Project, ProjectMilestone, ProjectPartner, Role, User
from permissions import has_permission, permission_required
from services import audit_service, file_service, ledger_service, project_service
from services.ledger_service import LedgerError
from services.project_service import ProjectError
from services.settings_service import project_categories

bp = Blueprint("projects", __name__, url_prefix="/admin/projects")

DOC_KINDS = {"report": "Progress report", "receipt": "Receipt", "photo": "Photograph", "document": "Document",
             "final_report": "Final report"}
MILESTONE_STATUSES = ["Planned", "In Progress", "Completed", "Delayed"]


def parse_project_form(form):
    errors = []
    data = {"name": (form.get("name") or "").strip()[:150],
            "description": (form.get("description") or "").strip(),
            "category": form.get("category") or "", "location": (form.get("location") or "").strip()[:150],
            "risks": (form.get("risks") or "").strip(),
            "start_date": parse_date(form.get("start_date")),
            "expected_end_date": parse_date(form.get("expected_end_date")),
            "partner_id": form.get("partner_id", type=int) or None,
            "manager_id": form.get("manager_id", type=int) or None,
            "is_public": form.get("is_public") == "on"}
    if len(data["name"]) < 3:
        errors.append("Enter a project name.")
    if len(data["description"]) < 20:
        errors.append("Describe the project (at least 20 characters).")
    if data["category"] not in project_categories():
        errors.append("Choose a category.")
    for field, label in (("requested_cents", "requested funding"), ("budget_cents", "budget")):
        try:
            data[field] = parse_money(form.get(field.replace("_cents", "")), allow_zero=True)
        except MoneyError:
            errors.append(f"Enter a valid {label}.")
    if data["start_date"] and data["expected_end_date"] and data["expected_end_date"] < data["start_date"]:
        errors.append("Expected completion must be after the start date.")
    return data, errors


def _form_context():
    managers = User.query.join(Role).filter(Role.name.in_(["project_manager", "super_admin"]),
                                            User.is_active.is_(True)).order_by(User.full_name).all()
    return {"partners": ProjectPartner.query.filter_by(is_active=True).order_by(ProjectPartner.name).all(),
            "managers": managers, "categories": project_categories()}


@bp.route("/")
@permission_required("view_projects_admin")
def index():
    q = select(Project)
    status = request.args.get("status")
    if status in PROJECT_STATUSES:
        q = q.where(Project.status == status)
    page = Page(q.order_by(Project.created_at.desc()), request.args.get("page"))
    rows = [(p, project_service.financials(p)) for p in page.items]
    return render_template("projects/index.html", page=page, rows=rows, statuses=PROJECT_STATUSES, status=status)


@bp.route("/new", methods=["GET", "POST"])
@permission_required("manage_projects")
def new():
    if request.method == "POST":
        data, errors = parse_project_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("projects/form.html", form=request.form, project=None, **_form_context()), 400
        project = project_service.create_project(data, g.user)
        db.session.commit()
        flash("Project proposal created.", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/form.html", form={}, project=None, **_form_context())


@bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@permission_required("manage_projects")
def edit(project_id):
    project = db.get_or_404(Project, project_id)
    if project.status in ("Completed", "Cancelled"):
        flash("Closed projects cannot be edited.", "error")
        return redirect(url_for("projects.detail", project_id=project.id))
    if request.method == "POST":
        data, errors = parse_project_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("projects/form.html", form=request.form, project=project, **_form_context()), 400
        if project.status not in ("Proposed", "Under Review", "Due Diligence"):
            # After approval the money figures are locked; only descriptive fields change.
            data.pop("requested_cents"), data.pop("budget_cents")
        old = {k: str(getattr(project, k)) for k in data}
        for k, v in data.items():
            setattr(project, k, v)
        audit_service.log("project.updated", "project", project.code, old=old, new={k: str(v) for k, v in data.items()})
        db.session.commit()
        flash("Project updated.", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    form = {"name": project.name, "description": project.description, "category": project.category,
            "location": project.location, "risks": project.risks,
            "start_date": project.start_date.isoformat() if project.start_date else "",
            "expected_end_date": project.expected_end_date.isoformat() if project.expected_end_date else "",
            "partner_id": project.partner_id, "manager_id": project.manager_id,
            "requested": _money_input(project.requested_cents),
            "budget": _money_input(project.budget_cents), "is_public": "on" if project.is_public else ""}
    return render_template("projects/form.html", form=form, project=project, **_form_context())


def _money_input(cents):
    whole, frac = divmod(int(cents or 0), 100)
    return f"{whole}" if not frac else f"{whole}.{frac:02d}"


@bp.route("/<int:project_id>")
@permission_required("view_projects_admin")
def detail(project_id):
    p = db.get_or_404(Project, project_id)
    funding_txns = [t for t in ledger_service_txns(p) if t.type == "project_funding"]
    return render_template("projects/detail.html", p=p, fin=project_service.financials(p),
                           actions=project_service.available_actions(p, g.user), doc_kinds=DOC_KINDS,
                           milestone_statuses=MILESTONE_STATUSES, funding_txns=funding_txns,
                           cash=ledger_service.CASH_ACCOUNTS, accounts=ledger_service.ACCOUNTS)


def ledger_service_txns(project):
    from models import Transaction
    return Transaction.query.filter_by(project_id=project.id).order_by(Transaction.created_at.desc()).all()


@bp.route("/<int:project_id>/action", methods=["POST"])
@permission_required("view_projects_admin")
def action(project_id):
    p = db.get_or_404(Project, project_id)
    kind = request.form.get("kind")
    try:
        if kind == "transition":
            project_service.transition(p, request.form.get("action"), g.user, request.form.get("reason", ""))
            flash(f"Project is now {p.status}.", "success")
        elif kind == "review":
            review_type = request.form.get("review_type")
            perm = {"review": "manage_projects", "financial": "financial_review", "risk": "risk_assessment"}.get(review_type)
            if not perm or not has_permission(g.user, perm):
                abort(403)
            approved = None
            if review_type == "financial" and request.form.get("approved"):
                approved = parse_money(request.form.get("approved"))
            project_service.record_review(p, review_type, request.form.get("notes"), g.user,
                                          request.form.get("risk_level"), approved)
            flash("Review recorded.", "success")
        elif kind == "release":
            if not has_permission(g.user, "release_funding"):
                abort(403)
            cash = request.form.get("cash_account")
            if cash not in ledger_service.CASH_ACCOUNTS:
                raise ProjectError("Choose the account the money is paid from.")
            txn = project_service.release_funding(p, parse_money(request.form.get("amount")), cash, g.user,
                                                  (request.form.get("note") or "").strip())
            if txn.approval_status == "pending":
                flash(f"Funding release {txn.txn_number} created. A second officer must approve it in the ledger.", "success")
            else:
                flash("Funds released and posted to the ledger.", "success")
        elif kind == "milestone":
            if not has_permission(g.user, "manage_projects"):
                abort(403)
            _save_milestone(p, request.form)
            flash("Milestone saved.", "success")
        elif kind == "update":
            if not has_permission(g.user, "manage_projects"):
                abort(403)
            _save_update(p, request.form)
            flash("Update posted to the timeline.", "success")
        elif kind == "document":
            if not has_permission(g.user, "manage_projects"):
                abort(403)
            save_project_document(p, request.form, request.files.get("file"))
            flash("Document uploaded.", "success")
        elif kind == "doc_visibility":
            if not has_permission(g.user, "manage_projects"):
                abort(403)
            doc = db.get_or_404(Document, request.form.get("doc_id", type=int))
            if doc.project_id != p.id:
                abort(400)
            doc.is_public = not doc.is_public
            audit_service.log("project.document_visibility", "document", doc.id, new={"is_public": doc.is_public})
            flash("Document visibility changed.", "success")
        else:
            abort(400)
        db.session.commit()
    except (ProjectError, LedgerError, MoneyError, file_service.UploadError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("projects.detail", project_id=p.id))


# ---------- shared helpers (also used by the partner portal)
def _save_milestone(project, form):
    mid = form.get("milestone_id", type=int)
    status = form.get("status") or "Planned"
    if status not in MILESTONE_STATUSES:
        raise ProjectError("Unknown milestone status.")
    if mid:
        m = db.get_or_404(ProjectMilestone, mid)
        if m.project_id != project.id:
            abort(400)
        old = m.status
        m.status = status
    else:
        title = (form.get("title") or "").strip()
        if not title:
            raise ProjectError("Enter a milestone title.")
        m = ProjectMilestone(project_id=project.id, title=title[:150], description=(form.get("description") or "").strip(),
                             due_date=parse_date(form.get("due_date")), status=status)
        db.session.add(m)
        old = None
    m.updated_by_id, m.updated_at = g.user.id, utcnow()
    if status == "Completed" and not m.completed_at:
        m.completed_at = utcnow()
    db.session.flush()
    if old != status:
        project_service.add_timeline(project, "milestone", f"Milestone '{m.title}': {status}", "", g.user)
        if status == "Delayed":
            project_service.add_timeline(project, "delay", f"Milestone delayed: {m.title}", form.get("note", ""), g.user)
    audit_service.log("project.milestone_saved", "project", project.code, old={"status": old},
                      new={"milestone": m.title, "status": status})


def _save_update(project, form):
    body = (form.get("body") or "").strip()
    kind = form.get("update_kind") if form.get("update_kind") in ("progress", "challenge", "decision", "delay") else "progress"
    progress = form.get("progress_percent", type=int)
    if not body:
        raise ProjectError("Write the update.")
    if progress is not None:
        if not 0 <= progress <= 100:
            raise ProjectError("Progress must be between 0 and 100.")
        project.progress_percent = progress
    titles = {"progress": "Progress update", "challenge": "Challenge reported", "decision": "Decision recorded",
              "delay": "Delay reported"}
    title = titles[kind] + (f" ({progress}%)" if progress is not None else "")
    project_service.add_timeline(project, kind, title, body, g.user, progress, public=form.get("is_public", "on") == "on")
    audit_service.log("project.update_posted", "project", project.code, new={"kind": kind, "progress": progress})


def save_project_document(project, form, file_storage, allowed_public=True):
    kind = form.get("doc_kind")
    if kind not in DOC_KINDS:
        raise ProjectError("Choose the document type.")
    saved = file_service.save_upload(file_storage, "project_documents")
    doc = Document(project_id=project.id, kind=kind, title=(form.get("title") or "").strip()[:200] or saved["original_name"],
                   stored_name=saved["stored_name"], original_name=saved["original_name"], mime_type=saved["mime_type"],
                   size_bytes=saved["size_bytes"], sha256=saved["sha256"],
                   is_public=allowed_public and form.get("is_public") == "on", uploaded_by_id=g.user.id)
    db.session.add(doc)
    db.session.flush()
    if kind == "final_report":
        project_service.add_timeline(project, "final_report", "Final report submitted", doc.title, g.user)
    elif kind == "report":
        project_service.add_timeline(project, "progress", "Progress report uploaded", doc.title, g.user)
    audit_service.log("project.document_uploaded", "project", project.code, new={"kind": kind, "title": doc.title})
    return doc
