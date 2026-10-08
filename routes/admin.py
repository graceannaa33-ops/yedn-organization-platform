"""Administration: dashboard, members, partners, staff, audit log, alerts, settings."""
from collections import OrderedDict
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select

from extensions import db
from helpers import MoneyError, Page, format_money, normalize_phone, parse_date, parse_money, password_problems, \
    utcnow, valid_email
from models import (AnomalyFlag, AuditLog, Complaint, Contribution, DistributionRun, Member, Payment, Project,
                    ProjectExpense, ProjectPartner, ProjectRevenue, Role, SupportTicket, Transaction, User)
from permissions import PERMISSIONS, ROLE_LABELS, STAFF_ROLES, has_permission, permission_required
from services import (anomaly_service, audit_service, contribution_service, distribution_service, file_service,
                      ledger_service, notification_service)
from services.settings_service import DEFAULTS, get_setting, set_setting

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
@permission_required("view_admin")
def dashboard():
    counts = contribution_service.organisation_counts()
    s = ledger_service.summary()
    projects = {status: n for status, n in db.session.execute(
        select(Project.status, func.count(Project.id)).group_by(Project.status)).all()}
    pending = {
        "payments": Payment.query.filter_by(status="Pending").count(),
        "transactions": Transaction.query.filter_by(approval_status="pending").count(),
        "projects": Project.query.filter(Project.status.in_(["Under Review", "Due Diligence"])).count(),
        "distributions": DistributionRun.query.filter_by(status="Pending").count(),
        "reports": ProjectExpense.query.filter_by(status="Reported").count()
                   + ProjectRevenue.query.filter_by(status="Reported").count(),
        "complaints": Complaint.query.filter(Complaint.status.not_in(["Resolved", "Closed"])).count(),
        "tickets": SupportTicket.query.filter(SupportTicket.status.not_in(["Resolved", "Closed"])).count(),
        "flags": AnomalyFlag.query.filter_by(status="Open").count(),
    }
    recent = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html", counts=counts, s=s, projects=projects, pending=pending,
                           recent=recent, monthly=_monthly_contributions())


def _monthly_contributions(months=6):
    today = date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    totals = OrderedDict((k, 0) for k in keys)
    first = date(keys[0][0], keys[0][1], 1)
    for p in Payment.query.filter(Payment.status == "Successful",
                                  Payment.verified_at >= contribution_service.start_of_day(first)).all():
        k = (p.verified_at.year, p.verified_at.month)
        if k in totals:
            totals[k] += p.amount_cents
    peak = max(totals.values()) or 1
    return [{"label": date(k[0], k[1], 1).strftime("%b %Y"), "amount": v, "pct": round(v * 100 / peak)}
            for k, v in totals.items()]


# ---------------------------------------------------------------- members
@bp.route("/members")
@permission_required("view_members")
def members():
    q = select(Member).join(User, Member.user_id == User.id)
    search = (request.args.get("q") or "").strip()
    status = request.args.get("status")
    if search:
        like = f"%{search}%"
        q = q.where(or_(User.full_name.ilike(like), Member.member_number.ilike(like), User.email.ilike(like),
                        Member.phone.ilike(like), Member.id_number.ilike(like)))
    if status in ("pending", "active", "suspended", "exited"):
        q = q.where(Member.status == status)
    page = Page(q.order_by(Member.id.desc()), request.args.get("page"))
    rows = [(m, contribution_service.member_summary(m)) for m in page.items]
    return render_template("admin/members.html", page=page, rows=rows, search=search, status=status)


