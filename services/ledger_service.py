"""The organisation's financial ledger (double entry, integer cents).

How it works
------------
* Every financial event is a Transaction (amount always positive).
* When a transaction is APPROVED it is "posted": two LedgerEntry lines are
  written - one debit, one credit - so the books always balance.
* Balances are never typed in by anyone. They are always calculated by adding
  up ledger lines.
* Mistakes are corrected with a REVERSAL transaction (lines swapped). Nothing
  is ever edited or deleted.

Accounts
--------
Assets:   CASH_MPESA, CASH_BANK, RESERVE_FUND, PROJECT_FUNDS
Equity:   OPENING_EQUITY, MEMBER_CONTRIBUTIONS, ADJUSTMENTS
Contra:   DISTRIBUTIONS, REFUNDS (reduce equity; shown as positive amounts paid out)
Income:   PROJECT_REVENUE, OTHER_INCOME
Expense:  PROJECT_EXPENSE, OPERATING_EXPENSE, FEES, OTHER_EXPENSE
"""
from datetime import date

from sqlalchemy import case, func, select

from extensions import db
from helpers import format_money, new_reference, utcnow
from models import LedgerEntry, Transaction
from services import anomaly_service, audit_service
from services.settings_service import get_bool

ACCOUNTS = {
    "CASH_MPESA": ("Mobile money (M-PESA)", "asset"),
    "CASH_BANK": ("Bank account", "asset"),
    "RESERVE_FUND": ("Reserve fund", "asset"),
    "PROJECT_FUNDS": ("Funds released to projects (unspent)", "asset"),
    "OPENING_EQUITY": ("Opening balance", "equity"),
    "MEMBER_CONTRIBUTIONS": ("Member contributions", "equity"),
    "DISTRIBUTIONS": ("Distributions paid", "contra-equity"),
    "REFUNDS": ("Refunds to members", "contra-equity"),
    "ADJUSTMENTS": ("Adjustments", "equity"),
    "PROJECT_REVENUE": ("Project revenue", "income"),
    "OTHER_INCOME": ("Other income", "income"),
    "PROJECT_EXPENSE": ("Project expenses", "expense"),
    "OPERATING_EXPENSE": ("Operating expenses", "expense"),
    "FEES": ("Bank and provider fees", "expense"),
    "OTHER_EXPENSE": ("Other expenses", "expense"),
}
CASH_ACCOUNTS = ("CASH_MPESA", "CASH_BANK")
HOLDING_ACCOUNTS = CASH_ACCOUNTS + ("RESERVE_FUND",)  # money the organisation holds

TXN_TYPES = {
    "opening_balance": "Opening balance",
    "member_contribution": "Member contribution",
    "project_funding": "Project funding",
    "project_revenue": "Project revenue",
    "project_expense": "Project expense",
    "operating_expense": "Operating expense",
    "bank_fee": "Bank fee",
    "provider_fee": "Payment provider fee",
    "refund": "Refund",
    "distribution": "Distribution",
    "reserve": "Transfer to reserve",
    "reserve_release": "Release from reserve",
    "cash_transfer": "Transfer between accounts",
    "adjustment": "Adjustment",
    "other": "Other approved transaction",
}
# Types a finance officer may enter by hand. The others are created by their
# own workflows (payments, project funding, expense verification, distributions).
MANUAL_TYPES = ("opening_balance", "operating_expense", "bank_fee", "provider_fee", "refund",
                "reserve", "reserve_release", "cash_transfer", "adjustment", "other")
INCOME_TYPES = ("member_contribution", "project_revenue")


class LedgerError(Exception):
    pass


def _other_cash(account):
    return "CASH_BANK" if account == "CASH_MPESA" else "CASH_MPESA"


