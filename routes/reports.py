"""Staff reports: view, export CSV/PDF, publish to the public Reports page."""
from datetime import date

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from extensions import db
from models import Report
from permissions import has_permission, permission_required
from services import audit_service, report_service
from services.report_service import ReportError

bp = Blueprint("reports", __name__, url_prefix="/admin/reports")


def _allowed(report_type):
    info = report_service.REPORT_TYPES.get(report_type)
    if info is None:
        abort(404)
    if info.get("permission") and not has_permission(g.user, info["permission"]):
        abort(403)
    return info


@bp.route("/")
@permission_required("view_reports")
def index():
    published = Report.query.order_by(Report.published_at.desc()).all()
    types = {k: v for k, v in report_service.REPORT_TYPES.items()
             if not v.get("permission") or has_permission(g.user, v["permission"])}
    return render_template("finance/reports.html", types=types, published=published, now_year=date.today().year)


@bp.route("/<report_type>")
@bp.route("/<report_type>.<fmt>")
@permission_required("view_reports")
def view(report_type, fmt=None):
    _allowed(report_type)
    try:
        start, end = report_service.resolve_period(report_type, request.args)
    except ReportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("reports.index"))
    report = report_service.build(report_type, start, end)
    name = f"{report_type}-{start}-{end}"
    if fmt == "csv":
        return Response(report_service.to_csv(report), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    if fmt == "pdf":
        return Response(report_service.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    if fmt:
        abort(404)
    return render_template("finance/report_view.html", report=report, report_type=report_type,
                           info=report_service.REPORT_TYPES[report_type], args=request.args)


@bp.route("/<report_type>/publish", methods=["POST"])
@permission_required("publish_reports")
def publish(report_type):
    info = _allowed(report_type)
    if not info["public"]:
        flash("This report contains private information and cannot be published.", "error")
        return redirect(url_for("reports.index"))
    try:
        start, end = report_service.resolve_period(report_type, request.form)
    except ReportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("reports.index"))
    item = Report(report_type=report_type, title=f"{info['label']}: {start:%d %b %Y} - {end:%d %b %Y}",
                  period_start=start, period_end=end, published_by_id=g.user.id)
    db.session.add(item)
    db.session.flush()
    audit_service.log("report.published", "report", item.id, new={"type": report_type, "start": start, "end": end})
    db.session.commit()
    flash("Report published on the public Reports page.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/published/<int:report_id>/unpublish", methods=["POST"])
@permission_required("publish_reports")
def unpublish(report_id):
    item = db.get_or_404(Report, report_id)
    item.is_published = not item.is_published
    audit_service.log("report.visibility", "report", item.id, new={"is_published": item.is_published})
    db.session.commit()
    flash("Report visibility changed.", "success")
    return redirect(url_for("reports.index"))
