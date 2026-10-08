"""Member area: dashboard, profile, contributions, statements, projects,
distributions, notifications, meetings, voting and support."""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from extensions import db
from helpers import normalize_phone, utcnow
from models import (Announcement, Distribution, IDDocument, Meeting, MeetingAttendance, Notification, Project,
                    Report, SupportTicket, Vote, VoteBallot)
from permissions import role_required
from services import (anomaly_service, audit_service, contribution_service, file_service, governance_service,
                      report_service)
from services.project_service import financials

bp = Blueprint("members", __name__, url_prefix="/member")

TICKET_CATEGORIES = ["Payments", "Contributions", "Distributions", "Account", "Projects", "Other"]


def _member():
    member = g.user.member
    if member is None:
        abort(403)
    return member


@bp.route("/")
@role_required("member")
def dashboard():
    member = _member()
    summary = contribution_service.member_summary(member)
    history = contribution_service.payment_history(member)[:5]
    distributions = Distribution.query.filter_by(member_id=member.id).order_by(Distribution.created_at.desc()).limit(5).all()
    notifications = Notification.query.filter_by(user_id=g.user.id).order_by(Notification.created_at.desc()).limit(5).all()
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(3).all()
    projects = Project.query.filter(Project.status.in_(["Active", "Delayed", "Funding Pending", "Approved"])).all()
    meetings = Meeting.query.filter(Meeting.meeting_at >= utcnow(), Meeting.status == "Scheduled") \
        .order_by(Meeting.meeting_at).limit(3).all()
    return render_template("member/dashboard.html", member=member, summary=summary, history=history,
                           distributions=distributions, notifications=notifications, announcements=announcements,
                           projects=projects, meetings=meetings)


@bp.route("/profile", methods=["GET", "POST"])
@role_required("member")
def profile():
    member = _member()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "contact":
                phone = normalize_phone(request.form.get("phone"))
                if not phone:
                    raise ValueError("Enter a valid phone number.")
                old = member.phone
                member.phone = phone
                audit_service.log("member.phone_changed", "member", member.member_number,
                                  old={"phone": old}, new={"phone": phone})
                anomaly_service.check_duplicate_account(member)
                flash("Phone number updated.", "success")
            elif action == "photo":
                saved = file_service.save_upload(request.files.get("profile_photo"), "profile_photos",
                                                 file_service.IMAGE_TYPES)
                member.profile_photo = saved["stored_name"]
                audit_service.log("member.photo_updated", "member", member.member_number)
                flash("Profile photo updated.", "success")
            elif action == "id_document":
                saved = file_service.save_upload(request.files.get("id_document"), "id_documents")
                for doc in member.id_documents:
                    doc.is_current = False
                db.session.add(IDDocument(member_id=member.id, original_name=saved["original_name"],
                                          stored_name=saved["stored_name"], mime_type=saved["mime_type"],
                                          size_bytes=saved["size_bytes"], sha256=saved["sha256"]))
                audit_service.log("member.id_document_uploaded", "member", member.member_number)
                flash("ID document uploaded.", "success")
            db.session.commit()
        except file_service.UploadError as exc:
            db.session.rollback()
            return render_template("errors/upload_error.html", message=str(exc),
                                   back=url_for("members.profile")), 400
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("members.profile"))
    return render_template("member/profile.html", member=member)


@bp.route("/photo")
@role_required("member")
def photo():
    member = _member()
    if not member.profile_photo:
        abort(404)
    ext = member.profile_photo.rsplit(".", 1)[-1]
    return file_service.send_private("profile_photos", member.profile_photo,
                                     "image/png" if ext == "png" else "image/jpeg", inline=True)


@bp.route("/id-document")
@role_required("member")
def id_document():
    doc = _member().current_id_document
    if doc is None:
        abort(404)
    return file_service.send_private("id_documents", doc.stored_name, doc.mime_type,
                                     download_name=f"my-id-document.{doc.stored_name.rsplit('.', 1)[-1]}")


@bp.route("/contributions")
@role_required("member")
def contributions():
    member = _member()
    return render_template("member/contributions.html", member=member,
                           summary=contribution_service.member_summary(member))


@bp.route("/payments")
@role_required("member")
def payments():
    member = _member()
    return render_template("member/payments.html", member=member,
                           history=contribution_service.payment_history(member),
                           summary=contribution_service.member_summary(member))