def account_pair(txn):
    """Return (debit_account, credit_account) for a transaction."""
    c = txn.cash_account if txn.cash_account in CASH_ACCOUNTS else "CASH_BANK"
    incoming = txn.direction != "out"
    mapping = {
        "opening_balance": (c, "OPENING_EQUITY"),
        "member_contribution": (c, "MEMBER_CONTRIBUTIONS"),
        "project_funding": ("PROJECT_FUNDS", c),
        "project_revenue": (c, "PROJECT_REVENUE"),
        "project_expense": ("PROJECT_EXPENSE", "PROJECT_FUNDS"),
        "operating_expense": ("OPERATING_EXPENSE", c),
        "bank_fee": ("FEES", c),
        "provider_fee": ("FEES", c),
        "refund": ("REFUNDS", c),
        "distribution": ("DISTRIBUTIONS", c),
        "reserve": ("RESERVE_FUND", c),
        "reserve_release": (c, "RESERVE_FUND"),
        "cash_transfer": (_other_cash(c), c),   # from cash_account to the other cash account
        "adjustment": (c, "ADJUSTMENTS") if incoming else ("ADJUSTMENTS", c),
        "other": (c, "OTHER_INCOME") if incoming else ("OTHER_EXPENSE", c),
    }
    if txn.type not in mapping:
        raise LedgerError(f"Unknown transaction type: {txn.type}")
    debit, credit = mapping[txn.type]
    if txn.is_reversal:
        debit, credit = credit, debit
    return debit, credit


# ---------------------------------------------------------------- balances
def account_balance(account, as_of=None):
    """Natural balance of one account (assets/expenses/contra-equity: debit-credit; others: credit-debit)."""
    q = select(func.coalesce(func.sum(LedgerEntry.debit_cents - LedgerEntry.credit_cents), 0)) \
        .where(LedgerEntry.account == account)
    if as_of is not None:
        q = q.join(Transaction).where(Transaction.txn_date <= as_of)
    raw = int(db.session.execute(q).scalar_one())
    kind = ACCOUNTS[account][1]
    return raw if kind in ("asset", "expense", "contra-equity") else -raw


def all_balances(as_of=None):
    return {acc: account_balance(acc, as_of) for acc in ACCOUNTS}


def available_cash(account=None):
    if account:
        return account_balance(account)
    return sum(account_balance(a) for a in CASH_ACCOUNTS)


def holding_effect_by_type(start=None, end=None):
    """Net effect on money held (cash + reserve) grouped by (type, direction)."""
    effect = func.sum(case((LedgerEntry.account.in_(HOLDING_ACCOUNTS),
                            LedgerEntry.debit_cents - LedgerEntry.credit_cents), else_=0))
    q = select(Transaction.type, Transaction.direction, func.coalesce(effect, 0)) \
        .join(LedgerEntry, LedgerEntry.transaction_id == Transaction.id) \
        .group_by(Transaction.type, Transaction.direction)
    if start:
        q = q.where(Transaction.txn_date >= start)
    if end:
        q = q.where(Transaction.txn_date <= end)
    return [(t, d, int(v)) for t, d, v in db.session.execute(q).all()]


def financial_position(start=None, end=None):
    """Opening + verified income - verified outflows + approved adjustments = calculated balance.

    With no dates this covers all time; with dates, "opening" is the money held
    at the start of the period.
    """
    if start:
        opening = sum(v for _, _, v in holding_effect_by_type(None, date.fromordinal(start.toordinal() - 1)))
    else:
        opening = 0
    income, outflows, adjustments = [], [], 0
    for ttype, direction, value in holding_effect_by_type(start, end):
        if ttype == "opening_balance":
            opening += value
        elif ttype == "adjustment":
            adjustments += value
        elif value > 0:
            income.append((label_for(ttype, direction), value))
        elif value < 0:
            outflows.append((label_for(ttype, direction), -value))
    total_income = sum(v for _, v in income)
    total_out = sum(v for _, v in outflows)
    closing = opening + total_income - total_out + adjustments
    return {
        "opening": opening,
        "income": sorted(income, key=lambda x: -x[1]),
        "total_income": total_income,
        "outflows": sorted(outflows, key=lambda x: -x[1]),
        "total_outflows": total_out,
        "adjustments": adjustments,
        "closing": closing,
    }


