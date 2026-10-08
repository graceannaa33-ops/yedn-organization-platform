"""Public Transparency Centre and published reports.

Only totals and anonymised activity are shown. Never: ID numbers, phone
numbers, emails, ID documents, names of individual contributors, or private
payment details.
"""
from flask import Blueprint, Response, abort, render_template

from extensions import db
from models import Project, Reconciliation, Report, Transaction
from services import contribution_service, ledger_service, report_service
from services.project_service import financials
from services.settings_service import get_setting
from routes.public import public_projects

bp = Blueprint("transparency", __name__)

PUBLIC_ACTIVITY_TYPES = {
    "member_contribution": "A member contribution was received",
    "project_funding": "Funds were released to a project",
    "project_revenue": "Project revenue was recorded",
    "project_expense": "A project expense was verified",
    "distribution": "A distribution was paid to a member",
    "operating_expense": "An operating expense was paid",
    "refund": "A refund was paid",
}


@bp.route("/transparency")
def index():
    counts = contribution_service.organisation_counts()
    summary = ledger_service.summary()
    projects = [(p, financials(p)) for p in public_projects()]
    active = sum(1 for p, _ in projects if p.status in ("Active", "Delayed"))
    completed = sum(1 for p, _ in projects if p.status == "Completed")
    recent = Transaction.query.filter(Transaction.approval_status == "approved",
                                      Transaction.type.in_(PUBLIC_ACTIVITY_TYPES)) \
        .order_by(Transaction.posted_at.desc()).limit(12).all()
    activity = []
    for t in recent:
        text = PUBLIC_ACTIVITY_TYPES[t.type]
        if t.project and t.project.is_public and t.type != "distribution":
            text += f" ({t.project.name})"
        if t.is_reversal:
            text = "Correction: " + text.lower() + " was reversed"
        activity.append({"when": t.posted_at, "text": text, "amount": t.amount_cents})
    recs = {}
    for acc in ledger_service.CASH_ACCOUNTS:
        recs[acc] = Reconciliation.query.filter_by(account=acc).order_by(Reconciliation.reconciled_at.desc()).first()
    return render_template("public/transparency.html", counts=counts, s=summary, projects=projects,
                           active=active, completed=completed, activity=activity, recs=recs,
                           last_reconciled=get_setting("last_reconciled_at"))


@bp.route("/reports")
def reports():
    items = Report.query.filter_by(is_published=True).order_by(Report.published_at.desc()).all()
    return render_template("public/reports.html", items=items, types=report_service.REPORT_TYPES)


@bp.route("/reports/<int:report_id>")
@bp.route("/reports/<int:report_id>.<fmt>")
def report(report_id, fmt=None):
    item = db.get_or_404(Report, report_id)
    if not item.is_published or not report_service.REPORT_TYPES.get(item.report_type, {}).get("public"):
        abort(404)
    data = report_service.build(item.report_type, item.period_start, item.period_end, public=True)
    data["title"] = item.title
    name = f"{item.report_type}-{item.period_start}-{item.period_end}"
    if fmt == "csv":
        return Response(report_service.to_csv(data), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    if fmt == "pdf":
        return Response(report_service.to_pdf(data), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    if fmt:
        abort(404)
    return render_template("public/report_view.html", report=data, item=item)
