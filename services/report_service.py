"""Reports and statements, generated in Python as HTML tables, CSV and PDF.

Every report is calculated from recorded transactions at the moment it is
viewed, so reports always agree with the ledger.
"""
import csv
import io
import json
from calendar import monthrange
from datetime import date

from sqlalchemy import select

from extensions import db
from helpers import cents_to_decimal, format_date, format_datetime, format_money, utcnow
from models import (AuditLog, DistributionRun, Member, Payment, Project, ProjectExpense, Reconciliation,
                    Transaction)
from services import contribution_service, ledger_service
from services.settings_service import get_setting, org_full


def org_contact_line():
    """Contact details exactly as configured in Organization Settings (never hard-coded)."""
    parts = [get_setting(k) for k in ("contact_address", "contact_phone", "contact_email", "organization_website",
                                      "organization_registration")]
    return " · ".join(p for p in parts if p)


class Money:
    """A money cell: prints formatted, exports to CSV as a plain decimal number."""

    def __init__(self, cents):
        self.cents = int(cents or 0)

    def __str__(self):
        return format_money(self.cents)

    def __html__(self):
        from markupsafe import escape
        return str(escape(str(self)))


REPORT_TYPES = {
    "monthly_financial": {"label": "Monthly financial report", "period": "month", "public": True},
    "quarterly_financial": {"label": "Quarterly financial report", "period": "quarter", "public": True},
    "annual_financial": {"label": "Annual financial report", "period": "year", "public": True},
    "contributions": {"label": "Contributions report", "period": "range", "public": False},
    "expenses": {"label": "Expense report", "period": "range", "public": True},
    "investment": {"label": "Investment report", "period": "range", "public": True},
    "project_performance": {"label": "Project performance report", "period": "range", "public": True},
    "profit_loss": {"label": "Profit / loss report", "period": "range", "public": True},
    "distribution": {"label": "Distribution report", "period": "range", "public": True},
    "reconciliation": {"label": "Reconciliation report", "period": "range", "public": False},
    "audit": {"label": "Audit report", "period": "range", "public": False, "permission": "view_audit"},
}


class ReportError(Exception):
    pass


