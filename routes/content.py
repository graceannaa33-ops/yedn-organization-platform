"""Admin management of YEDN programs and opportunities.

Nothing is invented: programs and opportunities appear publicly only after an
administrator creates and publishes them.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from default_content import PROGRAM_AREAS
from extensions import db
from helpers import parse_date, utcnow
from models import OPPORTUNITY_TYPES, PROGRAM_STATUSES, Opportunity, Program
from permissions import permission_required
from services import audit_service

bp = Blueprint("content", __name__, url_prefix="/admin")


def _text(name, limit=None):
    value = (request.form.get(name) or "").strip()
    return value[:limit] if limit else value


def _safe_link(url):
    return url if url.startswith(("https://", "http://")) else ""


# ---------------------------------------------------------------- programs
@bp.route("/programs", methods=["GET", "POST"])
@permission_required("manage_content")
def programs():
    if request.method == "POST":
        if request.form.get("action") == "add_drafts":
            existing = {p.title for p in Program.query.all()}
            added = 0
            for i, (title, description) in enumerate(PROGRAM_AREAS):
                if title not in existing:
                    db.session.add(Program(title=title, description=description, status="Planned",
                                           is_published=False, sort_order=i, created_by_id=g.user.id))
                    added += 1
            audit_service.log("program.drafts_added", "program", None, new={"count": added})
            db.session.commit()
            flash(f"{added} unpublished draft program(s) added. Complete and publish each one when it is real.", "success")
            return redirect(url_for("content.programs"))
        return _save_program(Program(created_by_id=g.user.id), new=True)
    items = Program.query.order_by(Program.sort_order, Program.title).all()
    return render_template("admin/programs.html", items=items, statuses=PROGRAM_STATUSES, p=None)


@bp.route("/programs/<int:program_id>", methods=["GET", "POST"])
@permission_required("manage_content")
def program(program_id):
    item = db.get_or_404(Program, program_id)
    if request.method == "POST":
        return _save_program(item, new=False)
    return render_template("admin/program_form.html", p=item, statuses=PROGRAM_STATUSES)


def _save_program(item, new):
    title, description = _text("title", 150), _text("description")
    status = request.form.get("status")
    if not title or len(description) < 10 or status not in PROGRAM_STATUSES:
        flash("Enter a title, a description (at least 10 characters) and a status.", "error")
        return redirect(request.path)
    item.title, item.description, item.status = title, description, status
    item.objectives, item.eligibility, item.application_info = _text("objectives"), _text("eligibility"), _text("application_info")
    item.is_published = request.form.get("is_published") == "on"
    item.sort_order = request.form.get("sort_order", type=int) or 0
    item.updated_at = utcnow()
    if new:
        db.session.add(item)
    db.session.flush()
    audit_service.log("program.created" if new else "program.updated", "program", item.id,
                      new={"title": title, "status": status, "published": item.is_published})
    db.session.commit()
    flash("Program saved." + ("" if item.is_published else " It is not published yet."), "success")
    return redirect(url_for("content.program", program_id=item.id))


# ---------------------------------------------------------------- opportunities
@bp.route("/opportunities", methods=["GET", "POST"])
@permission_required("manage_content")
def opportunities():
    if request.method == "POST":
        return _save_opportunity(Opportunity(created_by_id=g.user.id), new=True)
    items = Opportunity.query.order_by(Opportunity.created_at.desc()).all()
    return render_template("admin/opportunities.html", items=items, types=OPPORTUNITY_TYPES)


@bp.route("/opportunities/<int:opp_id>", methods=["GET", "POST"])
@permission_required("manage_content")
def opportunity(opp_id):
    item = db.get_or_404(Opportunity, opp_id)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            # Opportunities are content, not financial records, so they may be removed.
            audit_service.log("opportunity.deleted", "opportunity", item.id, old={"title": item.title})
            db.session.delete(item)
            db.session.commit()
            flash("Opportunity removed.", "info")
            return redirect(url_for("content.opportunities"))
        return _save_opportunity(item, new=False)
    return render_template("admin/opportunity_form.html", o=item, types=OPPORTUNITY_TYPES)


def _save_opportunity(item, new):
    title, description, kind = _text("title", 200), _text("description"), request.form.get("opportunity_type")
    if not title or len(description) < 10 or kind not in OPPORTUNITY_TYPES:
        flash("Enter a title, type and a description (at least 10 characters).", "error")
        return redirect(request.path)
    raw_deadline = request.form.get("deadline")
    if raw_deadline and not parse_date(raw_deadline):
        flash("The deadline must be a valid date.", "error")
        return redirect(request.path)
    item.title, item.description, item.opportunity_type = title, description, kind
    item.provider, item.eligibility, item.how_to_apply = _text("provider", 150), _text("eligibility"), _text("how_to_apply")
    item.link = _safe_link(_text("link", 500))
    item.deadline = parse_date(raw_deadline)
    item.members_only = request.form.get("members_only") == "on"
    item.is_published = request.form.get("is_published") == "on"
    if new:
        db.session.add(item)
    db.session.flush()
    audit_service.log("opportunity.created" if new else "opportunity.updated", "opportunity", item.id,
                      new={"title": title, "published": item.is_published})
    db.session.commit()
    flash("Opportunity saved.", "success")
    return redirect(url_for("content.opportunity", opp_id=item.id))
