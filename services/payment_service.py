"""Payments: request, provider response, server verification, callbacks,
duplicate protection, receipts, ledger update, notification and audit.

The golden rule: a payment becomes "Successful" ONLY after this server has
asked the payment provider directly and the provider confirmed it. Nothing the
member's browser sends can mark a payment as paid.

Providers
---------
SandboxProvider  Built-in simulator for development and demos. It keeps its
                 own records (SandboxCharge) and shows a "simulated phone"
                 page. It refuses to run when APP_ENV=production unless
                 ALLOW_SANDBOX_PAYMENTS=true (public test sites).
MpesaProvider    Safaricom Daraja M-PESA Express (STK Push). Entirely
                 server-to-server: no JavaScript SDK is needed.
"""
import base64
import json
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError

from extensions import db
from helpers import format_money, new_reference, normalize_phone, utcnow
from models import Payment, SandboxCharge
from services import anomaly_service, audit_service, contribution_service, ledger_service, notification_service
from services.settings_service import get_bool, get_int, get_setting

log = logging.getLogger("payments")


class PaymentError(Exception):
    pass


# ======================================================================
# Providers
# ======================================================================
class SandboxProvider:
    name = "sandbox"
    label = "Sandbox (simulated M-PESA - test only)"
    cash_account = "CASH_MPESA"

    def initiate(self, payment):
        charge = SandboxCharge(checkout_id="SBX-" + uuid.uuid4().hex[:16].upper(),
                               amount_cents=payment.amount_cents, phone=payment.phone)
        db.session.add(charge)
        db.session.flush()
        return {"checkout_id": charge.checkout_id,
                "message": "Simulated payment request created. Open the simulated phone to approve it."}

    def query(self, checkout_id):
        charge = SandboxCharge.query.filter_by(checkout_id=checkout_id).first()
        if charge is None:
            return {"state": "failed", "code": "404", "desc": "Unknown checkout request"}
        if charge.state == "approved":
            return {"state": "success", "code": "0", "desc": "The service request is processed successfully.",
                    "receipt": charge.receipt, "amount_cents": charge.amount_cents}
        if charge.state == "declined":
            return {"state": "cancelled", "code": "1032", "desc": "Request cancelled by user"}
        return {"state": "pending", "code": "", "desc": "Waiting for the customer"}