def resolve_period(report_type, args):
    """Turn request arguments into (start, end). Defaults to the current period."""
    today = date.today()
    kind = REPORT_TYPES[report_type]["period"]
    try:
        year = int(args.get("year") or today.year)
        if kind == "month":
            month = int(args.get("month") or today.month)
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        if kind == "quarter":
            q = int(args.get("quarter") or (today.month - 1) // 3 + 1)
            if q not in (1, 2, 3, 4):
                raise ValueError
            first = 3 * (q - 1) + 1
            return date(year, first, 1), date(year, first + 2, monthrange(year, first + 2)[1])
        if kind == "year":
            return date(year, 1, 1), date(year, 12, 31)
        start = date.fromisoformat(args.get("start")) if args.get("start") else date(today.year, 1, 1)
        end = date.fromisoformat(args.get("end")) if args.get("end") else today
    except (ValueError, TypeError):
        raise ReportError("Invalid period.")
    if end < start:
        raise ReportError("The end date is before the start date.")
    return start, end


def build(report_type, start, end, public=False):
    if report_type not in REPORT_TYPES:
        raise ReportError("Unknown report type.")
    builder = {
        "monthly_financial": _financial, "quarterly_financial": _financial, "annual_financial": _financial,
        "contributions": _contributions, "expenses": _expenses, "investment": _investment,
        "project_performance": _project_performance, "profit_loss": _profit_loss,
        "distribution": _distribution, "reconciliation": _reconciliation, "audit": _audit,
    }[report_type]
    report = {"type": report_type, "title": REPORT_TYPES[report_type]["label"],
              "organization": org_full(), "contact": org_contact_line(),
              "period_label": f"{start:%d %b %Y} to {end:%d %b %Y}", "start": start, "end": end,
              "generated_at": format_datetime(utcnow()), "summary": [], "sections": [], "notes": [],
              "public": public}
    builder(report, start, end)
    if get_setting("is_demo_data") == "1":
        report["notes"].append("DEMO DATA: figures are generated test data, not real organisation records.")
    return report


def _section(report, title, columns, rows):
    report["sections"].append({"title": title, "columns": columns, "rows": rows})


# ---------------------------------------------------------------- builders
def _posted_txns(start, end, types=None):
    q = select(Transaction).where(Transaction.approval_status == "approved",
                                  Transaction.txn_date >= start, Transaction.txn_date <= end)
    if types:
        q = q.where(Transaction.type.in_(types))
    return db.session.execute(q.order_by(Transaction.txn_date, Transaction.id)).scalars().all()


def _signed(txn):
    return -txn.amount_cents if txn.is_reversal else txn.amount_cents


def _financial(report, start, end):
    pos = ledger_service.financial_position(start, end)
    report["summary"] = [("Opening position", Money(pos["opening"]))]
    report["summary"] += [(f"+ {label}", Money(v)) for label, v in pos["income"]]
    report["summary"] += [("Total verified income", Money(pos["total_income"]))]
    report["summary"] += [(f"- {label}", Money(v)) for label, v in pos["outflows"]]
    report["summary"] += [("Total verified outflows", Money(pos["total_outflows"])),
                          ("+ Approved adjustments", Money(pos["adjustments"])),
                          ("Calculated closing position", Money(pos["closing"]))]
    balances = ledger_service.all_balances(end)
    _section(report, "Account balances at end of period", ["Account", "Type", "Balance"],
             [[ledger_service.ACCOUNTS[a][0], ledger_service.ACCOUNTS[a][1].title(), Money(v)]
              for a, v in balances.items() if v])
    def describe(t):
        # Public reports never identify individual members.
        if report["public"] and t.member_id:
            return ledger_service.label_for(t.type, t.direction)
        return t.description
    rows = [[format_date(t.txn_date), t.txn_number, ledger_service.label_for(t.type, t.direction)
             + (" (reversal)" if t.is_reversal else ""), describe(t), t.project.name if t.project else "",
             Money(_signed(t))] for t in _posted_txns(start, end)]
    _section(report, "Posted transactions in the period", ["Date", "Transaction", "Type", "Description",
                                                           "Project", "Amount"], rows)
    report["notes"].append("Opening + verified income - verified outflows + approved adjustments = "
                           "calculated closing position (money held in bank, mobile money and reserve).")


def _contributions(report, start, end):
    cutoff = contribution_service.end_of_day(end)
    rows, total_req, total_paid = [], 0, 0
    for m in Member.query.order_by(Member.member_number).all():
        req = contribution_service.required_as_of(m.id, cutoff)
        paid = contribution_service.paid_as_of(m.id, cutoff)
        total_req, total_paid = total_req + req, total_paid + paid
        rows.append([m.member_number, m.full_name, Money(req), Money(paid), Money(max(req - paid, 0)),
                     contribution_service.status_label(req, paid)])
    report["summary"] = [("Members", len(rows)), ("Total required", Money(total_req)),
                         ("Total verified contributions", Money(total_paid)),
                         ("Total remaining", Money(max(total_req - total_paid, 0)))]
    _section(report, "Member contribution status at end of period",
             ["Member ID", "Name", "Required", "Verified paid", "Remaining", "Status"], rows)
    payments = Payment.query.filter(Payment.created_at >= contribution_service.start_of_day(start),
                                    Payment.created_at <= cutoff).order_by(Payment.created_at).all()
    _section(report, "Payments in the period", ["Date", "Reference", "Member", "Method", "Amount", "Status"],
             [[format_datetime(p.created_at), p.reference, p.member.member_number, p.method,
               Money(p.amount_cents), p.status] for p in payments])


def _expenses(report, start, end):
    txns = _posted_txns(start, end, ["project_expense", "operating_expense", "bank_fee", "provider_fee"])
    by_type = {}
    for t in txns:
        by_type[t.type] = by_type.get(t.type, 0) + _signed(t)
    report["summary"] = [(ledger_service.TXN_TYPES[k], Money(v)) for k, v in by_type.items()]
    report["summary"].append(("Total expenses", Money(sum(by_type.values()))))
    _section(report, "Verified expenses", ["Date", "Type", "Description", "Project", "Category", "Amount"],
             [[format_date(t.txn_date), ledger_service.TXN_TYPES[t.type], t.description,
               t.project.name if t.project else "", t.category, Money(_signed(t))] for t in txns])
    pending = ProjectExpense.query.filter_by(status="Reported").count()
    if pending:
        report["notes"].append(f"{pending} reported project expense(s) are awaiting verification and are not included.")


def _project_rows():
    from services.project_service import financials
    return [(p, financials(p)) for p in Project.query.order_by(Project.name).all()]


def _investment(report, start, end):
    rows, total_rel = [], 0
    for p, fin in _project_rows():
        in_period = ledger_service.total_by_type("project_funding", start, end, project_id=p.id)
        total_rel += in_period
        rows.append([p.name, p.status, Money(fin["approved"]), Money(in_period), Money(fin["released"]),
                     Money(fin["spent"]), Money(fin["remaining_funds"]), Money(fin["unreleased"])])
    report["summary"] = [("Funds released in period", Money(total_rel)),
                         ("Total released to date", Money(ledger_service.total_by_type("project_funding")))]
    _section(report, "Investment by project", ["Project", "Status", "Approved", "Released in period",
                                               "Released to date", "Spent", "Unspent", "Not yet released"], rows)


def _project_performance(report, start, end):
    rows = []
    for p, fin in _project_rows():
        rows.append([p.name, p.status, f"{p.progress_percent}%", Money(fin["budget"]), Money(fin["released"]),
                     Money(fin["revenue"]), Money(fin["expenses"]), Money(fin["net"]), Money(fin["budget_variance"])])
    _section(report, "Project performance (to date)", ["Project", "Status", "Progress", "Budget", "Released",
                                                       "Revenue", "Expenses", "Net result", "Budget variance"], rows)
    report["notes"].append("Net result = revenue - expenses. Revenue is not profit. Past results do not "
                           "guarantee future results.")


def _profit_loss(report, start, end):
    rev_rows, exp_rows = [], []
    total_rev = total_exp = 0
    for p in Project.query.order_by(Project.name).all():
        r = ledger_service.total_by_type("project_revenue", start, end, project_id=p.id)
        e = ledger_service.total_by_type("project_expense", start, end, project_id=p.id)
        if r or e:
            rev_rows.append([p.name, Money(r), Money(e), Money(r - e)])
            total_rev += r
            total_exp += e
    other_in = sum(_signed(t) for t in _posted_txns(start, end, ["other"]) if t.direction == "in")
    other_out = sum(_signed(t) for t in _posted_txns(start, end, ["other"]) if t.direction == "out")
    operating = ledger_service.total_by_type("operating_expense", start, end)
    fees = ledger_service.total_by_type("bank_fee", start, end) + ledger_service.total_by_type("provider_fee", start, end)
    net = total_rev + other_in - total_exp - operating - fees - other_out
    report["summary"] = [("Project revenue", Money(total_rev)), ("Other income", Money(other_in)),
                         ("Project expenses", Money(total_exp)), ("Operating expenses", Money(operating)),
                         ("Bank and provider fees", Money(fees)), ("Other expenses", Money(other_out)),
                         ("Net profit / (loss)", Money(net))]
    _section(report, "Result by project", ["Project", "Revenue", "Expenses", "Net result"], rev_rows)
    exp_rows = [["Operating expenses", Money(operating)], ["Bank and provider fees", Money(fees)],
                ["Other expenses", Money(other_out)]]
    _section(report, "Organisation-level costs", ["Item", "Amount"], exp_rows)
    report["notes"].append("Member contributions and project funding are capital movements, not profit or loss.")


def _distribution(report, start, end):
    runs = DistributionRun.query.filter(DistributionRun.record_date >= start,
                                        DistributionRun.record_date <= end).order_by(DistributionRun.record_date).all()
    rows, total_paid = [], 0
    for r in runs:
        paid = sum(d.amount_cents for d in r.distributions if d.status == "Paid")
        pending = sum(d.amount_cents for d in r.distributions if d.status in ("Pending", "Approved", "Processing"))
        total_paid += paid
        rules = json.loads(r.rules_snapshot or "{}")
        rows.append([r.run_number, r.project.name, format_date(r.record_date), r.eligible_count,
                     f"{rules.get('basis', '')} / {rules.get('method', '')}", Money(r.net_distributable_cents),
                     Money(paid), Money(pending), Money(r.remainder_cents), r.status])
    report["summary"] = [("Distribution runs", len(runs)), ("Total paid", Money(total_paid))]
    _section(report, "Distribution runs", ["Run", "Project", "Record date", "Eligible members", "Rules",
                                           "Distributable", "Paid", "Pending", "Remainder kept", "Status"], rows)


def _reconciliation(report, start, end):
    recs = Reconciliation.query.filter(Reconciliation.statement_date >= start,
                                       Reconciliation.statement_date <= end).order_by(Reconciliation.statement_date).all()
    report["summary"] = [(ledger_service.ACCOUNTS[a][0] + " (ledger, now)", Money(ledger_service.account_balance(a)))
                         for a in ledger_service.CASH_ACCOUNTS]
    report["summary"].append(("Ledger debits equal credits", "Yes" if ledger_service.ledger_is_balanced() else "NO"))
    _section(report, "Reconciliations", ["Statement date", "Account", "Statement balance", "Ledger balance",
                                         "Difference", "By", "Notes"],
             [[format_date(r.statement_date), ledger_service.ACCOUNTS[r.account][0], Money(r.statement_balance_cents),
               Money(r.ledger_balance_cents), Money(r.difference_cents),
               r.reconciled_by.full_name if r.reconciled_by else "", r.notes or ""] for r in recs])


def _audit(report, start, end):
    logs = AuditLog.query.filter(AuditLog.created_at >= contribution_service.start_of_day(start),
                                 AuditLog.created_at <= contribution_service.end_of_day(end)) \
        .order_by(AuditLog.created_at).all()
    report["summary"] = [("Entries", len(logs))]
    _section(report, "Audit log", ["Time", "User", "Role", "Action", "Record", "Reason", "IP"],
             [[format_datetime(a.created_at), a.user_email, a.role, a.action,
               f"{a.record_type or ''} {a.record_id or ''}".strip(), a.reason or "", a.ip_address or ""] for a in logs])


# ---------------------------------------------------------------- statement
def member_statement(member):
    summary_data = contribution_service.member_summary(member)
    events = []
    for c in member.contributions:
        events.append((c.created_at, "Contribution required", c.period_label, Money(c.required_cents), "", 0, c.required_cents))
    for p in member.payments:
        if p.status in ("Successful", "Reversed") and p.verified_at:
            events.append((p.verified_at, f"Payment ({p.method})", p.reference, Money(p.amount_cents), "Successful", p.amount_cents, 0))
        if p.status == "Reversed":
            events.append((p.reversed_at, "Payment reversal", p.reference, Money(-p.amount_cents), "Reversed", -p.amount_cents, 0))
        if p.status in ("Pending", "Failed", "Cancelled"):
            events.append((p.created_at, f"Payment ({p.method})", p.reference, Money(p.amount_cents), p.status, 0, 0))
    from models import Distribution
    dists = Distribution.query.filter_by(member_id=member.id).all()
    for d in dists:
        if d.status in ("Paid", "Reversed") and d.paid_at:
            events.append((d.paid_at, f"Distribution - {d.project.name}", d.payment_reference or d.dist_number,
                           Money(d.amount_cents), "Paid", 0, 0))
    refunds = Transaction.query.filter_by(member_id=member.id, type="refund", approval_status="approved").all()
    for t in refunds:
        events.append((t.posted_at, "Refund" + (" reversal" if t.is_reversal else ""), t.txn_number,
                       Money(-_signed(t)), "Adjustment", 0, 0))
    events.sort(key=lambda e: e[0] or utcnow())
    rows, running_paid, running_required = [], 0, 0
    for when, kind, ref, amount, status, paid_delta, req_delta in events:
        running_paid += paid_delta
        running_required += req_delta
        rows.append([format_datetime(when), kind, ref, amount, status, Money(running_paid),
                     Money(max(running_required - running_paid, 0))])
    dist_total = sum(d.amount_cents for d in dists if d.status == "Paid")
    refund_total = sum(_signed(t) for t in refunds)
    report = {"type": "statement", "title": "Member statement", "organization": org_full(),
              "contact": org_contact_line(),
              "period_label": f"All activity to {date.today():%d %b %Y}", "generated_at": format_datetime(utcnow()),
              "summary": [("Member", member.full_name), ("Member ID", member.member_number),
                          ("Opening contribution (required)", Money(summary_data["required"])),
                          ("Verified payments", Money(summary_data["paid"])),
                          ("Remaining contribution", Money(summary_data["remaining"])),
                          ("Status", summary_data["status"]),
                          ("Distributions received", Money(dist_total)),
                          ("Refunds / adjustments", Money(-refund_total)),
                          ("Closing contribution balance", Money(summary_data["paid"] - refund_total))],
              "sections": [], "notes": ["Only verified payments count towards your contribution."]}
    _section(report, "Activity", ["Date", "Description", "Reference", "Amount", "Status",
                                  "Running total paid", "Remaining"], rows)
    return report


# ---------------------------------------------------------------- export
def _plain(value):
    if isinstance(value, Money):
        return str(cents_to_decimal(value.cents))
    return "" if value is None else str(value)


def to_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report["organization"]])
    if report.get("contact"):
        w.writerow([report["contact"]])
    w.writerow([report["title"], report["period_label"]])
    w.writerow(["Generated", report["generated_at"]])
    w.writerow([])
    for label, value in report["summary"]:
        w.writerow([label, _plain(value)])
    for section in report["sections"]:
        w.writerow([])
        w.writerow([section["title"]])
        w.writerow(section["columns"])
        for row in section["rows"]:
            w.writerow([_safe_csv(_plain(c)) for c in row])
    for note in report["notes"]:
        w.writerow([])
        w.writerow([note])
    return buf.getvalue()


