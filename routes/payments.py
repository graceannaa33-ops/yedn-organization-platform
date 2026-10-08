"""Member payments using plain HTML forms - no JavaScript.

Flow:  form POST -> Flask -> provider (STK push) -> "waiting" page that
       refreshes itself with an HTML <meta refresh> -> server asks the
       provider for the real status -> receipt.
"""
import secrets

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from extensions import csrf, db
from helpers import MoneyError, parse_money
from models import Contribution, Payment, SandboxCharge
from permissions import role_required
from services import contribution_service, payment_service, report_service
from services.payment_service import PaymentError

bp = Blueprint("payments", __name__, url_prefix="/payments")


def _own_payment(reference):
    payment = Payment.query.filter_by(reference=reference).first()
    if payment is None or g.user.member is None or payment.member_id != g.user.member.id:
        abort(404)
    return payment


@bp.route("/new", methods=["GET", "POST"])
@role_required("member")
def new():
    member = g.user.member
    summary = contribution_service.member_summary(member)
    try:
        provider = payment_service.get_provider()
    except PaymentError as exc:
        return render_template("errors/payment_error.html", message=str(exc)), 503
    if request.method == "POST":
        contribution = db.session.get(Contribution, request.form.get("contribution_id", type=int) or 0)
        key = request.form.get("idempotency_key", "")
        if key not in session.get("payment_keys", []):
            # Either a replayed form or a key we never issued: look up instead of creating twice.
            existing = Payment.query.filter_by(idempotency_key=key).first() if key else None
            if existing and existing.member_id == member.id:
                return redirect(url_for("payments.status", reference=existing.reference))
            flash("This payment form has expired. Please fill it in again.", "error")
            return redirect(url_for("payments.new"))
        try:
            if contribution is None:
                raise PaymentError("Choose the contribution you are paying for.")
            amount = parse_money(request.form.get("amount"))
            payment = payment_service.start_payment(member, contribution, amount, request.form.get("phone"),
                                                    key, g.user)
        except (MoneyError, PaymentError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template("member/pay.html", summary=summary, provider=provider, member=member,
                                   idempotency_key=key, form=request.form), 400
        session["payment_keys"] = [k for k in session.get("payment_keys", []) if k != key]
        return redirect(url_for("payments.status", reference=payment.reference))
    key = secrets.token_urlsafe(24)
    session["payment_keys"] = (session.get("payment_keys", []) + [key])[-5:]
    return render_template("member/pay.html", summary=summary, provider=provider, member=member,
                           idempotency_key=key, form={})


@bp.route("/<reference>")
@role_required("member")
def status(reference):
    payment = _own_payment(reference)
    if payment.status == "Pending":
        try:
            payment_service.verify_payment(payment)
        except PaymentError as exc:
            current_app.logger.warning("Verification error for %s: %s", reference, exc)
    sandbox = None
    if payment.provider == "sandbox" and payment.status == "Pending":
        sandbox = SandboxCharge.query.filter_by(checkout_id=payment.provider_checkout_id).first()
    return render_template("member/payment_status.html", payment=payment, sandbox=sandbox)


@bp.route("/<reference>/check", methods=["POST"])
@role_required("member")
def check(reference):
    payment = _own_payment(reference)
    try:
        payment_service.verify_payment(payment, force=True)
    except PaymentError as exc:
        flash(str(exc), "error")
    return redirect(url_for("payments.status", reference=reference))


@bp.route("/<reference>/cancel", methods=["POST"])
@role_required("member")
def cancel(reference):
    payment = _own_payment(reference)
    payment_service.cancel_pending(payment, g.user)
    if payment.status == "Successful":
        flash("Good news - this payment had already gone through.", "success")
    return redirect(url_for("payments.status", reference=reference))


@bp.route("/<reference>/receipt")
@bp.route("/<reference>/receipt.<fmt>")
@role_required("member")
def receipt(reference, fmt=None):
    payment = _own_payment(reference)
    if payment.status not in ("Successful", "Reversed"):
        abort(404)
    if fmt == "pdf":
        report = _receipt_report(payment)
        return Response(report_service.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=receipt-{payment.reference}.pdf"})
    if fmt:
        abort(404)
    return render_template("member/receipt.html", payment=payment)


def _receipt_report(payment):
    from helpers import format_datetime
    from services.report_service import Money
    from services.report_service import org_contact_line
    from services.settings_service import org_full
    return {"type": "receipt", "title": "Payment receipt", "organization": org_full(), "contact": org_contact_line(),
            "period_label": payment.reference, "generated_at": format_datetime(payment.verified_at),
            "summary": [("Receipt for", payment.member.full_name), ("Member ID", payment.member.member_number),
                        ("Amount", Money(payment.amount_cents)), ("Method", payment.method.upper()),
                        ("Payment reference", payment.reference),
                        ("Provider reference", payment.provider_receipt or "-"),
                        ("Contribution", payment.contribution.period_label),
                        ("Status", payment.status), ("Verified", format_datetime(payment.verified_at))],
            "sections": [], "notes": ["This receipt was generated after the payment was verified by the server."]}


# ---------------------------------------------------------------- simulator
@bp.route("/sandbox/<checkout_id>", methods=["GET", "POST"])
@role_required("member")
def sandbox_phone(checkout_id):
    """A pretend phone for the built-in sandbox provider. Disabled in production
    unless ALLOW_SANDBOX_PAYMENTS=true (public test deployments)."""
    if not payment_service.sandbox_allowed():
        abort(404)
    charge = SandboxCharge.query.filter_by(checkout_id=checkout_id).first_or_404()
    payment = Payment.query.filter_by(provider_checkout_id=checkout_id).first_or_404()
    if payment.member_id != g.user.member.id:
        abort(404)
    if request.method == "POST":
        approve = request.form.get("decision") == "approve"
        if approve and request.form.get("pin") != "1234":
            flash("Wrong PIN. In the simulator the PIN is always 1234.", "error")
            return redirect(url_for("payments.sandbox_phone", checkout_id=checkout_id))
        try:
            payment_service.sandbox_complete(checkout_id, approve, g.user)
            db.session.commit()
        except PaymentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("payments.status", reference=payment.reference))
    return render_template("member/sandbox_phone.html", charge=charge, payment=payment)


# ---------------------------------------------------------------- provider callback
@bp.route("/callback/mpesa/<token>", methods=["POST"])
@csrf.exempt
def mpesa_callback(token):
    """Called by Safaricom (server to server). Protected by a secret URL token,
    and every success is re-confirmed with an independent status query."""
    expected = current_app.config["MPESA_CALLBACK_TOKEN"]
    if not expected or not secrets.compare_digest(token, expected):
        abort(404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"ResultCode": 1, "ResultDesc": "Rejected"}, 400
    try:
        payment_service.handle_mpesa_callback(data)
    except PaymentError as exc:
        current_app.logger.error("Callback processing error: %s", exc)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
