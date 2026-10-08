"""Public pages. Nothing here shows private member information."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from extensions import db
from helpers import valid_email
from models import (OPPORTUNITY_TYPES, Announcement, Complaint, Document, Member, Opportunity, Program, Project,
                    ProjectPartner)
from services import contribution_service, file_service, governance_service, ledger_service
from services.distribution_service import rules_problems, rules_text
from services.project_service import financials
from services.settings_service import get_setting, org_text

bp = Blueprint("public", __name__)

PUBLIC_PROJECT_STATUSES = ["Approved", "Funding Pending", "Active", "Delayed", "Completed", "Suspended", "Cancelled"]
COMPLAINT_CATEGORIES = ["Payments", "Distributions", "Staff conduct", "Projects", "Privacy", "General enquiry", "Other"]
LEGAL_PAGES = {
    "terms": ("Terms and Conditions", "legal_terms"),
    "privacy": ("Privacy Policy", "legal_privacy"),
    "risk-disclosure": ("Risk Disclosure", "legal_risk"),
    "refund-policy": ("Refund Policy", "legal_refund"),
    "complaints-policy": ("Complaints Policy", "legal_complaints"),
    "contribution-rules": ("Contribution Rules", "legal_contribution_rules"),
}


def public_projects():
    return Project.query.filter(Project.is_public.is_(True), Project.status.in_(PUBLIC_PROJECT_STATUSES)) \
        .order_by(Project.created_at.desc()).all()


def public_stats():
    """Homepage statistics - every number is counted from the database, never invented."""
    from sqlalchemy import func, select
    by_status = dict(db.session.execute(select(Project.status, func.count(Project.id)).group_by(Project.status)).all())
    return {
        "members": Member.query.filter(Member.status != "exited").count(),
        "active_projects": by_status.get("Active", 0),
        "completed_projects": by_status.get("Completed", 0),
        "under_implementation": sum(by_status.get(s, 0) for s in ("Funding Pending", "Active", "Delayed")),
        "contributions": ledger_service.account_balance("MEMBER_CONTRIBUTIONS"),
    }


@bp.route("/")
def home():
    projects = [(p, financials(p)) for p in public_projects()[:3]]
    news = Announcement.query.filter_by(is_public=True).order_by(Announcement.created_at.desc()).limit(3).all()
    programs = Program.query.filter_by(is_published=True).order_by(Program.sort_order, Program.title).limit(3).all()
    return render_template("public/home.html", stats=public_stats(), projects=projects, news=news, programs=programs)


@bp.route("/about")
def about():
    return render_template("public/about.html", description=org_text("organization_description"),
                           vision=org_text("vision_text"), mission=org_text("mission_text"),
                           founding=org_text("founding_text"), founding_team=org_text("founding_team_text"),
                           university=org_text("university_relationship"))


@bp.route("/what-we-do")
def what_we_do():
    return render_template("public/what_we_do.html")


@bp.route("/how-it-works")
def how_it_works():
    return render_template("public/how_it_works.html")


@bp.route("/membership")
def membership():
    return render_template("public/membership.html", risk=org_text("legal_risk"))


@bp.route("/member-rights")
def member_rights():
    return render_template("public/member_rights.html")


@bp.route("/governance")
def governance():
    return render_template("public/governance.html", leadership=org_text("leadership_text"))


@bp.route("/organization-structure")
def structure():
    return render_template("public/structure.html", leadership=org_text("leadership_text"))


@bp.route("/project-funding")
def project_funding():
    return render_template("public/project_funding.html")


@bp.route("/programs")
def programs():
    items = Program.query.filter_by(is_published=True).order_by(Program.sort_order, Program.title).all()
    return render_template("public/programs.html", items=items)


@bp.route("/programs/<int:program_id>")
def program(program_id):
    item = db.get_or_404(Program, program_id)
    if not item.is_published:
        abort(404)
    return render_template("public/program.html", p=item)


@bp.route("/opportunities")
def opportunities():
    from datetime import date
    q = Opportunity.query.filter(Opportunity.is_published.is_(True))
    if not g.user:
        q = q.filter(Opportunity.members_only.is_(False))
    kind = request.args.get("type")
    if kind in OPPORTUNITY_TYPES:
        q = q.filter(Opportunity.opportunity_type == kind)
    items = q.order_by(Opportunity.deadline.is_(None), Opportunity.deadline, Opportunity.created_at.desc()).all()
    today = date.today()
    current = [o for o in items if not o.deadline or o.deadline >= today]
    past = [o for o in items if o.deadline and o.deadline < today]
    return render_template("public/opportunities.html", current=current, past=past, types=OPPORTUNITY_TYPES,
                           kind=kind)


@bp.route("/projects")
def projects():
    status = request.args.get("status")
    items = public_projects()
    if status in PUBLIC_PROJECT_STATUSES:
        items = [p for p in items if p.status == status]
    return render_template("public/projects.html", items=[(p, financials(p)) for p in items],
                           statuses=PUBLIC_PROJECT_STATUSES, status=status)


@bp.route("/projects/<int:project_id>")
def project(project_id):
    p = db.get_or_404(Project, project_id)
    if not p.is_public or p.status not in PUBLIC_PROJECT_STATUSES:
        abort(404)
    photos = [d for d in p.documents if d.is_public and d.kind == "photo"]
    docs = [d for d in p.documents if d.is_public and d.kind != "photo"]
    timeline = [u for u in p.updates if u.is_public]
    return render_template("public/project.html", p=p, fin=financials(p), photos=photos, docs=docs,
                           events=timeline)


@bp.route("/projects/<int:project_id>/files/<int:doc_id>")
def project_file(project_id, doc_id):
    doc = db.get_or_404(Document, doc_id)
    if doc.project_id != project_id or not doc.is_public or not doc.project.is_public:
        abort(404)
    return file_service.send_private("project_documents", doc.stored_name, doc.mime_type,
                                     download_name=doc.original_name, inline=doc.kind == "photo")


@bp.route("/partners")
def partners():
    items = ProjectPartner.query.filter_by(is_public=True, is_active=True).order_by(ProjectPartner.name).all()
    return render_template("public/partners.html", items=items)


@bp.route("/faq")
def faq():
    return render_template("public/faq.html")


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        return _save_complaint(default_category="General enquiry", template="public/contact.html")
    return render_template("public/contact.html", form={}, categories=COMPLAINT_CATEGORIES)


@bp.route("/legal/<slug>")
def legal(slug):
    if slug == "distribution-rules":
        return render_template("public/distribution_rules.html", lines=rules_text(),
                               problems=rules_problems(), notes=org_text("dist_rules_notes"))
    if slug not in LEGAL_PAGES:
        abort(404)
    title, key = LEGAL_PAGES[slug]
    return render_template("public/legal.html", title=title, text=org_text(key))


@bp.route("/complaints", methods=["GET", "POST"])
def complaints():
    if request.method == "POST":
        return _save_complaint(template="public/complaints.html")
    form = {}
    if g.user:
        form = {"name": g.user.full_name, "email": g.user.email}
    return render_template("public/complaints.html", form=form, categories=COMPLAINT_CATEGORIES,
                           policy=org_text("legal_complaints"))


def _save_complaint(template, default_category=None):
    form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "phone", "subject", "category", "description")}
    if default_category and not form["category"]:
        form["category"] = default_category
    errors = []
    if len(form["name"]) < 2:
        errors.append("Enter your name.")
    if not valid_email(form["email"]):
        errors.append("Enter a valid email so we can reply.")
    if len(form["subject"]) < 3:
        errors.append("Enter a subject.")
    if form["category"] not in COMPLAINT_CATEGORIES:
        errors.append("Choose a category.")
    if len(form["description"]) < 10:
        errors.append("Describe the issue (at least 10 characters).")
    if request.form.get("website"):  # honeypot field: hidden from people, filled by bots
        errors.append("Submission rejected.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(template, form=form, categories=COMPLAINT_CATEGORIES,
                               policy=org_text("legal_complaints")), 400
    c = governance_service.open_complaint(form["name"], form["email"].lower(), form["phone"], form["subject"],
                                          form["category"], form["description"][:5000], g.user)
    db.session.commit()
    return render_template("public/complaint_received.html", complaint=c)


@bp.route("/complaints/status", methods=["GET", "POST"])
def complaint_status():
    complaint = None
    if request.method == "POST":
        number = (request.form.get("reference") or "").strip().upper()
        email = (request.form.get("email") or "").strip().lower()
        complaint = Complaint.query.filter_by(complaint_number=number).first()
        if complaint is None or complaint.email.lower() != email:
            complaint = None
            flash("No complaint matches that reference and email.", "error")
    return render_template("public/complaint_status.html", complaint=complaint)


@bp.route("/logo")
def logo():
    name = get_setting("logo")
    if not name:
        abort(404)
    ext = name.rsplit(".", 1)[-1]
    resp = file_service.send_private("logos", name, "image/png" if ext == "png" else "image/jpeg", inline=True)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
