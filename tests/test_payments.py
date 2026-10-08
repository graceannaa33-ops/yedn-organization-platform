"""Contributions: partial and full payments, verification, idempotency, duplicates, reversal."""
import json

import pytest

from conftest import login


def _summary(member):
    from services import contribution_service
    return contribution_service.member_summary(member)


def test_partial_then_full_payment(app, factory):
    m = factory.member()
    factory.pay(m, 200000)
    s = _summary(m)
    assert (s["paid"], s["remaining"], s["status"]) == (200000, 800000, "Partially Paid")
    factory.pay(m, 300000)
    factory.pay(m, 500000)
    s = _summary(m)
    assert (s["paid"], s["remaining"], s["status"], s["percent"]) == (1000000, 0, "Fully Paid", 100)
    from models import Payment
    assert Payment.query.filter_by(member_id=m.id).count() == 3  # every payment kept


def test_payment_history_running_totals(app, factory):
    from services import contribution_service
    m = factory.member()
    factory.pay(m, 200000)
    factory.pay(m, 100000, approve=False)
    factory.pay(m, 300000)
    rows = list(reversed(contribution_service.payment_history(m)))
    assert [(r["payment"].status, r["total_paid"], r["remaining"]) for r in rows] == [
        ("Successful", 200000, 800000), ("Cancelled", 200000, 800000), ("Successful", 500000, 500000)]


def test_overpayment_and_minimum_rejected(app, factory):
    from services.payment_service import PaymentError, start_payment
    m = factory.member()
    factory.pay(m, 900000)
    with pytest.raises(PaymentError, match="more than what remains"):
        start_payment(m, m.contributions[0], 200000, m.phone, None, m.user)
    with pytest.raises(PaymentError, match="minimum"):
        start_payment(m, m.contributions[0], 500, m.phone, None, m.user)


def test_pending_payment_is_not_counted_and_browser_cannot_confirm(client, app, factory):
    from services import payment_service
    m = factory.member()
    login(client, m.email)
    p = payment_service.start_payment(m, m.contributions[0], 200000, m.phone, "k1", m.user)
    assert _summary(m)["paid"] == 0
    # A browser cannot mark a payment paid: status page and "check" only ask the provider
    client.get(f"/payments/{p.reference}?status=Successful")
    client.post(f"/payments/{p.reference}/check", data={"status": "Successful"})
    from extensions import db
    db.session.refresh(p)
    assert p.status == "Pending" and _summary(m)["paid"] == 0


def test_full_payment_flow_through_web_forms(client, app, factory):
    m = factory.member()
    login(client, m.email)
    page = client.get("/payments/new").get_data(as_text=True)
    key = page.split('name="idempotency_key" value="')[1].split('"')[0]
    form = {"contribution_id": m.contributions[0].id, "amount": "2,000", "phone": "0710000001", "idempotency_key": key}
    r = client.post("/payments/new", data=form)
    assert r.status_code == 302
    ref = r.headers["Location"].rsplit("/", 1)[-1]
    r2 = client.post("/payments/new", data=form)          # double-click: same payment again
    assert r2.headers["Location"].endswith(ref)
    from models import Payment, SandboxCharge
    assert Payment.query.count() == 1
    charge = SandboxCharge.query.one()
    assert client.post(f"/payments/sandbox/{charge.checkout_id}", data={"decision": "approve", "pin": "0000"}).status_code == 302
    assert Payment.query.one().status == "Pending"         # wrong PIN
    client.post(f"/payments/sandbox/{charge.checkout_id}", data={"decision": "approve", "pin": "1234"})
    p = Payment.query.one()
    assert p.status == "Successful" and p.provider_receipt.startswith("SBX")
    assert client.get(f"/payments/{ref}/receipt").status_code == 200
    assert client.get(f"/payments/{ref}/receipt.pdf").data[:4] == b"%PDF"
    assert b"KSh 2,000" in client.get("/member/payments").data


def test_successful_payment_updates_ledger_notification_and_audit(app, factory):
    from models import AuditLog, Notification, Transaction
    from services import ledger_service
    m = factory.member()
    p = factory.pay(m, 250000)
    txn = Transaction.query.filter_by(reference=p.reference).one()
    assert txn.type == "member_contribution" and txn.verification_status == "provider_verified"
    assert ledger_service.account_balance("CASH_MPESA") == 250000
    assert ledger_service.account_balance("MEMBER_CONTRIBUTIONS") == 250000
    note = Notification.query.filter_by(user_id=m.user_id, kind="payment").first()
    assert "Payment received: KSh 2,500" in note.body and "Remaining contribution: KSh 7,500" in note.body
    assert AuditLog.query.filter_by(action="payment.successful", record_id=p.reference).count() == 1


def test_confirmation_is_idempotent(app, factory):
    from services import payment_service
    from models import Transaction
    m = factory.member()
    p = factory.pay(m, 100000)
    payment_service.verify_payment(p, force=True)
    payment_service._mark_successful(p, p.provider_receipt, p.amount_cents)
    assert Transaction.query.filter_by(reference=p.reference).count() == 1
    assert _summary(m)["paid"] == 100000


