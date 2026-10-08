"""Basic server-side anomaly checks.

Each check only CREATES AN ALERT for a human to review. Nothing here blocks
a user or labels anyone as a fraudster.
"""
from datetime import timedelta

from sqlalchemy import func, select

from extensions import db
from helpers import utcnow
from models import AnomalyFlag, AuditLog, Distribution, LoginAttempt, Member, Payment, Transaction

SENSITIVE_ACTIONS = ("transaction.approved", "transaction.reversed", "payment.reversed",
                     "payment.verified_manual", "distribution.paid", "distribution.reversed",
                     "member.id_document_viewed", "staff.role_changed", "settings.updated")


def flag(flag_type, description, record_type=None, record_id=None, severity="medium"):
    existing = AnomalyFlag.query.filter_by(flag_type=flag_type, record_type=record_type,
                                           record_id=str(record_id) if record_id else None,
                                           status="Open").first()
    if existing:
        return existing
    item = AnomalyFlag(flag_type=flag_type, description=description, record_type=record_type,
                       record_id=str(record_id) if record_id else None, severity=severity)
    db.session.add(item)
    return item


def check_repeated_failed_payments(member):
    since = utcnow() - timedelta(hours=24)
    count = db.session.execute(
        select(func.count(Payment.id)).where(Payment.member_id == member.id,
                                             Payment.status.in_(["Failed", "Cancelled"]),
                                             Payment.created_at >= since)).scalar_one()
    if count >= 3:
        flag("repeated_failed_payments",
             f"Member {member.member_number} has {count} failed or cancelled payments in 24 hours. "
             "This may be a phone or balance problem - please review.",
             "member", member.id, "low")


def check_duplicate_transaction(txn):
    """Same type, amount and reference/project within 24h."""
    since = (txn.created_at or utcnow()) - timedelta(hours=24)
    q = Transaction.query.filter(Transaction.id != txn.id, Transaction.type == txn.type,
                                 Transaction.amount_cents == txn.amount_cents,
                                 Transaction.is_reversal.is_(False),
                                 Transaction.created_at >= since)
    if txn.reference:
        q = q.filter(Transaction.reference == txn.reference)
    elif txn.project_id:
        q = q.filter(Transaction.project_id == txn.project_id)
    else:
        q = q.filter(Transaction.description == txn.description)
    other = q.first()
    if other:
        flag("possible_duplicate_transaction",
             f"Transaction {txn.txn_number} looks similar to {other.txn_number} "
             "(same type and amount within 24 hours). Please confirm both are genuine.",
             "transaction", txn.id)


def duplicate_payment_reference(receipt, payment):
    flag("duplicate_payment_reference",
         f"Payment {payment.reference} reported provider reference {receipt}, which is already "
         "used by another payment. The payment was NOT recorded twice.",
         "payment", payment.id, "high")


def check_staff_activity(user):
    if user is None or not user.is_staff:
        return
    since = utcnow() - timedelta(minutes=10)
    count = db.session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.user_id == user.id,
                                              AuditLog.action.in_(SENSITIVE_ACTIONS),
                                              AuditLog.created_at >= since)).scalar_one()
    if count >= 20:
        flag("unusual_admin_activity",
             f"{user.email} performed {count} sensitive actions in 10 minutes.",
             "user", user.id, "medium")


def check_failed_logins(email, user):
    if user is None or not user.is_staff:
        return
    since = utcnow() - timedelta(hours=1)
    count = db.session.execute(
        select(func.count(LoginAttempt.id)).where(LoginAttempt.email == email,
                                                  LoginAttempt.success.is_(False),
                                                  LoginAttempt.created_at >= since)).scalar_one()
    if count >= 5:
        flag("staff_failed_logins", f"{count} failed log-in attempts for staff account {email} in one hour.",
             "user", user.id, "high")


def check_distribution_run(run):
    amounts = sorted(d.amount_cents for d in run.distributions)
    if not amounts:
        return
    median = amounts[len(amounts) // 2]
    largest = amounts[-1]
    if median and largest > median * 3 and len(amounts) >= 3:
        flag("unusual_distribution",
             f"In {run.run_number} the largest share is more than 3x the median share. "
             "This can be normal with pro-rata rules; please confirm.", "distribution_run", run.id, "low")
    if largest > run.net_distributable_cents:
        flag("unusual_distribution", f"A share in {run.run_number} exceeds the distributable amount.",
             "distribution_run", run.id, "high")


def check_distribution_payment(dist):
    others = Distribution.query.filter(Distribution.member_id == dist.member_id,
                                       Distribution.run_id == dist.run_id,
                                       Distribution.id != dist.id,
                                       Distribution.status == "Paid").count()
    if others:
        flag("unusual_distribution", f"Member received more than one payment in run {dist.run.run_number}.",
             "distribution", dist.id, "high")


def check_duplicate_account(member):
    same_phone = Member.query.filter(Member.phone == member.phone, Member.id != member.id).all()
    if same_phone:
        numbers = ", ".join(m.member_number or str(m.id) for m in same_phone)
        flag("possible_duplicate_account",
             f"Member {member.member_number} uses the same phone number as {numbers}. "
             "Family members sometimes share phones - please review.", "member", member.id, "low")