@bp.route("/members/<int:member_id>", methods=["GET", "POST"])
@permission_required("view_members")
def member(member_id):
    m = db.get_or_404(Member, member_id)
    if request.method == "POST":
        if not has_permission(g.user, "manage_members"):
            abort(403)
        action = request.form.get("action")
        reason = (request.form.get("reason") or "").strip()
        old = m.status
        if action == "admit" and m.status == "pending":
            m.status, m.admitted_at, m.admitted_by_id = "active", utcnow(), g.user.id
            notification_service.notify(m.user, "Membership approved",
                                        f"Welcome! Your membership {m.member_number} is now active.", "account",
                                        sms=True, email=True)
        elif action == "suspend" and m.status == "active" and reason:
            m.status = "suspended"
        elif action == "reactivate" and m.status == "suspended":
            m.status = "active"
        elif action == "exit" and m.status in ("active", "suspended") and reason:
            m.status = "exited"
        elif action == "reset_password":
            new = request.form.get("new_password") or ""
            problems = password_problems(new)
            if problems:
                flash(" ".join(problems), "error")
                return redirect(url_for("admin.member", member_id=m.id))
            m.user.set_password(new)
            audit_service.log("member.password_reset", "member", m.member_number, reason=reason or "Admin reset")
            db.session.commit()
            flash("Password reset. Give the new password to the member privately.", "success")
            return redirect(url_for("admin.member", member_id=m.id))
        else:
            flash("That action is not available (a reason is required to suspend or exit).", "error")
            return redirect(url_for("admin.member", member_id=m.id))
        audit_service.log("member.status_changed", "member", m.member_number, old={"status": old},
                          new={"status": m.status}, reason=reason)
        db.session.commit()
        flash(f"Member status is now {m.status}.", "success")
        return redirect(url_for("admin.member", member_id=m.id))
    summary = contribution_service.member_summary(m)
    history = contribution_service.payment_history(m)
    from models import Distribution
    dists = Distribution.query.filter_by(member_id=m.id).order_by(Distribution.created_at.desc()).all()
    return render_template("admin/member_detail.html", m=m, summary=summary, history=history, dists=dists)


@bp.route("/members/<int:member_id>/id-document/<int:doc_id>")
@permission_required("view_id_documents")
def member_id_document(member_id, doc_id):
    m = db.get_or_404(Member, member_id)
    doc = next((d for d in m.id_documents if d.id == doc_id), None)
    if doc is None:
        abort(404)
    audit_service.log("member.id_document_viewed", "member", m.member_number, new={"document": doc.id})
    anomaly_service.check_staff_activity(g.user)
    db.session.commit()
    return file_service.send_private("id_documents", doc.stored_name, doc.mime_type,
                                     download_name=f"{m.member_number}-id.{doc.stored_name.rsplit('.', 1)[-1]}",
                                     inline=doc.mime_type != "application/pdf")


@bp.route("/members/<int:member_id>/photo")
@permission_required("view_members")
def member_photo(member_id):
    m = db.get_or_404(Member, member_id)
    if not m.profile_photo:
        abort(404)
    ext = m.profile_photo.rsplit(".", 1)[-1]
    return file_service.send_private("profile_photos", m.profile_photo,
                                     "image/png" if ext == "png" else "image/jpeg", inline=True)


@bp.route("/members/contribution-period", methods=["POST"])
@permission_required("manage_settings")
def apply_contribution_period():
    created = 0
    label = contribution_service.current_period_label()
    for m in Member.query.filter(Member.status.in_(["active", "pending"])).all():
        if not Contribution.query.filter_by(member_id=m.id, period_label=label).first():
            contribution_service.ensure_current_contribution(m, g.user)
            created += 1
    audit_service.log("contribution.period_applied", "contribution", label, new={"members": created})
    db.session.commit()
    flash(f"'{label}' applied: {created} member obligation(s) created.", "success")
    return redirect(url_for("admin.settings"))


# ---------------------------------------------------------------- partners
@bp.route("/partners", methods=["GET", "POST"])
@permission_required("manage_partners")
def partners():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if len(name) < 2:
            flash("Enter the partner name.", "error")
        else:
            p = ProjectPartner(name=name, description=request.form.get("description", "").strip(),
                               contact_person=request.form.get("contact_person", "").strip(),
                               contact_email=request.form.get("contact_email", "").strip().lower(),
                               contact_phone=request.form.get("contact_phone", "").strip(),
                               website=w if (w := request.form.get("website", "").strip()).startswith(("http://", "https://")) else "",
                               is_public=request.form.get("is_public") == "on")
            db.session.add(p)
            db.session.flush()
            audit_service.log("partner.created", "partner", p.id, new={"name": name})
            db.session.commit()
            flash("Partner added.", "success")
        return redirect(url_for("admin.partners"))
    items = ProjectPartner.query.order_by(ProjectPartner.name).all()
    return render_template("admin/partners.html", items=items)


