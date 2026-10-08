"""Meetings, voting, support tickets, complaints, announcements and document downloads."""
from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from extensions import db
from helpers import MoneyError, Page, parse_money, utcnow
from models import (CASE_STATUSES, Announcement, Complaint, Document, Meeting, MeetingAttendance, Member,
                    SupportTicket, User, Vote)
from permissions import STAFF_ROLES, has_permission, login_required, permission_required
from services import audit_service, file_service, governance_service

bp = Blueprint("community", __name__)


def _parse_dt(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _local_to_utc(dt):
    """Form times are entered in the organisation's local time zone."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from flask import current_app
    tz = ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])
    return dt.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def _attach(file_storage, title, kind, **owner):
    saved = file_service.save_upload(file_storage, "project_documents")
    doc = Document(kind=kind, title=title or saved["original_name"], stored_name=saved["stored_name"],
                   original_name=saved["original_name"], mime_type=saved["mime_type"],
                   size_bytes=saved["size_bytes"], sha256=saved["sha256"], uploaded_by_id=g.user.id, **owner)
    db.session.add(doc)
    return doc


# ---------------------------------------------------------------- meetings
@bp.route("/admin/meetings", methods=["GET", "POST"])
@permission_required("manage_meetings")
def meetings():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        when = _parse_dt(request.form.get("meeting_at"))
        if not title or not when:
            flash("Enter a title and date/time.", "error")
        else:
            m = Meeting(title=title, meeting_at=_local_to_utc(when), location=request.form.get("location", "").strip(),
                        online_link=request.form.get("online_link", "").strip(),
                        agenda=request.form.get("agenda", "").strip(), created_by_id=g.user.id)
            db.session.add(m)
            db.session.flush()
            audit_service.log("meeting.created", "meeting", m.id, new={"title": title})
            db.session.commit()
            flash("Meeting scheduled.", "success")
            return redirect(url_for("community.meeting", meeting_id=m.id))
    items = Meeting.query.order_by(Meeting.meeting_at.desc()).all()
    return render_template("admin/meetings.html", items=items)


@bp.route("/admin/meetings/<int:meeting_id>", methods=["GET", "POST"])
@permission_required("manage_meetings")
def meeting(meeting_id):
    m = db.get_or_404(Meeting, meeting_id)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "record":
                for f in ("agenda", "minutes", "decisions", "follow_up_actions", "location", "online_link"):
                    setattr(m, f, (request.form.get(f) or "").strip())
                status = request.form.get("status")
                if status in ("Scheduled", "Held", "Cancelled"):
                    m.status = status
                audit_service.log("meeting.updated", "meeting", m.id, new={"status": m.status})
            elif action == "attendance":
                for member in Member.query.filter(Member.status == "active").all():
                    value = request.form.get(f"att_{member.id}")
                    if value in ("Attended", "Absent"):
                        row = MeetingAttendance.query.filter_by(meeting_id=m.id, member_id=member.id).first()
                        if row is None:
                            db.session.add(MeetingAttendance(meeting_id=m.id, member_id=member.id, status=value))
                        else:
                            row.status, row.recorded_at = value, utcnow()
                audit_service.log("meeting.attendance_recorded", "meeting", m.id)
            elif action == "document":
                doc = _attach(request.files.get("file"), request.form.get("title", "").strip(),
                              request.form.get("kind") if request.form.get("kind") in ("minutes", "document") else "document",
                              meeting_id=m.id)
                audit_service.log("meeting.document_uploaded", "meeting", m.id, new={"title": doc.title})
            db.session.commit()
            flash("Meeting updated.", "success")
        except file_service.UploadError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("community.meeting", meeting_id=m.id))
    attendance = {a.member_id: a.status for a in m.attendance}
    members = Member.query.filter_by(status="active").order_by(Member.member_number).all()
    return render_template("admin/meeting_detail.html", m=m, attendance=attendance, members=members)


# ---------------------------------------------------------------- voting
@bp.route("/admin/votes", methods=["GET", "POST"])
@permission_required("manage_votes")
def votes():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        opens, closes = _parse_dt(request.form.get("opens_at")), _parse_dt(request.form.get("closes_at"))
        try:
            budget = parse_money(request.form.get("budget") or "0", allow_zero=True)
        except MoneyError as exc:
            flash(str(exc), "error")
            return redirect(url_for("community.votes"))
        quorum = request.form.get("quorum_percent", type=int)
        pass_pct = request.form.get("pass_percent", type=int)
        if not title or not description or not opens or not closes or closes <= opens:
            flash("Enter a title, description and valid voting dates (close after open).", "error")
        elif quorum is None or not 0 <= quorum <= 100 or pass_pct is None or not 0 <= pass_pct < 100:
            flash("Quorum must be 0-100% and the pass mark 0-99%.", "error")
        elif request.form.get("eligibility") not in ("fully_paid", "active"):
            flash("Choose who may vote.", "error")
        else:
            v = Vote(title=title, description=description, budget_cents=budget,
                     risks=request.form.get("risks", "").strip(), eligibility=request.form.get("eligibility"),
                     opens_at=_local_to_utc(opens), closes_at=_local_to_utc(closes), quorum_percent=quorum,
                     pass_percent=pass_pct, created_by_id=g.user.id)
            db.session.add(v)
            db.session.flush()
            audit_service.log("vote.created", "vote", v.id, new={"title": title, "eligibility": v.eligibility})
            db.session.commit()
            flash("Vote created.", "success")
            return redirect(url_for("community.vote", vote_id=v.id))
    items = Vote.query.order_by(Vote.created_at.desc()).all()
    return render_template("admin/votes.html", items=items)


@bp.route("/admin/votes/<int:vote_id>", methods=["GET", "POST"])
@permission_required("manage_votes")
def vote(vote_id):
    v = db.get_or_404(Vote, vote_id)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "close":
                governance_service.close_vote(v, g.user)
                flash(f"Vote closed. Result: {v.result}.", "success")
            elif action == "cancel" and v.status == "Open" and not v.ballots:
                v.status = "Cancelled"
                audit_service.log("vote.cancelled", "vote", v.id, reason=request.form.get("reason"))
                flash("Vote cancelled.", "info")
            elif action == "document":
                _attach(request.files.get("file"), request.form.get("title", "").strip(), "document", vote_id=v.id)
                audit_service.log("vote.document_uploaded", "vote", v.id)
                flash("Document uploaded.", "success")
            db.session.commit()
        except (governance_service.GovernanceError, file_service.UploadError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("community.vote", vote_id=v.id))
    counts = governance_service.tally(v)
    eligible = len(governance_service.eligible_members(v)) if v.status == "Open" else v.eligible_count
    return render_template("admin/vote_detail.html", v=v, counts=counts, eligible=eligible,
                           is_open=governance_service.vote_is_open(v))


# ---------------------------------------------------------------- support & complaints
def _staff_users():
    from models import Role
    return User.query.join(Role).filter(Role.name.in_(STAFF_ROLES), User.is_active.is_(True)).order_by(User.full_name).all()


@bp.route("/admin/support")
@permission_required("view_support")
def tickets():
    status = request.args.get("status")
    q = select(SupportTicket)
    if status in CASE_STATUSES:
        q = q.where(SupportTicket.status == status)
    page = Page(q.order_by(SupportTicket.updated_at.desc()), request.args.get("page"))
    return render_template("admin/cases.html", page=page, kind="ticket", status=status, statuses=CASE_STATUSES)


@bp.route("/admin/complaints")
@permission_required("view_support")
def complaints():
    status = request.args.get("status")
    q = select(Complaint)
    if status in CASE_STATUSES:
        q = q.where(Complaint.status == status)
    page = Page(q.order_by(Complaint.updated_at.desc()), request.args.get("page"))
    return render_template("admin/cases.html", page=page, kind="complaint", status=status, statuses=CASE_STATUSES)


@bp.route("/admin/support/<int:case_id>", methods=["GET", "POST"], defaults={"kind": "ticket"})
@bp.route("/admin/complaints/<int:case_id>", methods=["GET", "POST"], defaults={"kind": "complaint"})
@permission_required("view_support")
def case(case_id, kind):
    model = SupportTicket if kind == "ticket" else Complaint
    c = db.get_or_404(model, case_id)
    if request.method == "POST":
        if not has_permission(g.user, "manage_support"):
            abort(403)
        assignee = request.form.get("assigned_to_id", type=int)
        if assignee is not None and assignee != c.assigned_to_id:
            staff = db.session.get(User, assignee)
            if staff and staff.is_staff:
                c.assigned_to_id = staff.id
        try:
            governance_service.respond(c, g.user, request.form.get("body"), request.form.get("internal") == "on",
                                       request.form.get("status"), request.form.get("resolution"))
            db.session.commit()
            flash("Case updated.", "success")
        except governance_service.GovernanceError as exc:
            db.session.rollback()
            if assignee:
                db.session.commit()
            flash(str(exc), "error")
        return redirect(request.path)
    return render_template("admin/case_detail.html", c=c, kind=kind, statuses=CASE_STATUSES, staff=_staff_users())


# ---------------------------------------------------------------- announcements
@bp.route("/admin/announcements", methods=["GET", "POST"])
@permission_required("manage_announcements")
def announcements():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        body = (request.form.get("body") or "").strip()
        if not title or not body:
            flash("Enter a title and message.", "error")
        else:
            a = Announcement(title=title, body=body, is_public=request.form.get("is_public") == "on",
                             created_by_id=g.user.id)
            db.session.add(a)
            db.session.flush()
            audit_service.log("announcement.created", "announcement", a.id, new={"title": title})
            db.session.commit()
            flash("Announcement published.", "success")
        return redirect(url_for("community.announcements"))
    items = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template("admin/announcements.html", items=items)


# ---------------------------------------------------------------- document downloads
@bp.route("/documents/<int:doc_id>")
@login_required
def document(doc_id):
    """One download route with the authorisation rules for every attached document."""
    doc = db.get_or_404(Document, doc_id)
    user = g.user
    allowed = False
    if doc.project_id:
        if has_permission(user, "view_projects_admin"):
            allowed = True
        elif user.role_name == "partner" and doc.project and doc.project.partner_id == user.partner_id:
            allowed = True
        elif user.role_name == "member" and doc.is_public:
            allowed = True
    elif doc.meeting_id or doc.vote_id:
        allowed = user.role_name == "member" or user.is_staff
    if not allowed:
        abort(404)
    return file_service.send_private("project_documents", doc.stored_name, doc.mime_type,
                                     download_name=doc.original_name, inline=doc.mime_type.startswith("image/"))
