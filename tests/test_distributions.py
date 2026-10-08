"""Profit distribution: configurable rules, calculation, approval, payment, reversal."""
import datetime

import pytest

from conftest import login


@pytest.fixture()
def setup(app, factory):
    """Three fully paid members, one partial, a project with a verified net result of KSh 9,000."""
    from services import project_service as P
    team = {"admin": factory.staff("super_admin"), "pm": factory.staff("project_manager"),
            "f1": factory.staff("finance_officer"), "f2": factory.staff("finance_officer")}
    members = [factory.member() for _ in range(4)]
    for m in members[:3]:
        factory.pay(m, 1000000)
    factory.pay(members[3], 400000)
    p = factory.active_project(team["pm"], team["admin"], team["f1"], team["f2"], release=1000000)
    exp = P.report_expense(p, {"description": "Inputs", "amount_cents": 100000, "expense_date": datetime.date.today()}, team["pm"])
    P.verify_expense(exp, team["f1"])
    rev = P.report_revenue(p, {"description": "Sales", "amount_cents": 1000000, "revenue_date": datetime.date.today()}, team["pm"])
    P.verify_revenue(rev, team["f1"], "CASH_BANK")
    return team, members, p


def _rules(basis="fully_paid", method="equal", late="current_and_future", deadline=""):
    from services.settings_service import set_setting
    for k, v in {"dist_eligibility_basis": basis, "dist_allocation_method": method,
                 "dist_late_policy": late, "contribution_due_date": deadline}.items():
        set_setting(k, v)


def test_no_distribution_without_documented_rules(app, setup):
    from services import distribution_service as D
    team, members, p = setup
    assert D.rules_problems()
    with pytest.raises(D.DistributionError, match="not configured"):
        D.calculate(p, 300000, datetime.date.today())


def test_equal_split_fully_paid_only(app, setup):
    from services import distribution_service as D
    team, members, p = setup
    _rules()
    preview = D.calculate(p, 300001, datetime.date.today())
    assert [e["member"].id for e in preview["eligible"]] == [m.id for m in members[:3]]
    assert all(e["amount"] == 100000 for e in preview["eligible"])
    assert preview["remainder"] == 1          # leftover cent kept, never invented
    assert any("Not fully paid" in reason for _, reason in preview["excluded"])


def test_pro_rata_any_contribution(app, setup):
    from services import distribution_service as D
    team, members, p = setup
    _rules(basis="any_contribution", method="pro_rata")
    preview = D.calculate(p, 340000, datetime.date.today())
    amounts = {e["member"].id: e["amount"] for e in preview["eligible"]}
    assert amounts[members[0].id] == 100000 and amounts[members[3].id] == 40000


def test_cannot_distribute_more_than_net_result(app, setup):
    from services import distribution_service as D
    team, members, p = setup
    _rules()
    with pytest.raises(D.DistributionError, match="available"):
        D.calculate(p, 900001, datetime.date.today())


def test_late_completion_policies(app, setup):
    from extensions import db
    from models import Payment
    from services import distribution_service as D
    team, members, p = setup
    today = datetime.date.today()
    # member 0 completed long ago (on time); member 1 completed after the deadline AND after the project was funded
    late_payment = Payment.query.filter_by(member_id=members[1].id).one()
    from helpers import utcnow
    late_payment.verified_at = utcnow() + datetime.timedelta(minutes=1)   # just after the project was funded
    rd = today + datetime.timedelta(days=1)
    for m in (members[0], members[2]):
        pay = Payment.query.filter_by(member_id=m.id).one()
        pay.verified_at = datetime.datetime(2020, 1, 1)
    db.session.commit()
    deadline = (today - datetime.timedelta(days=30)).isoformat()

    _rules(late="none", deadline=deadline)
    ids = {e["member"].id for e in D.calculate(p, 300000, rd)["eligible"]}
    assert members[1].id not in ids and members[0].id in ids

    _rules(late="future_only", deadline=deadline)
    ids = {e["member"].id for e in D.calculate(p, 300000, rd)["eligible"]}
    assert members[1].id not in ids          # project was funded before they completed

    _rules(late="current_and_future", deadline=deadline)
    ids = {e["member"].id for e in D.calculate(p, 300000, rd)["eligible"]}
    assert members[1].id in ids

    _rules(late="future_only", deadline="")
    assert "deadline" in " ".join(D.rules_problems())


def test_distribution_approval_payment_and_reversal(client, app, setup):
    from extensions import db
    from models import Distribution, Notification
    from services import distribution_service as D, ledger_service as L
    team, members, p = setup
    _rules()
    run = D.create_run(p, 300000, datetime.date.today(), "First", team["f1"])
    with pytest.raises(D.DistributionError, match="Two-person"):
        D.approve_run(run, team["f1"])
    with pytest.raises(D.DistributionError, match="approved"):
        D.mark_paid(run.distributions[0], "mpesa", "REF1", "CASH_BANK", team["f1"])
    D.approve_run(run, team["f2"])
    d = run.distributions[0]
    D.mark_paid(d, "mpesa", "QWE123", "CASH_BANK", team["f1"])
    assert d.status == "Paid" and d.payer_id == team["f1"].id and d.approver_id == team["f2"].id
    assert L.account_balance("DISTRIBUTIONS") == 100000 and L.ledger_is_balanced()
    with pytest.raises(D.DistributionError, match="already used"):
        D.mark_paid(run.distributions[1], "mpesa", "QWE123", "CASH_BANK", team["f1"])
    assert Notification.query.filter_by(user_id=d.member.user_id, kind="distribution").count() == 1
    # the member sees the payment on their dashboard
    login(client, d.member.email)
    assert b"QWE123" in client.get("/member/distributions").data
    D.reverse(d, "Sent to wrong number", team["f2"])
    assert db.session.get(Distribution, d.id).status == "Reversed" and L.account_balance("DISTRIBUTIONS") == 0
    # what was reversed can be distributed again
    assert D.distributed_for_project(p.id) == 200000


def test_distribution_pages(client, app, setup):
    team, members, p = setup
    _rules()
    login(client, team["f1"].email)
    r = client.post("/admin/distributions/new", data={"project_id": p.id, "amount": "3000",
                                                       "record_date": datetime.date.today().isoformat()})
    assert b"Preview for" in r.data
    r = client.post("/admin/distributions/new", data={"project_id": p.id, "amount": "3000", "confirm": "yes",
                                                       "record_date": datetime.date.today().isoformat()})
    assert r.status_code == 302
    assert client.get(r.headers["Location"]).status_code == 200