def label_for(ttype, direction=None):
    label = TXN_TYPES.get(ttype, ttype)
    if ttype == "other":
        label += " (in)" if direction != "out" else " (out)"
    return label


def summary():
    """Headline figures used by dashboards and the transparency page."""
    b = all_balances()
    pos = financial_position()
    return {
        "mpesa": b["CASH_MPESA"],
        "bank": b["CASH_BANK"],
        "available": b["CASH_MPESA"] + b["CASH_BANK"],
        "reserves": b["RESERVE_FUND"],
        "held": b["CASH_MPESA"] + b["CASH_BANK"] + b["RESERVE_FUND"],
        "project_funds_unspent": b["PROJECT_FUNDS"],
        "contributions": b["MEMBER_CONTRIBUTIONS"],
        "distributions": b["DISTRIBUTIONS"],
        "project_revenue": b["PROJECT_REVENUE"],
        "project_expenses": b["PROJECT_EXPENSE"],
        "invested": total_by_type("project_funding"),
        "position": pos,
        "balanced": ledger_is_balanced(),
    }


def total_by_type(ttype, start=None, end=None, project_id=None):
    """Net posted amount of a type (reversals subtract)."""
    signed = case((Transaction.is_reversal.is_(True), -Transaction.amount_cents), else_=Transaction.amount_cents)
    q = select(func.coalesce(func.sum(signed), 0)).where(Transaction.type == ttype,
                                                         Transaction.approval_status == "approved")
    if project_id is not None:
        q = q.where(Transaction.project_id == project_id)
    if start:
        q = q.where(Transaction.txn_date >= start)
    if end:
        q = q.where(Transaction.txn_date <= end)
    return int(db.session.execute(q).scalar_one())


def ledger_is_balanced():
    q = select(func.coalesce(func.sum(LedgerEntry.debit_cents), 0),
               func.coalesce(func.sum(LedgerEntry.credit_cents), 0))
    debit, credit = db.session.execute(q).one()
    return int(debit) == int(credit)


# ---------------------------------------------------------------- writing
def create_transaction(ttype, amount_cents, description, created_by, *, txn_date=None,
                       cash_account="CASH_BANK", direction="in", project=None, member=None,
                       reference=None, category="", evidence_note=None, auto_approve=False,
                       verification_status="unverified", approver=None):
    if ttype not in TXN_TYPES:
        raise LedgerError("Unknown transaction type.")
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise LedgerError("Amount must be a positive whole number of cents.")
    if cash_account not in CASH_ACCOUNTS:
        raise LedgerError("Choose the bank or mobile-money account.")
    if direction not in ("in", "out"):
        raise LedgerError("Direction must be in or out.")
    if not (description or "").strip():
        raise LedgerError("A description is required.")
    txn = Transaction(
        txn_number=new_reference("TXN"), type=ttype, category=category or TXN_TYPES[ttype],
        description=description.strip()[:255], amount_cents=amount_cents, direction=direction,
        cash_account=cash_account, reference=(reference or None), txn_date=txn_date or date.today(),
        project_id=project.id if project else None, member_id=member.id if member else None,
        created_by_id=created_by.id if created_by else None, evidence_note=evidence_note,
        approval_status="pending", verification_status="unverified",
    )
    db.session.add(txn)
    db.session.flush()
    audit_service.log("transaction.created", "transaction", txn.txn_number,
                      new={"type": ttype, "amount_cents": amount_cents, "description": txn.description},
                      user=created_by)
    anomaly_service.check_duplicate_transaction(txn)
    if auto_approve:
        _post(txn, approver, verification_status)
    return txn


