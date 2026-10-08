"""Finance: ledger, financial position, manual transactions, payments,
manual (bank/cash) payment verification, reconciliation and the
project expense/revenue verification queue."""
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_, select

from extensions import db
from helpers import MoneyError, Page, format_money, parse_date, parse_money, utcnow
from models import Contribution, Member, Payment, Project, ProjectExpense, ProjectRevenue, Reconciliation, Transaction
from permissions import has_permission, permission_required
from services import audit_service, ledger_service, payment_service, project_service
from services.ledger_service import LedgerError
from services.payment_service import PaymentError
from services.settings_service import set_setting

bp = Blueprint("finance", __name__, url_prefix="/admin/finance")


@bp.route("/")
@permission_required("view_finance")
def position():
    s = ledger_service.summary()
    recs = {a: Reconciliation.query.filter_by(account=a).order_by(Reconciliation.reconciled_at.desc()).first()
            for a in ledger_service.CASH_ACCOUNTS}
    balances = ledger_service.all_balances()
    committed = sum(max(p.approved_cents - ledger_service.total_by_type("project_funding", project_id=p.id), 0)
                    for p in Project.query.filter(Project.status.in_(["Approved", "Funding Pending", "Active", "Delayed"])))
    from models import Distribution
    owed = sum(d.amount_cents for d in Distribution.query.filter(Distribution.status.in_(["Approved", "Processing"])))
    return render_template("finance/position.html", s=s, recs=recs, balances=balances,
                           accounts=ledger_service.ACCOUNTS, committed=committed, owed=owed)


