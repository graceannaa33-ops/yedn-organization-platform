"""Profit distribution.

NO ASSUMPTIONS ABOUT ELIGIBILITY: until a Super Admin documents the rules in
Settings -> Distribution rules, no distribution can be calculated.

The three rules
---------------
1. Eligibility basis (on the record date):
   - fully_paid        only members whose verified payments cover everything
                       they were required to pay by the record date.
   - any_contribution  every member with a verified contribution above zero.
2. Allocation method:
   - equal     the distributable amount is split equally.
   - pro_rata  shares are proportional to each member's verified contribution.
3. Late completion policy - for members whose contribution was completed
   (or, with any_contribution, first made) AFTER the contribution deadline:
   - current_and_future  treated like everyone else: they qualify for the
                         current distribution and future ones.
   - future_only         they qualify only for projects first funded AFTER
                         they completed; not for projects already funded.
   - none                they do not qualify for distributions.

Amounts are integer cents; shares are rounded DOWN and the leftover cents are
reported as an undistributed remainder that stays with the organisation.
"""
import json

from sqlalchemy import func, select

from extensions import db
from helpers import format_money, new_reference, parse_date, utcnow
from models import Distribution, DistributionRun, Member
from services import anomaly_service, audit_service, contribution_service, ledger_service, notification_service
from services.settings_service import get_bool, get_setting

BASIS = {"fully_paid": "Fully paid members only (as at the record date)",
         "any_contribution": "Any member with a verified contribution (as at the record date)"}
METHODS = {"equal": "Equal share for each eligible member",
           "pro_rata": "In proportion to each member's verified contribution"}
LATE = {"current_and_future": "Late completers qualify for current and future distributions",
        "future_only": "Late completers qualify only for projects funded after they completed",
        "none": "Late completers do not qualify for distributions"}


class DistributionError(Exception):
    pass


def current_rules():
    return {"basis": get_setting("dist_eligibility_basis"), "method": get_setting("dist_allocation_method"),
            "late_policy": get_setting("dist_late_policy"), "deadline": get_setting("contribution_due_date"),
            "notes": get_setting("dist_rules_notes")}


def rules_problems(rules=None):
    rules = rules or current_rules()
    problems = []
    if rules["basis"] not in BASIS:
        problems.append("Eligibility basis is not set.")
    if rules["method"] not in METHODS:
        problems.append("Allocation method is not set.")
    if rules["late_policy"] not in LATE:
        problems.append("Late-completion policy is not set.")
    if rules["late_policy"] in ("future_only", "none") and not parse_date(rules["deadline"]):
        problems.append("A contribution deadline must be set for the chosen late-completion policy.")
    return problems


def rules_text(rules=None):
    rules = rules or current_rules()
    if rules_problems(rules):
        return None
    lines = [BASIS[rules["basis"]] + ".", METHODS[rules["method"]] + "."]
    deadline = parse_date(rules["deadline"])
    if deadline:
        lines.append(f"Contribution deadline: {deadline:%d %b %Y}. {LATE[rules['late_policy']]}.")
    else:
        lines.append(LATE[rules["late_policy"]] + ".")
    return lines


def distributed_for_project(project_id):
    q = select(func.coalesce(func.sum(Distribution.amount_cents), 0)).join(DistributionRun).where(
        Distribution.project_id == project_id, Distribution.status.not_in(["Failed", "Reversed"]),
        DistributionRun.status != "Cancelled")
    return int(db.session.execute(q).scalar_one())


def distributable_for_project(project):
    from services.project_service import financials
    net = financials(project)["net"]
    return max(net - distributed_for_project(project.id), 0)