def approve(txn, approver, evidence_note=None):
    """Approve (and verify) a pending manual transaction, then post it."""
    if txn.approval_status != "pending":
        raise LedgerError("Only pending transactions can be approved.")
    if get_bool("require_two_person_approval") and txn.created_by_id == approver.id:
        raise LedgerError("Two-person rule: a different officer must approve this transaction.")
    if evidence_note:
        txn.evidence_note = evidence_note[:255]
    _post(txn, approver, "verified")
    audit_service.log("transaction.approved", "transaction", txn.txn_number,
                      old={"approval_status": "pending"}, new={"approval_status": "approved"},
                      reason=evidence_note, user=approver)
    anomaly_service.check_staff_activity(approver)
    return txn


def reject(txn, user, reason):
    if txn.approval_status != "pending":
        raise LedgerError("Only pending transactions can be rejected.")
    if not (reason or "").strip():
        raise LedgerError("Give a reason for rejecting.")
    txn.approval_status = "rejected"
    txn.rejection_reason = reason.strip()[:255]
    txn.approved_by_id = user.id
    txn.approved_at = utcnow()
    audit_service.log("transaction.rejected", "transaction", txn.txn_number, reason=reason, user=user)
    _after_rejected(txn)


def _check_funds(txn):
    """Money cannot leave an account that does not hold it.
    Reversals are corrections of records and are always allowed."""
    if txn.is_reversal:
        return
    _, credit = account_pair(txn)
    if credit in HOLDING_ACCOUNTS:
        balance = account_balance(credit)
        if balance < txn.amount_cents:
            raise LedgerError(f"Not enough money in {ACCOUNTS[credit][0]}: balance is "
                              f"{format_money(balance)}, needed {format_money(txn.amount_cents)}.")


def _post(txn, approver, verification_status):
    if txn.lines:
        raise LedgerError("Transaction already posted.")
    _check_funds(txn)
    debit, credit = account_pair(txn)
    txn.lines.append(LedgerEntry(account=debit, debit_cents=txn.amount_cents, credit_cents=0))
    txn.lines.append(LedgerEntry(account=credit, debit_cents=0, credit_cents=txn.amount_cents))
    now = utcnow()
    txn.approval_status = "approved"
    txn.approved_by_id = approver.id if approver else None
    txn.approved_at = now
    txn.verification_status = verification_status
    txn.verified_by_id = approver.id if approver else None
    txn.verified_at = now
    txn.posted_at = now
    db.session.flush()
    _after_posted(txn, approver)


def reverse(txn, user, reason):
    """Create a correcting entry that cancels a posted transaction."""
    if txn.approval_status != "approved" or not txn.lines:
        raise LedgerError("Only posted (approved) transactions can be reversed.")
    if txn.is_reversal:
        raise LedgerError("A reversal cannot itself be reversed. Create a new transaction instead.")
    if txn.reversed_by_txn_id:
        raise LedgerError("This transaction has already been reversed.")
    if not (reason or "").strip():
        raise LedgerError("Give a reason for the reversal.")
    rev = Transaction(
        txn_number=new_reference("TXN"), type=txn.type, category=txn.category,
        description=f"REVERSAL of {txn.txn_number}: {reason.strip()}"[:255],
        amount_cents=txn.amount_cents, direction=txn.direction, cash_account=txn.cash_account,
        reference=txn.reference, txn_date=date.today(), project_id=txn.project_id,
        member_id=txn.member_id, created_by_id=user.id if user else None,
        is_reversal=True, reverses_id=txn.id, approval_status="pending",
    )
    db.session.add(rev)
    db.session.flush()
    _post(rev, user, "verified")
    txn.reversed_by_txn_id = rev.id
    audit_service.log("transaction.reversed", "transaction", txn.txn_number,
                      new={"reversal": rev.txn_number}, reason=reason, user=user)
    anomaly_service.check_staff_activity(user)
    return rev


def _after_posted(txn, user):
    """Hooks that keep other records in step with the ledger."""
    if txn.type == "project_funding" and txn.project_id:
        from services import project_service
        project_service.on_funding_posted(txn, user)


def _after_rejected(txn):
    pass