def test_duplicate_provider_reference_is_not_recorded_twice(app, factory):
    from extensions import db
    from models import AnomalyFlag, SandboxCharge
    from services import payment_service
    m = factory.member()
    first = factory.pay(m, 100000)
    p2 = payment_service.start_payment(m, m.contributions[0], 100000, m.phone, None, m.user)
    charge = SandboxCharge.query.filter_by(checkout_id=p2.provider_checkout_id).one()
    charge.state, charge.receipt = "approved", "DUPLICATE"
    db.session.commit()
    # Provider (wrongly) reports the receipt number already used by the first payment
    payment_service._apply_result(p2, {"state": "success", "receipt": first.provider_receipt, "amount_cents": 100000})
    db.session.commit()
    assert p2.status == "Pending"
    assert _summary(m)["paid"] == 100000
    assert AnomalyFlag.query.filter_by(flag_type="duplicate_payment_reference").count() == 1


def test_amount_mismatch_held_for_review(app, factory):
    from models import AnomalyFlag
    from services import payment_service
    m = factory.member()
    p = payment_service.start_payment(m, m.contributions[0], 100000, m.phone, None, m.user)
    payment_service._apply_result(p, {"state": "success", "receipt": "X1", "amount_cents": 1000})
    assert p.status == "Pending" and AnomalyFlag.query.filter_by(flag_type="payment_amount_mismatch").count() == 1


class FakeMpesa:
    name = "mpesa"
    cash_account = "CASH_MPESA"
    result = {"state": "failed", "code": "1", "desc": "Insufficient balance"}

    def initiate(self, payment):
        return {"checkout_id": "ws_CO_TEST1", "message": "ok"}

    def query(self, checkout_id):
        return dict(self.result)


def _callback(code=0, receipt="QHX123ABC", amount=1000):
    return {"Body": {"stkCallback": {"MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_TEST1",
                                     "ResultCode": code, "ResultDesc": "x",
                                     "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amount},
                                                                   {"Name": "MpesaReceiptNumber", "Value": receipt}]}}}}


@pytest.fixture()
def mpesa(app, monkeypatch):
    from services import payment_service
    app.config["MPESA_CALLBACK_TOKEN"] = "secret-token"
    fake = FakeMpesa()
    monkeypatch.setattr(payment_service, "get_provider", lambda name=None: fake)
    return fake


def test_mpesa_callback_is_verified_server_side(client, app, factory, mpesa):
    from services import payment_service
    m = factory.member()
    p = payment_service.start_payment(m, m.contributions[0], 100000, m.phone, None, m.user)
    # Wrong token: rejected outright
    assert client.post("/payments/callback/mpesa/wrong", json=_callback()).status_code == 404
    # A callback claiming success is NOT trusted: the independent status query says it failed
    r = client.post("/payments/callback/mpesa/secret-token", json=_callback())
    assert r.status_code == 200 and json.loads(r.data)["ResultCode"] == 0
    from extensions import db
    db.session.refresh(p)
    assert p.status == "Failed" and _summary(m)["paid"] == 0


def test_mpesa_callback_success_records_receipt(client, app, factory, mpesa):
    from extensions import db
    from services import payment_service
    m = factory.member()
    p = payment_service.start_payment(m, m.contributions[0], 100000, m.phone, None, m.user)
    mpesa.result = {"state": "success", "code": "0", "desc": "ok"}
    client.post("/payments/callback/mpesa/secret-token", json=_callback())
    client.post("/payments/callback/mpesa/secret-token", json=_callback())   # Safaricom retries: no double count
    db.session.refresh(p)
    assert p.status == "Successful" and p.provider_receipt == "QHX123ABC"
    assert _summary(m)["paid"] == 100000


def test_manual_bank_payment_two_person_rule_and_duplicates(app, factory):
    from services import payment_service
    from services.payment_service import PaymentError
    m = factory.member()
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    p = payment_service.record_manual_payment(m, m.contributions[0], 500000, "bank", "slip-001", f1)
    with pytest.raises(PaymentError, match="Two-person"):
        payment_service.verify_manual_payment(p, f1)
    with pytest.raises(PaymentError, match="already been recorded"):
        payment_service.record_manual_payment(m, m.contributions[0], 100000, "bank", "SLIP-001", f1)
    payment_service.verify_manual_payment(p, f2)
    assert p.status == "Successful" and _summary(m)["paid"] == 500000


def test_payment_reversal_keeps_record_and_corrects_ledger(app, factory):
    from extensions import db
    from models import Payment, Transaction
    from services import ledger_service, payment_service
    m = factory.member()
    fo = factory.staff("finance_officer")
    p = factory.pay(m, 300000)
    payment_service.reverse_payment(p, fo, "Provider chargeback")
    assert db.session.get(Payment, p.id).status == "Reversed"
    assert _summary(m)["paid"] == 0 and _summary(m)["remaining"] == 1000000
    assert Transaction.query.filter_by(is_reversal=True).count() == 1
    assert ledger_service.account_balance("CASH_MPESA") == 0
    assert ledger_service.ledger_is_balanced()