def _safe_csv(text):
    """Stop spreadsheet formula injection (=, +, -, @ at the start of text)."""
    if text and text[0] in "=+@\t\r" and not _is_number(text):
        return "'" + text
    if text and text[0] == "-" and not _is_number(text):
        return "'" + text
    return text


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def to_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    wide = any(len(s["columns"]) > 6 for s in report["sections"])
    pagesize = landscape(A4) if wide else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=report["title"])
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7.5, leading=9)
    head = styles["BodyText"].clone("head", fontSize=7.5, leading=9, textColor=colors.white, fontName="Helvetica-Bold")
    story = [Paragraph(escape(report["organization"]), styles["Title"]),
             Paragraph(escape(get_setting("organization_tagline")), styles["Italic"]),
             Paragraph(escape(report.get("contact") or ""), styles["BodyText"]),
             Paragraph(escape(f"{report['title']} - {report['period_label']}"), styles["Heading2"]),
             Paragraph(escape(f"Generated {report['generated_at']}"), styles["Italic"]), Spacer(1, 6)]
    if report["summary"]:
        t = Table([[Paragraph(escape(str(k)), small), Paragraph(escape(_display(v)), small)]
                   for k, v in report["summary"]], colWidths=[80 * mm, 60 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                               ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef3ef"))]))
        story += [t, Spacer(1, 10)]
    width = pagesize[0] - 28 * mm
    for s in report["sections"]:
        story.append(Paragraph(escape(s["title"]), styles["Heading3"]))
        data = [[Paragraph(escape(c), head) for c in s["columns"]]]
        data += [[Paragraph(escape(_display(c)), small) for c in row] for row in s["rows"]] or \
                [[Paragraph("No records.", small)] + [""] * (len(s["columns"]) - 1)]
        t = Table(data, colWidths=[width / len(s["columns"])] * len(s["columns"]), repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4d3a")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [t, Spacer(1, 10)]
    for note in report["notes"]:
        story.append(Paragraph(escape(note), small))
    doc.build(story)
    return buf.getvalue()


def _display(value):
    return "" if value is None else str(value)