def calculate(project, net_cents, record_date, rules=None):
    """Work out who qualifies and how much. Returns a preview; saves nothing."""
    rules = rules or current_rules()
    problems = rules_problems(rules)
    if problems:
        raise DistributionError("Distribution rules are not configured: " + " ".join(problems))
    if net_cents <= 0:
        raise DistributionError("The distributable amount must be greater than zero.")
    available = distributable_for_project(project)
    if net_cents > available:
        raise DistributionError(f"Only {format_money(available)} of verified net result is available to "
                                f"distribute for this project.")
    from services.project_service import first_funding_datetime
    cutoff = contribution_service.end_of_day(record_date)
    deadline = parse_date(rules["deadline"])
    deadline_end = contribution_service.end_of_day(deadline) if deadline else None
    funded_at = first_funding_datetime(project.id)

    eligible, excluded = [], []
    members = Member.query.filter(Member.joined_at <= cutoff).order_by(Member.id).all()
    for m in members:
        if m.status not in ("active",):
            excluded.append((m, f"Membership status is {m.status}"))
            continue
        paid = contribution_service.paid_as_of(m.id, cutoff)
        required = contribution_service.required_as_of(m.id, cutoff)
        if rules["basis"] == "fully_paid":
            if required <= 0 or paid < required:
                excluded.append((m, "Not fully paid on the record date"))
                continue
            qualified_at = contribution_service.completion_datetime(m.id, cutoff)
        else:
            if paid <= 0:
                excluded.append((m, "No verified contribution on the record date"))
                continue
            qualified_at = contribution_service.first_payment_datetime(m.id, cutoff)
        late = bool(deadline_end and qualified_at and qualified_at > deadline_end)
        if late and rules["late_policy"] == "none":
            excluded.append((m, "Completed after the deadline (late completers do not qualify)"))
            continue
        if late and rules["late_policy"] == "future_only" and funded_at and qualified_at > funded_at:
            excluded.append((m, "Completed after the deadline and after this project was funded"))
            continue
        eligible.append({"member": m, "basis": paid, "late": late})

    if not eligible:
        raise DistributionError("No members qualify under the current rules.")
    if rules["method"] == "equal":
        share = net_cents // len(eligible)
        for e in eligible:
            e["amount"] = share
    else:
        total_basis = sum(e["basis"] for e in eligible)
        for e in eligible:
            e["amount"] = net_cents * e["basis"] // total_basis
    distributed = sum(e["amount"] for e in eligible)
    return {"eligible": eligible, "excluded": excluded, "net": net_cents, "distributed": distributed,
            "remainder": net_cents - distributed, "rules": rules, "available": available}


def create_run(project, net_cents, record_date, title, user, notes=""):
    preview = calculate(project, net_cents, record_date)
    run = DistributionRun(run_number=new_reference("DRN"), project_id=project.id, title=title[:200],
                          record_date=record_date, net_distributable_cents=net_cents,
                          distributed_cents=preview["distributed"], remainder_cents=preview["remainder"],
                          eligible_count=len(preview["eligible"]), rules_snapshot=json.dumps(preview["rules"]),
                          notes=notes, created_by_id=user.id, status="Pending")
    db.session.add(run)
    db.session.flush()
    for e in preview["eligible"]:
        db.session.add(Distribution(dist_number=new_reference("DST"), run_id=run.id, member_id=e["member"].id,
                                    project_id=project.id, amount_cents=e["amount"], basis_cents=e["basis"],
                                    status="Pending"))
    db.session.flush()
    audit_service.log("distribution.run_created", "distribution_run", run.run_number,
                      new={"net_cents": net_cents, "eligible": run.eligible_count, "rules": preview["rules"]},
                      user=user)
    anomaly_service.check_distribution_run(run)
    return run


def approve_run(run, user):
    if run.status != "Pending":
        raise DistributionError("Only pending distributions can be approved.")
    if get_bool("require_two_person_approval") and run.created_by_id == user.id:
        raise DistributionError("Two-person rule: a different officer must approve this distribution.")
    if run.distributed_cents > ledger_service.available_cash():
        raise DistributionError("Available funds are lower than the total to be distributed.")
    run.status, run.approved_by_id, run.approved_at = "Approved", user.id, utcnow()
    for d in run.distributions:
        if d.status == "Pending":
            d.status, d.approver_id = "Approved", user.id
    audit_service.log("distribution.run_approved", "distribution_run", run.run_number, user=user)