@bp.route("/ledger")
@permission_required("view_finance")
def ledger():
    q = select(Transaction)
    args = request.args
    if args.get("type") in ledger_service.TXN_TYPES:
        q = q.where(Transaction.type == args["type"])
    if args.get("status") in ("pending", "approved", "rejected"):
        q = q.where(Transaction.approval_status == args["status"])
    if args.get("project", type=int):
        q = q.where(Transaction.project_id == args.get("project", type=int))
    start, end = parse_date(args.get("start")), parse_date(args.get("end"))
    if start:
        q = q.where(Transaction.txn_date >= start)
    if end:
        q = q.where(Transaction.txn_date <= end)
    search = (args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.where(or_(Transaction.txn_number.ilike(like), Transaction.description.ilike(like),
                        Transaction.reference.ilike(like)))
    page = Page(q.order_by(Transaction.id.desc()), args.get("page"), 30)
    return render_template("finance/ledger.html", page=page, types=ledger_service.TXN_TYPES, args=args,
                           projects=Project.query.order_by(Project.name).all())


@bp.route("/transactions/new", methods=["GET", "POST"])
@permission_required("create_transaction")
def new_transaction():
    if request.method == "POST":
        f = request.form
        try:
            ttype = f.get("type")
            if ttype not in ledger_service.MANUAL_TYPES:
                raise LedgerError("This type is created by its own workflow (payments, projects or distributions).")
            amount = parse_money(f.get("amount"))
            project = db.session.get(Project, f.get("project_id", type=int)) if f.get("project_id") else None
            member = None
            if f.get("member_number"):
                member = Member.query.filter_by(member_number=f.get("member_number").strip().upper()).first()
                if member is None:
                    raise LedgerError("No member with that member ID.")
            if ttype == "refund" and member is None:
                raise LedgerError("A refund must name the member it is paid to.")
            txn = ledger_service.create_transaction(
                ttype, amount, f.get("description", ""), g.user, txn_date=parse_date(f.get("txn_date")) or date.today(),
                cash_account=f.get("cash_account"), direction=f.get("direction", "in"), project=project,
                member=member, reference=(f.get("reference") or "").strip()[:100] or None,
                category=(f.get("category") or "").strip()[:80], evidence_note=(f.get("evidence") or "").strip()[:255])
            db.session.commit()
            flash(f"Transaction {txn.txn_number} saved and waiting for approval.", "success")
            return redirect(url_for("finance.transaction", txn_id=txn.id))
        except (LedgerError, MoneyError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("finance/transaction_new.html", types={k: ledger_service.TXN_TYPES[k] for k in ledger_service.MANUAL_TYPES},
                           projects=Project.query.order_by(Project.name).all(), form=request.form, today=date.today())


@bp.route("/transactions/<int:txn_id>", methods=["GET", "POST"])
@permission_required("view_finance")
def transaction(txn_id):
    txn = db.get_or_404(Transaction, txn_id)
    if request.method == "POST":
        action = request.form.get("action")
        perm = {"approve": "approve_transaction", "reject": "approve_transaction", "reverse": "approve_transaction"}.get(action)
        if not perm or not has_permission(g.user, perm):
            abort(403)
        try:
            if action == "approve":
                ledger_service.approve(txn, g.user, request.form.get("evidence"))
                flash("Approved and posted to the ledger.", "success")
            elif action == "reject":
                ledger_service.reject(txn, g.user, request.form.get("reason"))
                flash("Transaction rejected.", "info")
            elif action == "reverse":
                if txn.type in ("member_contribution", "distribution", "project_expense", "project_revenue"):
                    raise LedgerError("Reverse this from its own record (payment, distribution or project) "
                                      "so both stay in step.")
                rev = ledger_service.reverse(txn, g.user, request.form.get("reason"))
                flash(f"Reversal {rev.txn_number} posted.", "success")
            db.session.commit()
        except LedgerError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.transaction", txn_id=txn.id))
    return render_template("finance/transaction_detail.html", txn=txn, accounts=ledger_service.ACCOUNTS,
                           types=ledger_service.TXN_TYPES)


# ---------------------------------------------------------------- payments
@bp.route("/payments")
@permission_required("view_finance")
def payments():
    q = select(Payment).join(Member)
    args = request.args
    if args.get("status") in ("Pending", "Successful", "Failed", "Cancelled", "Reversed"):
        q = q.where(Payment.status == args["status"])
    if args.get("method") in ("mpesa", "sandbox", "bank", "cash"):
        q = q.where(Payment.method == args["method"])
    search = (args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.where(or_(Payment.reference.ilike(like), Member.member_number.ilike(like),
                        Payment.provider_receipt.ilike(like)))
    page = Page(q.order_by(Payment.created_at.desc()), args.get("page"), 30)
    return render_template("finance/payments.html", page=page, args=args)


@bp.route("/payments/manual", methods=["GET", "POST"])
@permission_required("record_payment")
def manual_payment():
    form = request.form
    if request.method == "POST":
        try:
            member = Member.query.filter_by(member_number=(form.get("member_number") or "").strip().upper()).first()
            if member is None:
                raise PaymentError("No member with that member ID.")
            contribution = None
            if form.get("contribution_id", type=int):
                contribution = Contribution.query.filter_by(id=form.get("contribution_id", type=int),
                                                            member_id=member.id).first()
            if contribution is None:
                open_row = _open_row(member)
                contribution = open_row["contribution"] if open_row else None
            if contribution is None:
                raise PaymentError("This member has no contribution with an amount remaining.")
            amount = parse_money(form.get("amount"))
            payment = payment_service.record_manual_payment(member, contribution, amount, form.get("method"),
                                                            form.get("reference"), g.user, form.get("notes"))
            db.session.commit()
            flash(f"Payment {payment.reference} recorded as Pending. A second officer must verify it.", "success")
            return redirect(url_for("finance.payment", payment_id=payment.id))
        except (PaymentError, MoneyError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("finance/manual_payment.html", form=form)


def _open_row(member):
    from services.contribution_service import member_summary
    return member_summary(member)["open"]


@bp.route("/payments/<int:payment_id>", methods=["GET", "POST"])
@permission_required("view_finance")
def payment(payment_id):
    p = db.get_or_404(Payment, payment_id)
    if request.method == "POST":
        action = request.form.get("action")
        needed = {"verify": "verify_payment", "reject": "verify_payment", "reverse": "reverse_payment",
                  "recheck": "verify_payment"}.get(action)
        if not needed or not has_permission(g.user, needed):
            abort(403)
        try:
            if action == "verify":
                payment_service.verify_manual_payment(p, g.user)
                flash("Payment verified and posted to the ledger.", "success")
            elif action == "reject":
                reason = (request.form.get("reason") or "").strip()
                if not reason:
                    raise PaymentError("Give a reason.")
                payment_service.reject_manual_payment(p, g.user, reason)
                flash("Payment rejected.", "info")
            elif action == "reverse":
                payment_service.reverse_payment(p, g.user, request.form.get("reason"))
                flash("Payment reversed. A correcting ledger entry was posted.", "success")
            elif action == "recheck":
                payment_service.verify_payment(p, force=True)
                flash(f"Provider status checked: {p.status}.", "info")
            db.session.commit()
        except (PaymentError, LedgerError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.payment", payment_id=p.id))
    return render_template("finance/payment_detail.html", p=p)


# ---------------------------------------------------------------- reconciliation
@bp.route("/reconciliation", methods=["GET", "POST"])
@permission_required("view_finance")
def reconciliation():
    if request.method == "POST":
        if not has_permission(g.user, "reconcile"):
            abort(403)
        account = request.form.get("account")
        statement_date = parse_date(request.form.get("statement_date"))
        try:
            if account not in ledger_service.CASH_ACCOUNTS or not statement_date:
                raise MoneyError("Choose the account and statement date.")
            statement = parse_money(request.form.get("statement_balance"), allow_zero=True)
        except MoneyError as exc:
            flash(str(exc), "error")
            return redirect(url_for("finance.reconciliation"))
        ledger_balance = ledger_service.account_balance(account, statement_date)
        rec = Reconciliation(account=account, statement_date=statement_date, statement_balance_cents=statement,
                             ledger_balance_cents=ledger_balance, difference_cents=statement - ledger_balance,
                             notes=(request.form.get("notes") or "").strip(), reconciled_by_id=g.user.id)
        db.session.add(rec)
        db.session.flush()
        set_setting("last_reconciled_at", utcnow().isoformat(timespec="minutes"), g.user)
        audit_service.log("finance.reconciled", "reconciliation", rec.id,
                          new={"account": account, "statement": statement, "ledger": ledger_balance})
        db.session.commit()
        if rec.difference_cents:
            flash(f"Reconciliation saved with a difference of {format_money(rec.difference_cents)}. "
                  "Investigate and post an approved adjustment if needed.", "error")
        else:
            flash("Reconciled: statement matches the ledger.", "success")
        return redirect(url_for("finance.reconciliation"))
    items = Reconciliation.query.order_by(Reconciliation.reconciled_at.desc()).limit(100).all()
    return render_template("finance/reconciliation.html", items=items, accounts=ledger_service.ACCOUNTS,
                           cash=ledger_service.CASH_ACCOUNTS, today=date.today(),
                           balances={a: ledger_service.account_balance(a) for a in ledger_service.CASH_ACCOUNTS})


# ---------------------------------------------------------------- verification queue
@bp.route("/verification", methods=["GET", "POST"])
@permission_required("view_finance")
def verification():
    if request.method == "POST":
        if not has_permission(g.user, "verify_project_finance"):
            abort(403)
        kind = request.form.get("kind")
        model = ProjectExpense if kind == "expense" else ProjectRevenue if kind == "revenue" else None
        if model is None:
            abort(400)
        item = db.get_or_404(model, request.form.get("item_id", type=int))
        try:
            if request.form.get("decision") == "verify":
                if kind == "expense":
                    project_service.verify_expense(item, g.user)
                else:
                    cash = request.form.get("cash_account")
                    if cash not in ledger_service.CASH_ACCOUNTS:
                        raise project_service.ProjectError("Choose where the revenue was received.")
                    project_service.verify_revenue(item, g.user, cash)
                flash("Verified and posted to the ledger.", "success")
            else:
                project_service.reject_item(item, g.user, request.form.get("reason"))
                flash("Rejected.", "info")
            db.session.commit()
        except (project_service.ProjectError, LedgerError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.verification"))
    expenses = ProjectExpense.query.filter_by(status="Reported").order_by(ProjectExpense.reported_at).all()
    revenues = ProjectRevenue.query.filter_by(status="Reported").order_by(ProjectRevenue.reported_at).all()
    pending_txns = Transaction.query.filter_by(approval_status="pending").order_by(Transaction.created_at).all()
    pending_payments = Payment.query.filter(Payment.status == "Pending", Payment.method.in_(["bank", "cash"])).all()
    return render_template("finance/verification.html", expenses=expenses, revenues=revenues,
                           pending_txns=pending_txns, pending_payments=pending_payments,
                           cash=ledger_service.CASH_ACCOUNTS, accounts=ledger_service.ACCOUNTS)
