"""The financial ledger: double entry, calculated balances, approvals, reversals, immutability."""
import pytest


def test_balance_formula_matches_accounts(app, factory):
    from services import ledger_service as L
    m = factory.member()
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    factory.pay(m, 500000)
    t = L.create_transaction("opening_balance", 100000, "Opening cash", f1, cash_account="CASH_BANK")
    L.approve(t, f2)
    t = L.create_transaction("operating_expense", 20000, "Stationery", f1, cash_account="CASH_BANK")
    L.approve(t, f2)
    t = L.create_transaction("adjustment", 5000, "Bank interest correction", f1, cash_account="CASH_BANK", direction="in")
    L.approve(t, f2)
    t = L.create_transaction("reserve", 50000, "Emergency reserve", f1, cash_account="CASH_MPESA")
    L.approve(t, f2)
    pos = L.financial_position()
    assert pos["opening"] == 100000
    assert pos["total_income"] == 500000
    assert pos["total_outflows"] == 20000
    assert pos["adjustments"] == 5000
    assert pos["closing"] == 100000 + 500000 - 20000 + 5000
    s = L.summary()
    assert s["held"] == pos["closing"] and s["reserves"] == 50000 and s["available"] == pos["closing"] - 50000
    assert L.ledger_is_balanced()


def test_money_is_integer_cents():
    from helpers import format_money, parse_money
    assert parse_money("2,000.50") == 200050 and isinstance(parse_money("0.10"), int)
    assert parse_money("0.1") + parse_money("0.2") == parse_money("0.3")  # no float drift
    for bad in ("abc", "-5", "1.234", ""):
        with pytest.raises(ValueError):
            parse_money(bad)
    assert format_money(200000, "KSh") == "KSh 2,000" and format_money(200050, "KSh") == "KSh 2,000.50"


def test_two_person_approval_and_rejection(app, factory):
    from services import ledger_service as L
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    t = L.create_transaction("opening_balance", 1000, "Opening", f1)
    with pytest.raises(L.LedgerError, match="Two-person"):
        L.approve(t, f1)
    assert t.approval_status == "pending" and not t.lines and L.account_balance("CASH_BANK") == 0
    t2 = L.create_transaction("opening_balance", 2000, "Typo", f1)
    L.reject(t2, f2, "Wrong amount")
    assert t2.approval_status == "rejected" and not t2.lines


def test_insufficient_funds_blocked(app, factory):
    from services import ledger_service as L
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    t = L.create_transaction("operating_expense", 1000, "Rent", f1)
    with pytest.raises(L.LedgerError, match="Not enough money"):
        L.approve(t, f2)


def test_reversal_creates_correcting_entry(app, factory):
    from models import Transaction
    from services import ledger_service as L
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    t = L.create_transaction("opening_balance", 7000, "Opening", f1)
    L.approve(t, f2)
    rev = L.reverse(t, f2, "Entered twice")
    assert rev.is_reversal and rev.reverses_id == t.id and t.reversed_by_txn_id == rev.id
    assert L.account_balance("CASH_BANK") == 0 and Transaction.query.count() == 2
    with pytest.raises(L.LedgerError):
        L.reverse(t, f2, "again")
    with pytest.raises(L.LedgerError):
        L.reverse(rev, f2, "reverse the reversal")


def test_financial_records_cannot_be_deleted_or_edited(app, factory):
    from extensions import db
    from models import ImmutableRecordError, LedgerEntry, Payment
    from services import ledger_service as L
    m = factory.member()
    p = factory.pay(m, 100000)
    with pytest.raises(ImmutableRecordError):
        db.session.delete(db.session.get(Payment, p.id))
        db.session.flush()
    db.session.rollback()
    line = LedgerEntry.query.first()
    with pytest.raises(ImmutableRecordError):
        line.debit_cents = 1
        db.session.flush()
    db.session.rollback()
    txn = p.transaction
    with pytest.raises(ImmutableRecordError):
        txn.amount_cents = 1
        db.session.flush()
    db.session.rollback()
    assert L.ledger_is_balanced()


def test_ledger_pages_and_manual_transaction_flow(client, app, factory):
    from conftest import login
    f1, f2 = factory.staff("finance_officer"), factory.staff("finance_officer")
    login(client, f1.email)
    r = client.post("/admin/finance/transactions/new", data={
        "type": "opening_balance", "amount": "1,500", "txn_date": "2026-01-01", "cash_account": "CASH_BANK",
        "direction": "in", "description": "Opening cash"})
    assert r.status_code == 302
    url = r.headers["Location"]
    assert b"Two-person" in client.post(url, data={"action": "approve"}, follow_redirects=True).data
    client.post("/logout")
    login(client, f2.email)
    client.post(url, data={"action": "approve", "evidence": "Statement line 1"})
    page = client.get("/admin/finance/").get_data(as_text=True)
    assert "KSh 1,500" in page
    assert client.get("/admin/finance/ledger?type=opening_balance").status_code == 200
    # contribution-type entries cannot be typed in by hand
    r = client.post("/admin/finance/transactions/new", data={"type": "member_contribution", "amount": "100",
                                                            "description": "x", "cash_account": "CASH_BANK"})
    assert b"own workflow" in r.data


def test_reconciliation(client, app, factory):
    from conftest import login
    from models import Reconciliation
    m = factory.member()
    factory.pay(m, 100000)
    fo = factory.staff("finance_officer")
    login(client, fo.email)
    client.post("/admin/finance/reconciliation", data={"account": "CASH_MPESA", "statement_date": "2099-01-01",
                                                       "statement_balance": "900"})
    rec = Reconciliation.query.one()
    assert rec.ledger_balance_cents == 100000 and rec.difference_cents == -10000
    assert b"Last reconciled" in client.get("/transparency").data