def start_processing(run, user):
    if run.status != "Approved":
        raise DistributionError("Approve the distribution first.")
    run.status = "Processing"
    for d in run.distributions:
        if d.status == "Approved":
            d.status = "Processing"
    audit_service.log("distribution.run_processing", "distribution_run", run.run_number, user=user)


def mark_paid(dist, method, reference, cash_account, user):
    if dist.run.status not in ("Approved", "Processing"):
        raise DistributionError("The distribution run must be approved before paying.")
    if dist.status not in ("Approved", "Processing", "Failed"):
        raise DistributionError(f"Cannot pay a distribution that is {dist.status}.")
    if dist.amount_cents <= 0:
        raise DistributionError("Nothing to pay for this line.")
    reference = (reference or "").strip().upper()
    if not reference or method not in ("mpesa", "bank", "cash"):
        raise DistributionError("Enter the payment method and the M-PESA/bank reference.")
    if Distribution.query.filter(Distribution.payment_reference == reference, Distribution.id != dist.id).first():
        raise DistributionError("That payment reference was already used for another distribution.")
    txn = ledger_service.create_transaction(
        "distribution", dist.amount_cents,
        f"Distribution {dist.dist_number} to {dist.member.member_number} ({dist.project.name})", user,
        cash_account=cash_account, project=dist.project, member=dist.member, reference=reference,
        auto_approve=True, verification_status="verified", approver=user)
    dist.status, dist.payment_method, dist.payment_reference = "Paid", method, reference
    dist.payer_id, dist.paid_at, dist.transaction_id = user.id, utcnow(), txn.id
    dist.failure_reason = None
    audit_service.log("distribution.paid", "distribution", dist.dist_number,
                      new={"amount_cents": dist.amount_cents, "reference": reference}, user=user)
    anomaly_service.check_distribution_payment(dist)
    anomaly_service.check_staff_activity(user)
    msg = notification_service.render("tpl_distribution_paid", amount=format_money(dist.amount_cents),
                                      project=dist.project.name, reference=reference)
    notification_service.notify(dist.member.user, "Distribution paid", msg, "distribution", sms=True, email=True)
    _maybe_complete(dist.run)


def mark_failed(dist, reason, user):
    if dist.status not in ("Approved", "Processing"):
        raise DistributionError("Only approved or processing lines can be marked failed.")
    if not (reason or "").strip():
        raise DistributionError("Give a reason.")
    dist.status, dist.failure_reason = "Failed", reason.strip()[:255]
    audit_service.log("distribution.failed", "distribution", dist.dist_number, reason=reason, user=user)


def reverse(dist, reason, user):
    if dist.status != "Paid":
        raise DistributionError("Only paid distributions can be reversed.")
    if not (reason or "").strip():
        raise DistributionError("Give a reason.")
    from models import Transaction
    txn = db.session.get(Transaction, dist.transaction_id)
    ledger_service.reverse(txn, user, f"Distribution {dist.dist_number} reversed: {reason}")
    dist.status, dist.failure_reason = "Reversed", reason.strip()[:255]
    audit_service.log("distribution.reversed", "distribution", dist.dist_number, reason=reason, user=user)


def cancel_run(run, user, reason):
    if run.status not in ("Pending", "Approved"):
        raise DistributionError("Only pending or approved runs with no payments can be cancelled.")
    if any(d.status == "Paid" for d in run.distributions):
        raise DistributionError("Some lines are already paid.")
    if not (reason or "").strip():
        raise DistributionError("Give a reason.")
    run.status = "Cancelled"
    for d in run.distributions:
        d.status = "Failed"
        d.failure_reason = "Run cancelled: " + reason.strip()[:200]
    audit_service.log("distribution.run_cancelled", "distribution_run", run.run_number, reason=reason, user=user)


def _maybe_complete(run):
    if all(d.status in ("Paid", "Reversed") or d.amount_cents == 0 for d in run.distributions):
        run.status = "Completed"
