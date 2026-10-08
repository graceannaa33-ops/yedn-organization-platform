"""Member contribution calculations.

Remaining contribution = Required contribution - Verified (Successful) payments.
Pending, failed, cancelled and reversed payments never count.
"""
from datetime import datetime, time

from sqlalchemy import func, select

from extensions import db
from helpers import parse_date, utcnow
from models import Contribution, Member, Payment
from services.settings_service import get_int, get_setting

STATUS_FULL = "Fully Paid"
STATUS_PARTIAL = "Partially Paid"
STATUS_NONE = "Not Paid"


def current_period_label():
    label = get_setting("contribution_period_label") or "Membership contribution"
    period = get_setting("contribution_period")
    now = utcnow()
    if period == "annual":
        return f"{label} {now.year}"
    if period == "quarterly":
        return f"{label} {now.year} Q{(now.month - 1) // 3 + 1}"
    if period == "monthly":
        return f"{label} {now:%Y-%m}"
    return label


def ensure_current_contribution(member, created_by=None):
    """Create this period's contribution obligation for a member if missing."""
    label = current_period_label()
    existing = Contribution.query.filter_by(member_id=member.id, period_label=label).first()
    if existing:
        return existing
    contribution = Contribution(member_id=member.id, period_label=label,
                                required_cents=get_int("contribution_amount_cents", 1000000),
                                due_date=parse_date(get_setting("contribution_due_date")),
                                created_by_id=created_by.id if created_by else None)
    db.session.add(contribution)
    db.session.flush()
    return contribution


def paid_for_contribution(contribution_id):
    q = select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
        Payment.contribution_id == contribution_id, Payment.status == "Successful")
    return int(db.session.execute(q).scalar_one())


def pending_for_contribution(contribution_id):
    q = select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
        Payment.contribution_id == contribution_id, Payment.status == "Pending")
    return int(db.session.execute(q).scalar_one())


def status_label(required, paid):
    if required > 0 and paid >= required:
        return STATUS_FULL
    if paid > 0:
        return STATUS_PARTIAL
    return STATUS_NONE


def member_summary(member):
    rows = []
    for c in member.contributions:
        paid = paid_for_contribution(c.id)
        rows.append({"contribution": c, "required": c.required_cents, "paid": paid,
                     "remaining": max(c.required_cents - paid, 0),
                     "pending": pending_for_contribution(c.id),
                     "status": status_label(c.required_cents, paid)})
    required = sum(r["required"] for r in rows)
    paid = sum(r["paid"] for r in rows)
    remaining = max(required - paid, 0)
    percent = min(int(paid * 100 // required), 100) if required else 0
    open_rows = [r for r in rows if r["remaining"] > 0]
    return {"required": required, "paid": paid, "remaining": remaining, "percent": percent,
            "status": status_label(required, paid), "rows": rows,
            "open": open_rows[0] if open_rows else None}


def payment_history(member):
    """Every payment with running Total Paid / Remaining after each one."""
    required = sum(c.required_cents for c in member.contributions)
    running = 0
    history = []
    for p in sorted(member.payments, key=lambda x: x.created_at):
        if p.status == "Successful":
            running += p.amount_cents
        history.append({"payment": p, "total_paid": running, "remaining": max(required - running, 0)})
    return list(reversed(history))


def end_of_day(d):
    return datetime.combine(d, time.max)


def start_of_day(d):
    return datetime.combine(d, time.min)


def paid_as_of(member_id, when):
    q = select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
        Payment.member_id == member_id, Payment.status == "Successful", Payment.verified_at <= when)
    return int(db.session.execute(q).scalar_one())


def required_as_of(member_id, when):
    q = select(func.coalesce(func.sum(Contribution.required_cents), 0)).where(
        Contribution.member_id == member_id, Contribution.created_at <= when)
    return int(db.session.execute(q).scalar_one())


def completion_datetime(member_id, when=None):
    """When the member's verified payments first reached their requirement (as of `when`)."""
    when = when or utcnow()
    required = required_as_of(member_id, when)
    if required <= 0:
        return None
    payments = Payment.query.filter(Payment.member_id == member_id, Payment.status == "Successful",
                                    Payment.verified_at <= when).order_by(Payment.verified_at).all()
    total = 0
    for p in payments:
        total += p.amount_cents
        if total >= required:
            return p.verified_at
    return None


def first_payment_datetime(member_id, when=None):
    q = select(func.min(Payment.verified_at)).where(Payment.member_id == member_id,
                                                    Payment.status == "Successful")
    if when:
        q = q.where(Payment.verified_at <= when)
    return db.session.execute(q).scalar_one()


def organisation_counts():
    """Counts of members by payment status, plus totals - computed in two grouped queries."""
    req = dict(db.session.execute(
        select(Contribution.member_id, func.sum(Contribution.required_cents)).group_by(Contribution.member_id)).all())
    paid = dict(db.session.execute(
        select(Payment.member_id, func.sum(Payment.amount_cents)).where(Payment.status == "Successful")
        .group_by(Payment.member_id)).all())
    members = db.session.execute(select(Member.id, Member.status)).all()
    counts = {STATUS_FULL: 0, STATUS_PARTIAL: 0, STATUS_NONE: 0}
    by_status = {"active": 0, "pending": 0, "suspended": 0, "exited": 0}
    for mid, mstatus in members:
        by_status[mstatus] = by_status.get(mstatus, 0) + 1
        if mstatus in ("exited",):
            continue
        counts[status_label(int(req.get(mid) or 0), int(paid.get(mid) or 0))] += 1
    total_required = sum(int(v or 0) for v in req.values())
    total_paid = sum(int(v or 0) for v in paid.values())
    return {"total": len(members), "full": counts[STATUS_FULL], "partial": counts[STATUS_PARTIAL],
            "unpaid": counts[STATUS_NONE], "by_status": by_status,
            "total_required": total_required, "total_paid": total_paid}