class MpesaProvider:
    name = "mpesa"
    label = "M-PESA"
    cash_account = "CASH_MPESA"

    def __init__(self, config):
        self.cfg = config
        self.base = ("https://api.safaricom.co.ke" if config["MPESA_ENV"] == "production"
                     else "https://sandbox.safaricom.co.ke")
        missing = [k for k in ("PAYMENT_PUBLIC_KEY", "PAYMENT_SECRET_KEY", "MPESA_SHORTCODE",
                               "MPESA_PASSKEY", "MPESA_CALLBACK_TOKEN") if not config.get(k)]
        if missing:
            raise PaymentError("M-PESA is not configured. Missing: " + ", ".join(missing))

    def _token(self):
        resp = requests.get(f"{self.base}/oauth/v1/generate?grant_type=client_credentials",
                            auth=(self.cfg["PAYMENT_PUBLIC_KEY"], self.cfg["PAYMENT_SECRET_KEY"]), timeout=15)
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _password(self):
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        raw = f"{self.cfg['MPESA_SHORTCODE']}{self.cfg['MPESA_PASSKEY']}{ts}"
        return base64.b64encode(raw.encode()).decode(), ts

    def initiate(self, payment):
        password, ts = self._password()
        callback = f"{self.cfg['APP_BASE_URL'].rstrip('/')}/payments/callback/mpesa/{self.cfg['MPESA_CALLBACK_TOKEN']}"
        body = {
            "BusinessShortCode": self.cfg["MPESA_SHORTCODE"], "Password": password, "Timestamp": ts,
            "TransactionType": "CustomerPayBillOnline", "Amount": payment.amount_cents // 100,
            "PartyA": payment.phone, "PartyB": self.cfg["MPESA_SHORTCODE"], "PhoneNumber": payment.phone,
            "CallBackURL": callback, "AccountReference": (payment.member.member_number or "MEMBER")[:12],
            "TransactionDesc": "Contribution",
        }
        try:
            resp = requests.post(f"{self.base}/mpesa/stkpush/v1/processrequest", json=body, timeout=30,
                                 headers={"Authorization": f"Bearer {self._token()}"})
            data = resp.json()
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("M-PESA STK push error: %s", exc)
            raise PaymentError("Could not reach M-PESA. Please try again in a few minutes.")
        if str(data.get("ResponseCode")) != "0" or not data.get("CheckoutRequestID"):
            log.warning("M-PESA STK push rejected: %s", data)
            raise PaymentError(data.get("errorMessage") or data.get("ResponseDescription")
                               or "M-PESA rejected the request.")
        return {"checkout_id": data["CheckoutRequestID"],
                "message": data.get("CustomerMessage") or "Check your phone and enter your M-PESA PIN."}

    def query(self, checkout_id):
        password, ts = self._password()
        body = {"BusinessShortCode": self.cfg["MPESA_SHORTCODE"], "Password": password,
                "Timestamp": ts, "CheckoutRequestID": checkout_id}
        try:
            resp = requests.post(f"{self.base}/mpesa/stkpushquery/v1/query", json=body, timeout=30,
                                 headers={"Authorization": f"Bearer {self._token()}"})
            data = resp.json()
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("M-PESA query error: %s", exc)
            return {"state": "pending", "code": "", "desc": "Could not reach M-PESA to confirm yet."}
        if "errorCode" in data:  # e.g. 500.001.1001 "The transaction is being processed"
            return {"state": "pending", "code": data.get("errorCode"), "desc": data.get("errorMessage", "")}
        code = str(data.get("ResultCode", ""))
        desc = data.get("ResultDesc", "")
        if code == "0":
            return {"state": "success", "code": code, "desc": desc}
        if code == "1032":
            return {"state": "cancelled", "code": code, "desc": desc}
        if code == "":
            return {"state": "pending", "code": code, "desc": desc}
        return {"state": "failed", "code": code, "desc": desc}

    @staticmethod
    def parse_callback(data):
        cb = data["Body"]["stkCallback"]
        items = {i.get("Name"): i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
        amount_cents = None
        if items.get("Amount") is not None:
            try:
                amount_cents = int(Decimal(str(items["Amount"])) * 100)
            except InvalidOperation:
                amount_cents = None
        return {"checkout_id": cb.get("CheckoutRequestID"), "code": str(cb.get("ResultCode")),
                "desc": cb.get("ResultDesc", ""), "receipt": items.get("MpesaReceiptNumber"),
                "amount_cents": amount_cents}


def sandbox_allowed():
    """The simulator runs in development, or on a public TEST deployment where
    ALLOW_SANDBOX_PAYMENTS=true was set on purpose. No real money ever moves."""
    cfg = current_app.config
    return cfg["APP_ENV"] != "production" or cfg.get("ALLOW_SANDBOX_PAYMENTS", False)


def provider_name():
    return (get_setting("payment_provider") or current_app.config["PAYMENT_PROVIDER"]).strip().lower()


def get_provider(name=None):
    name = name or provider_name()
    if name == "mpesa":
        return MpesaProvider(current_app.config)
    if name == "sandbox":
        if not sandbox_allowed():
            raise PaymentError("The sandbox payment simulator is disabled in production. Configure M-PESA.")
        return SandboxProvider()
    raise PaymentError(f"Unknown payment provider '{name}'.")


# ======================================================================
# Member-initiated payments
# ======================================================================
def validate_amount(contribution, amount_cents, provider=None):
    minimum = get_int("min_payment_cents", 1000)
    if amount_cents < minimum:
        raise PaymentError(f"The minimum payment is {format_money(minimum)}.")
    paid = contribution_service.paid_for_contribution(contribution.id)
    recent_pending = db.session.query(db.func.coalesce(db.func.sum(Payment.amount_cents), 0)).filter(
        Payment.contribution_id == contribution.id, Payment.status == "Pending",
        Payment.created_at >= utcnow() - timedelta(minutes=10)).scalar()
    remaining = contribution.required_cents - paid - int(recent_pending or 0)
    if remaining <= 0:
        if recent_pending:
            raise PaymentError("You already have a payment waiting for confirmation. Please wait for it to finish.")
        raise PaymentError("This contribution is already fully paid.")
    if amount_cents > remaining:
        raise PaymentError(f"The amount is more than what remains ({format_money(remaining)}).")
    if provider is not None and provider.name == "mpesa" and amount_cents % 100:
        raise PaymentError("M-PESA payments must be in whole shillings.")


def start_payment(member, contribution, amount_cents, phone, idempotency_key, user):
    """Create a Pending payment and ask the provider to collect it."""
    if idempotency_key:
        existing = Payment.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.member_id != member.id:
                raise PaymentError("Invalid payment request.")
            return existing  # double-click / resubmitted form: same payment, not a new one
    if contribution.member_id != member.id:
        raise PaymentError("Invalid contribution.")
    if member.status not in ("active",):
        raise PaymentError("Your membership is not active yet, so payments are not open.")
    phone = normalize_phone(phone)
    if not phone:
        raise PaymentError("Enter a valid phone number, e.g. 0712345678.")
    provider = get_provider()
    validate_amount(contribution, amount_cents, provider)

    payment = Payment(reference=new_reference("PAY"), member_id=member.id, contribution_id=contribution.id,
                      amount_cents=amount_cents, method=provider.name, phone=phone, status="Pending",
                      idempotency_key=idempotency_key, provider=provider.name, initiated_by_id=user.id)
    db.session.add(payment)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = Payment.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            return existing
        raise PaymentError("Could not create the payment. Please try again.")
    audit_service.log("payment.initiated", "payment", payment.reference,
                      new={"amount_cents": amount_cents, "provider": provider.name}, user=user)
    try:
        result = provider.initiate(payment)
    except PaymentError as exc:
        _mark_failed(payment, "INIT", str(exc), "Failed")
        db.session.commit()
        raise
    payment.provider_checkout_id = result["checkout_id"]
    payment.provider_result_desc = result.get("message")
    db.session.commit()
    return payment


def verify_payment(payment, force=False):
    """Ask the provider for the true status of a pending payment (server to server)."""
    if payment.status != "Pending" or not payment.provider_checkout_id:
        return payment
    now = utcnow()
    if not force and payment.last_checked_at and now - payment.last_checked_at < timedelta(seconds=3):
        return payment
    payment.last_checked_at = now
    provider = get_provider(payment.provider)
    result = provider.query(payment.provider_checkout_id)
    _apply_result(payment, result)
    db.session.commit()
    return payment


def _callback_data(payment):
    try:
        return json.loads(payment.provider_payload or "{}")
    except ValueError:
        return {}


def _apply_result(payment, result):
    state = result.get("state")
    if state == "success":
        receipt = result.get("receipt") or _callback_data(payment).get("receipt")
        amount = result.get("amount_cents") or _callback_data(payment).get("amount_cents") or payment.amount_cents
        _mark_successful(payment, receipt, amount)
    elif state in ("failed", "cancelled"):
        _mark_failed(payment, result.get("code"), result.get("desc"),
                     "Cancelled" if state == "cancelled" else "Failed")
    else:
        payment.provider_result_desc = (result.get("desc") or payment.provider_result_desc or "")[:255]


def handle_mpesa_callback(data):
    """Safaricom calls this URL. We store what it says, then CONFIRM with an
    independent status query before recording anything as paid."""
    try:
        parsed = MpesaProvider.parse_callback(data)
    except (KeyError, TypeError):
        log.warning("Malformed M-PESA callback: %s", data)
        return None
    payment = Payment.query.filter_by(provider_checkout_id=parsed["checkout_id"]).with_for_update().first()
    if payment is None:
        log.warning("Callback for unknown checkout id %s", parsed["checkout_id"])
        return None
    payment.provider_payload = json.dumps({"receipt": parsed["receipt"], "amount_cents": parsed["amount_cents"],
                                           "code": parsed["code"], "desc": parsed["desc"]})
    if payment.status == "Successful" and parsed["receipt"] and not payment.provider_receipt:
        _set_receipt(payment, parsed["receipt"])
        db.session.commit()
        return payment
    db.session.commit()
    return verify_payment(payment, force=True)


def sandbox_complete(checkout_id, approve, user):
    """Simulated phone: the 'customer' approves or declines, then the simulator
    'calls back' and the server verifies - the same path real payments take."""
    if not sandbox_allowed():
        raise PaymentError("Simulator disabled in production.")
    charge = SandboxCharge.query.filter_by(checkout_id=checkout_id).first()
    if charge is None or charge.state != "pending":
        raise PaymentError("This simulated request is no longer waiting.")
    charge.state = "approved" if approve else "declined"
    charge.completed_at = utcnow()
    if approve:
        charge.receipt = "SBX" + uuid.uuid4().hex[:7].upper()
    db.session.flush()
    payment = Payment.query.filter_by(provider_checkout_id=checkout_id).first()
    if payment is not None:
        verify_payment(payment, force=True)
    return payment


def _set_receipt(payment, receipt):
    clash = Payment.query.filter(Payment.provider_receipt == receipt, Payment.id != payment.id).first()
    if clash:
        anomaly_service.duplicate_payment_reference(receipt, payment)
        return False
    payment.provider_receipt = receipt
    return True


def _mark_successful(payment, receipt, amount_cents, verified_by=None, cash_account=None):
    db.session.flush()
    db.session.refresh(payment, with_for_update=True)  # row lock on PostgreSQL
    if payment.status == "Successful":
        return payment  # idempotent: already recorded once
    if payment.status != "Pending":
        anomaly_service.flag("late_payment_confirmation",
                             f"Provider confirmed payment {payment.reference} but it is already {payment.status}. "
                             "Review whether money was received.", "payment", payment.id, "high")
        return payment
    if amount_cents != payment.amount_cents:
        anomaly_service.flag("payment_amount_mismatch",
                             f"Payment {payment.reference}: requested {format_money(payment.amount_cents)} but "
                             f"provider reported {format_money(amount_cents)}. Not recorded automatically.",
                             "payment", payment.id, "high")
        payment.provider_result_desc = "Amount mismatch - held for review"
        return payment
    if receipt and not _set_receipt(payment, receipt):
        payment.provider_result_desc = "Duplicate provider reference - held for review"
        return payment

    provider_verified = verified_by is None
    payment.status = "Successful"
    payment.verified_at = utcnow()
    payment.verified_by_id = verified_by.id if verified_by else None
    payment.provider_result_code = "0"
    member = payment.member
    if cash_account is None:
        cash_account = "CASH_BANK" if payment.method in ("bank", "cash") else "CASH_MPESA"
    txn = ledger_service.create_transaction(
        "member_contribution", payment.amount_cents,
        f"Contribution {payment.reference} from {member.member_number}",
        verified_by or payment.initiated_by, cash_account=cash_account, member=member,
        reference=payment.reference, txn_date=date.today(), auto_approve=True,
        verification_status="provider_verified" if provider_verified else "verified",
        approver=verified_by)
    payment.transaction_id = txn.id
    db.session.flush()
    audit_service.log("payment.successful", "payment", payment.reference,
                      old={"status": "Pending"}, new={"status": "Successful", "receipt": payment.provider_receipt},
                      user=verified_by)
    summary = contribution_service.member_summary(member)
    message = notification_service.render(
        "tpl_payment_received", amount=format_money(payment.amount_cents), total=format_money(summary["paid"]),
        remaining=format_money(summary["remaining"]), reference=payment.reference)
    notification_service.notify(member.user, "Payment received", message, "payment", sms=True, email=True)
    return payment


def _mark_failed(payment, code, desc, status="Failed"):
    if payment.status != "Pending":
        return payment
    payment.status = status
    payment.failed_at = utcnow()
    payment.provider_result_code = (code or "")[:20]
    payment.provider_result_desc = (desc or "")[:255]
    audit_service.log(f"payment.{status.lower()}", "payment", payment.reference,
                      old={"status": "Pending"}, new={"status": status, "reason": desc})
    message = notification_service.render("tpl_payment_failed", reference=payment.reference,
                                          amount=format_money(payment.amount_cents), reason=desc or status)
    notification_service.notify(payment.member.user, f"Payment {status.lower()}", message, "payment")
    anomaly_service.check_repeated_failed_payments(payment.member)
    return payment


def cancel_pending(payment, user):
    """Member gives up waiting. Provider is asked first so a real success is never lost."""
    verify_payment(payment, force=True)
    if payment.status == "Pending":
        _mark_failed(payment, "USER", "Cancelled by member before confirmation", "Cancelled")
        db.session.commit()
    return payment


# ======================================================================
# Manual (bank / cash) payments - maker/checker
# ======================================================================
def record_manual_payment(member, contribution, amount_cents, method, reference, user, notes=""):
    if method not in ("bank", "cash"):
        raise PaymentError("Method must be bank or cash.")
    reference = (reference or "").strip().upper()
    if not reference:
        raise PaymentError("Enter the bank slip / receipt reference.")
    validate_amount(contribution, amount_cents)
    receipt_key = f"{method.upper()}:{reference}"
    if Payment.query.filter_by(provider_receipt=receipt_key).first():
        anomaly_service.flag("duplicate_payment_reference",
                             f"Someone tried to record {receipt_key} again for {member.member_number}.",
                             "member", member.id, "high")
        db.session.commit()
        raise PaymentError("That reference has already been recorded. Duplicate payments are not allowed.")
    payment = Payment(reference=new_reference("PAY"), member_id=member.id, contribution_id=contribution.id,
                      amount_cents=amount_cents, method=method, phone=member.phone, status="Pending",
                      provider=method, provider_receipt=receipt_key, notes=(notes or "")[:255],
                      initiated_by_id=user.id)
    db.session.add(payment)
    db.session.flush()
    audit_service.log("payment.recorded_manual", "payment", payment.reference,
                      new={"amount_cents": amount_cents, "method": method, "receipt": receipt_key}, user=user)
    return payment


def verify_manual_payment(payment, user):
    if payment.method not in ("bank", "cash") or payment.status != "Pending":
        raise PaymentError("Only pending bank/cash payments can be verified here.")
    if get_bool("require_two_person_approval") and payment.initiated_by_id == user.id:
        raise PaymentError("Two-person rule: a different officer must verify this payment.")
    _mark_successful(payment, None, payment.amount_cents, verified_by=user, cash_account="CASH_BANK")
    audit_service.log("payment.verified_manual", "payment", payment.reference, user=user)
    anomaly_service.check_staff_activity(user)
    return payment


def reject_manual_payment(payment, user, reason):
    if payment.method not in ("bank", "cash") or payment.status != "Pending":
        raise PaymentError("Only pending bank/cash payments can be rejected.")
    _mark_failed(payment, "REJECTED", f"Rejected by finance: {reason}", "Failed")
    audit_service.log("payment.rejected_manual", "payment", payment.reference, reason=reason, user=user)


def reverse_payment(payment, user, reason):
    if payment.status != "Successful":
        raise PaymentError("Only successful payments can be reversed.")
    if not (reason or "").strip():
        raise PaymentError("Give a reason for the reversal.")
    if payment.transaction is not None:
        ledger_service.reverse(payment.transaction, user, f"Payment {payment.reference} reversed: {reason}")
    payment.status = "Reversed"
    payment.reversed_at = utcnow()
    payment.reversal_reason = reason.strip()[:255]
    audit_service.log("payment.reversed", "payment", payment.reference,
                      old={"status": "Successful"}, new={"status": "Reversed"}, reason=reason, user=user)
    message = notification_service.render("tpl_payment_reversed", reference=payment.reference,
                                          amount=format_money(payment.amount_cents), reason=reason)
    notification_service.notify(payment.member.user, "Payment reversed", message, "payment", sms=True, email=True)
    anomaly_service.check_staff_activity(user)
    return payment