@bp.route("/statement")
@bp.route("/statement.<fmt>")
@role_required("member")
def statement(fmt=None):
    member = _member()
    report = report_service.member_statement(member)
    if fmt == "csv":
        return Response(report_service.to_csv(report), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=statement-{member.member_number}.csv"})
    if fmt == "pdf":
        return Response(report_service.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=statement-{member.member_number}.pdf"})
    if fmt:
        abort(404)
    return render_template("member/statement.html", report=report, member=member)


@bp.route("/projects")
@role_required("member")
def projects():
    items = Project.query.filter(Project.status.not_in(["Proposed", "Cancelled"])).order_by(Project.created_at.desc()).all()
    return render_template("member/projects.html", items=[(p, financials(p)) for p in items])


@bp.route("/distributions")
@role_required("member")
def distributions():
    member = _member()
    items = Distribution.query.filter_by(member_id=member.id).order_by(Distribution.created_at.desc()).all()
    paid = sum(d.amount_cents for d in items if d.status == "Paid")
    return render_template("member/distributions.html", items=items, paid=paid)


@bp.route("/notifications", methods=["GET", "POST"])
@role_required("member", "partner", "super_admin", "finance_officer", "project_manager", "auditor")
def notifications():
    if request.method == "POST":
        Notification.query.filter_by(user_id=g.user.id, read_at=None).update({"read_at": utcnow()})
        db.session.commit()
        flash("All notifications marked as read.", "success")
        return redirect(url_for("members.notifications"))
    items = Notification.query.filter_by(user_id=g.user.id).order_by(Notification.created_at.desc()).limit(200).all()
    return render_template("member/notifications.html", items=items)


@bp.route("/reports")
@role_required("member")
def reports():
    items = Report.query.filter_by(is_published=True).order_by(Report.published_at.desc()).all()
    return render_template("member/reports.html", items=items)


@bp.route("/meetings", methods=["GET", "POST"])
@role_required("member")
def meetings():
    member = _member()
    if request.method == "POST":
        meeting = db.get_or_404(Meeting, request.form.get("meeting_id", type=int))
        choice = request.form.get("rsvp")
        if meeting.status != "Scheduled" or choice not in ("Attending", "Not attending"):
            abort(400)
        row = MeetingAttendance.query.filter_by(meeting_id=meeting.id, member_id=member.id).first()
        if row is None:
            row = MeetingAttendance(meeting_id=meeting.id, member_id=member.id, status=choice)
            db.session.add(row)
        elif row.status in ("Attending", "Not attending"):
            row.status = choice
        db.session.commit()
        flash("Your reply has been saved.", "success")
        return redirect(url_for("members.meetings"))
    items = Meeting.query.order_by(Meeting.meeting_at.desc()).all()
    mine = {a.meeting_id: a.status for a in MeetingAttendance.query.filter_by(member_id=member.id)}
    return render_template("member/meetings.html", items=items, mine=mine, now=utcnow())


@bp.route("/votes")
@role_required("member")
def votes():
    member = _member()
    items = Vote.query.filter(Vote.status != "Cancelled").order_by(Vote.opens_at.desc()).all()
    voted = {b.vote_id: b.choice for b in VoteBallot.query.filter_by(member_id=member.id)}
    rows = [(v, governance_service.vote_is_open(v), governance_service.member_eligible(member, v)) for v in items]
    return render_template("member/votes.html", rows=rows, voted=voted)


@bp.route("/votes/<int:vote_id>", methods=["GET", "POST"])
@role_required("member")
def vote_detail(vote_id):
    member = _member()
    vote = db.get_or_404(Vote, vote_id)
    if request.method == "POST":
        try:
            governance_service.cast_ballot(vote, member, request.form.get("choice"))
            db.session.commit()
            flash("Your vote has been recorded. Thank you.", "success")
        except governance_service.GovernanceError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("members.vote_detail", vote_id=vote.id))
    ballot = VoteBallot.query.filter_by(vote_id=vote.id, member_id=member.id).first()
    return render_template("member/vote_detail.html", vote=vote, ballot=ballot,
                           is_open=governance_service.vote_is_open(vote),
                           eligible=governance_service.member_eligible(member, vote))


@bp.route("/support", methods=["GET", "POST"])
@role_required("member")
def support():
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        category = request.form.get("category")
        description = (request.form.get("description") or "").strip()
        if not subject or category not in TICKET_CATEGORIES or len(description) < 10:
            flash("Fill in the subject, category and a description (at least 10 characters).", "error")
        else:
            t = governance_service.open_ticket(g.user, subject, category, description)
            db.session.commit()
            flash(f"Ticket {t.ticket_number} created.", "success")
            return redirect(url_for("members.ticket", ticket_id=t.id))
    items = SupportTicket.query.filter_by(user_id=g.user.id).order_by(SupportTicket.created_at.desc()).all()
    return render_template("member/support.html", items=items, categories=TICKET_CATEGORIES)


@bp.route("/support/<int:ticket_id>", methods=["GET", "POST"])
@role_required("member")
def ticket(ticket_id):
    t = db.get_or_404(SupportTicket, ticket_id)
    if t.user_id != g.user.id:
        abort(404)
    if request.method == "POST":
        if t.status == "Closed":
            flash("This ticket is closed. Please open a new one.", "error")
        else:
            try:
                governance_service.respond(t, g.user, request.form.get("body"))
                db.session.commit()
                flash("Reply sent.", "success")
            except governance_service.GovernanceError as exc:
                flash(str(exc), "error")
        return redirect(url_for("members.ticket", ticket_id=t.id))
    return render_template("member/ticket.html", ticket=t)
