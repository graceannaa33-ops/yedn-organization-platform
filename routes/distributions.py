"""Profit distributions: preview, create, approve, pay, fail, reverse."""
import json
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from extensions import db
from helpers import MoneyError, parse_date, parse_money
from models import Distribution, DistributionRun, Project
from permissions import has_permission, permission_required
from services import distribution_service, ledger_service
from services.distribution_service import DistributionError
from services.ledger_service import LedgerError
from services.project_service import financials

bp = Blueprint("distributions", __name__, url_prefix="/admin/distributions")


@bp.route("/")
@permission_required("view_distributions")
def index():
    runs = DistributionRun.query.order_by(DistributionRun.created_at.desc()).all()
    return render_template("finance/distributions.html", runs=runs, problems=distribution_service.rules_problems(),
                           rules=distribution_service.rules_text())


@bp.route("/new", methods=["GET", "POST"])
@permission_required("manage_distributions")
def new():
    problems = distribution_service.rules_problems()
    projects = [(p, financials(p), distribution_service.distributable_for_project(p))
                for p in Project.query.order_by(Project.name).all()]
    preview, form = None, request.form
    if request.method == "POST" and not problems:
        try:
            project = db.session.get(Project, form.get("project_id", type=int) or 0)
            if project is None:
                raise DistributionError("Choose a project.")
            net = parse_money(form.get("amount"))
            record_date = parse_date(form.get("record_date"))
            if not record_date or record_date > date.today():
                raise DistributionError("Choose a record date that is today or earlier.")
            if form.get("confirm") == "yes":
                title = (form.get("title") or "").strip() or f"{project.name} distribution {record_date:%b %Y}"
                run = distribution_service.create_run(project, net, record_date, title, g.user, form.get("notes", ""))
                db.session.commit()
                flash(f"Distribution {run.run_number} calculated. It must now be approved by another officer.", "success")
                return redirect(url_for("distributions.run", run_id=run.id))
            preview = distribution_service.calculate(project, net, record_date)
            preview["project"] = project
        except (DistributionError, MoneyError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("finance/distribution_new.html", problems=problems, projects=projects, preview=preview,
                           form=form, rules=distribution_service.rules_text(), today=date.today())


@bp.route("/<int:run_id>", methods=["GET", "POST"])
@permission_required("view_distributions")
def run(run_id):
    r = db.get_or_404(DistributionRun, run_id)
    if request.method == "POST":
        action = request.form.get("action")
        needed = "approve_distribution" if action == "approve" else "manage_distributions"
        if not has_permission(g.user, needed):
            abort(403)
        try:
            if action == "approve":
                distribution_service.approve_run(r, g.user)
                flash("Distribution approved. Payments can now be made.", "success")
            elif action == "process":
                distribution_service.start_processing(r, g.user)
                flash("Marked as processing.", "success")
            elif action == "cancel":
                distribution_service.cancel_run(r, g.user, request.form.get("reason"))
                flash("Distribution cancelled.", "info")
            elif action in ("pay", "fail", "reverse"):
                d = db.get_or_404(Distribution, request.form.get("dist_id", type=int))
                if d.run_id != r.id:
                    abort(400)
                if action == "pay":
                    cash = request.form.get("cash_account")
                    if cash not in ledger_service.CASH_ACCOUNTS:
                        raise DistributionError("Choose the account the money is paid from.")
                    distribution_service.mark_paid(d, request.form.get("method"), request.form.get("reference"), cash, g.user)
                    flash(f"{d.dist_number} marked as paid.", "success")
                elif action == "fail":
                    distribution_service.mark_failed(d, request.form.get("reason"), g.user)
                    flash(f"{d.dist_number} marked as failed.", "info")
                else:
                    distribution_service.reverse(d, request.form.get("reason"), g.user)
                    flash(f"{d.dist_number} reversed.", "info")
            else:
                abort(400)
            db.session.commit()
        except (DistributionError, LedgerError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("distributions.run", run_id=r.id))
    rules = json.loads(r.rules_snapshot or "{}")
    return render_template("finance/distribution_run.html", r=r, rules=rules, basis=distribution_service.BASIS,
                           methods=distribution_service.METHODS, late=distribution_service.LATE,
                           cash=ledger_service.CASH_ACCOUNTS, accounts=ledger_service.ACCOUNTS)