@bp.route("/partners/<int:partner_id>", methods=["GET", "POST"])
@permission_required("manage_partners")
def partner(partner_id):
    p = db.get_or_404(ProjectPartner, partner_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update":
            for field in ("name", "description", "contact_person", "contact_email", "contact_phone", "website"):
                setattr(p, field, (request.form.get(field) or "").strip())
            if p.website and not p.website.startswith(("http://", "https://")):
                p.website = ""
            p.is_public = request.form.get("is_public") == "on"
            p.is_active = request.form.get("is_active") == "on"
            audit_service.log("partner.updated", "partner", p.id)
            flash("Partner updated.", "success")
        elif action == "add_user":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            full_name = (request.form.get("full_name") or "").strip()
            problems = password_problems(password)
            if not valid_email(email) or not full_name:
                flash("Enter a name and valid email.", "error")
                return redirect(url_for("admin.partner", partner_id=p.id))
            if User.query.filter(func.lower(User.email) == email).first():
                flash("A user with that email already exists.", "error")
                return redirect(url_for("admin.partner", partner_id=p.id))
            if problems:
                flash(" ".join(problems), "error")
                return redirect(url_for("admin.partner", partner_id=p.id))
            u = User(email=email, full_name=full_name, role=Role.query.filter_by(name="partner").one(), partner_id=p.id)
            u.set_password(password)
            db.session.add(u)
            db.session.flush()
            audit_service.log("partner.user_created", "user", u.id, new={"email": email, "partner": p.name})
            flash("Partner login created. Share the password privately.", "success")
        elif action == "toggle_user":
            u = db.get_or_404(User, request.form.get("user_id", type=int))
            if u.partner_id != p.id:
                abort(400)
            u.is_active = not u.is_active
            audit_service.log("partner.user_toggled", "user", u.id, new={"is_active": u.is_active})
            flash("Partner login updated.", "success")
        db.session.commit()
        return redirect(url_for("admin.partner", partner_id=p.id))
    return render_template("admin/partner_detail.html", p=p)


# ---------------------------------------------------------------- staff
@bp.route("/staff", methods=["GET", "POST"])
@permission_required("manage_staff")
def staff():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            email = (request.form.get("email") or "").strip().lower()
            name = (request.form.get("full_name") or "").strip()
            role_name = request.form.get("role")
            password = request.form.get("password") or ""
            problems = password_problems(password)
            if role_name not in STAFF_ROLES or not valid_email(email) or not name:
                flash("Enter a name, valid email and staff role.", "error")
            elif User.query.filter(func.lower(User.email) == email).first():
                flash("A user with that email already exists.", "error")
            elif problems:
                flash(" ".join(problems), "error")
            else:
                u = User(email=email, full_name=name, role=Role.query.filter_by(name=role_name).one())
                u.set_password(password)
                db.session.add(u)
                db.session.flush()
                audit_service.log("staff.created", "user", u.id, new={"email": email, "role": role_name})
                db.session.commit()
                flash("Staff account created.", "success")
        elif action in ("role", "toggle"):
            u = db.get_or_404(User, request.form.get("user_id", type=int))
            if not u.is_staff or u.id == g.user.id:
                flash("You cannot change your own account here.", "error")
                return redirect(url_for("admin.staff"))
            if action == "role":
                role_name = request.form.get("role")
                if role_name not in STAFF_ROLES:
                    abort(400)
                old = u.role_name
                u.role = Role.query.filter_by(name=role_name).one()
                audit_service.log("staff.role_changed", "user", u.id, old={"role": old}, new={"role": role_name},
                                  reason=request.form.get("reason"))
            else:
                u.is_active = not u.is_active
                audit_service.log("staff.toggled", "user", u.id, new={"is_active": u.is_active})
            db.session.commit()
            flash("Staff account updated.", "success")
        return redirect(url_for("admin.staff"))
    users = User.query.join(Role).filter(Role.name.in_(STAFF_ROLES)).order_by(User.full_name).all()
    matrix = [(perm, [r for r in ("super_admin", "finance_officer", "project_manager", "auditor") if r in roles])
              for perm, roles in PERMISSIONS.items()]
    return render_template("admin/staff.html", users=users, roles=[r for r in ROLE_LABELS if r in STAFF_ROLES],
                           matrix=matrix)


# ---------------------------------------------------------------- audit & alerts
@bp.route("/audit")
@permission_required("view_audit")
def audit():
    q = select(AuditLog)
    action = (request.args.get("action") or "").strip()
    user = (request.args.get("user") or "").strip()
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if user:
        q = q.where(AuditLog.user_email.ilike(f"%{user}%"))
    start, end = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    if start:
        q = q.where(AuditLog.created_at >= contribution_service.start_of_day(start))
    if end:
        q = q.where(AuditLog.created_at <= contribution_service.end_of_day(end))
    page = Page(q.order_by(AuditLog.created_at.desc()), request.args.get("page"), 50)
    return render_template("admin/audit.html", page=page, args=request.args)


@bp.route("/alerts", methods=["GET", "POST"])
@permission_required("view_flags")
def alerts():
    if request.method == "POST":
        if not has_permission(g.user, "review_flags"):
            abort(403)
        f = db.get_or_404(AnomalyFlag, request.form.get("flag_id", type=int))
        decision = request.form.get("decision")
        note = (request.form.get("note") or "").strip()
        if decision not in ("Reviewed", "Dismissed") or not note:
            flash("Choose an outcome and write a short review note.", "error")
        else:
            f.status, f.review_note, f.reviewed_by_id, f.reviewed_at = decision, note, g.user.id, utcnow()
            audit_service.log("alert.reviewed", "anomaly_flag", f.id, new={"status": decision}, reason=note)
            db.session.commit()
            flash("Alert updated.", "success")
        return redirect(url_for("admin.alerts", status=request.args.get("status", "Open")))
    status = request.args.get("status", "Open")
    q = select(AnomalyFlag)
    if status in ("Open", "Reviewed", "Dismissed"):
        q = q.where(AnomalyFlag.status == status)
    page = Page(q.order_by(AnomalyFlag.created_at.desc()), request.args.get("page"))
    return render_template("admin/alerts.html", page=page, status=status)


# ---------------------------------------------------------------- settings
TEXT_SETTINGS = ["organization_name", "organization_tagline", "contact_email", "contact_phone", "contact_address",
                 "currency", "contribution_period_label", "maintenance_message", "project_categories",
                 "tpl_payment_received", "tpl_payment_failed", "tpl_distribution_paid", "tpl_payment_reversed",
                 "dist_rules_notes", "about_text", "legal_terms", "legal_privacy", "legal_risk", "legal_refund",
                 "legal_complaints", "legal_contribution_rules"]
IDENTITY_SETTINGS = ["organization_name", "organization_short_name", "organization_tagline",
                     "organization_supporting_message", "organization_website", "organization_registration",
                     "university_relationship", "organization_description", "vision_text", "mission_text",
                     "founding_text", "leadership_text", "founding_team_text"]
BOOL_SETTINGS = ["require_membership_approval", "require_two_person_approval", "require_admin_2fa", "sms_enabled",
                 "email_enabled", "maintenance_mode"]


@bp.route("/settings", methods=["GET", "POST"])
@permission_required("manage_settings")
def settings():
    if request.method == "POST":
        section = request.form.get("section")
        changes, errors = {}, []
        if section == "general":
            fields = ["contact_email", "contact_phone", "contact_address", "currency", "maintenance_message",
                      "project_categories"]
            for f in fields:
                changes[f] = (request.form.get(f) or "").strip()
            for f in ("require_membership_approval", "require_two_person_approval", "require_admin_2fa",
                      "sms_enabled", "email_enabled", "maintenance_mode"):
                changes[f] = "1" if request.form.get(f) == "on" else "0"
            provider = request.form.get("payment_provider", "")
            if provider not in ("", "sandbox", "mpesa"):
                errors.append("Unknown payment provider.")
            changes["payment_provider"] = provider
            logo = request.files.get("logo")
            if file_service.has_file(logo):
                try:
                    changes["logo"] = file_service.save_upload(logo, "logos", file_service.IMAGE_TYPES)["stored_name"]
                except file_service.UploadError as exc:
                    errors.append(str(exc))
        elif section == "identity":
            for f in IDENTITY_SETTINGS:
                changes[f] = (request.form.get(f) or "").strip()
            if not changes["organization_name"] or not changes["organization_short_name"]:
                errors.append("The organization name and short name are required.")
            if changes["organization_website"] and not changes["organization_website"].startswith(("http://", "https://")):
                errors.append("The website must start with https:// or http://")
            for f in ("organization_description", "vision_text", "mission_text", "founding_text"):
                changes[f] = changes[f] or DEFAULTS[f]
        elif section == "contributions":
            try:
                changes["contribution_amount_cents"] = str(parse_money(request.form.get("contribution_amount")))
                changes["min_payment_cents"] = str(parse_money(request.form.get("min_payment")))
            except MoneyError as exc:
                errors.append(str(exc))
            period = request.form.get("contribution_period")
            if period not in ("one-off", "annual", "quarterly", "monthly"):
                errors.append("Choose a contribution period.")
            changes["contribution_period"] = period
            changes["contribution_period_label"] = (request.form.get("contribution_period_label") or "").strip() or "Membership contribution"
            due = request.form.get("contribution_due_date", "").strip()
            if due and not parse_date(due):
                errors.append("Deadline must be a valid date.")
            changes["contribution_due_date"] = due
        elif section == "distribution":
            basis = request.form.get("dist_eligibility_basis", "")
            method = request.form.get("dist_allocation_method", "")
            late = request.form.get("dist_late_policy", "")
            if basis not in distribution_service.BASIS or method not in distribution_service.METHODS \
                    or late not in distribution_service.LATE:
                errors.append("Choose all three distribution rules.")
            changes.update({"dist_eligibility_basis": basis, "dist_allocation_method": method,
                            "dist_late_policy": late,
                            "dist_rules_notes": (request.form.get("dist_rules_notes") or "").strip()})
            if late in ("future_only", "none") and not parse_date(get_setting("contribution_due_date")):
                errors.append("Set the contribution deadline (Contributions section) before choosing this "
                              "late-completion policy.")
        elif section == "templates":
            for f in ("tpl_payment_received", "tpl_payment_failed", "tpl_distribution_paid", "tpl_payment_reversed"):
                changes[f] = (request.form.get(f) or "").strip() or DEFAULTS[f]
        elif section == "legal":
            for f in ("about_text", "legal_terms", "legal_privacy", "legal_risk", "legal_refund",
                      "legal_complaints", "legal_contribution_rules"):
                changes[f] = (request.form.get(f) or "").strip() or DEFAULTS[f]
        else:
            abort(400)
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("admin.settings") + f"#{section}")
        old = {k: get_setting(k) for k in changes}
        for k, v in changes.items():
            set_setting(k, v, g.user)
        changed = {k: v for k, v in changes.items() if old.get(k) != v}
        audit_service.log("settings.updated", "settings", section,
                          old={k: old[k][:200] for k in changed}, new={k: v[:200] for k, v in changed.items()})
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings") + f"#{section}")
    return render_template("admin/settings.html", basis=distribution_service.BASIS,
                           methods=distribution_service.METHODS, late=distribution_service.LATE,
                           rules_problems=distribution_service.rules_problems(),
                           period_label=contribution_service.current_period_label(),
                           amount=format_money(int(get_setting("contribution_amount_cents") or 0), show_currency=False),
                           min_payment=format_money(int(get_setting("min_payment_cents") or 0), show_currency=False))
