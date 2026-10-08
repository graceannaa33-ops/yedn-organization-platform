"""Database models (SQLAlchemy).

Money columns end in `_cents` and hold INTEGER cents. Never floats.
Financial records (payments, transactions, ledger entries) are never deleted:
corrections are made with reversal records. Guards at the bottom of this file
enforce that at the ORM level.
"""
from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint, event
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db
from helpers import utcnow

Money = BigInteger


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)
    label = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), default="")


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False, index=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("project_partners.id"), index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    language = db.Column(db.String(8), default="en")
    totp_secret = db.Column(db.String(64))
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    totp_last_counter = db.Column(db.BigInteger)
    last_login_at = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime, default=utcnow)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    role = db.relationship("Role")
    partner = db.relationship("ProjectPartner", back_populates="users")
    member = db.relationship("Member", back_populates="user", uselist=False, foreign_keys="Member.user_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.password_changed_at = utcnow()

    def check_password(self, password):
        return check_password_hash(self.password_hash, password or "")

    @property
    def role_name(self):
        return self.role.name if self.role else None

    @property
    def is_staff(self):
        from permissions import STAFF_ROLES
        return self.role_name in STAFF_ROLES


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), index=True)
    ip_address = db.Column(db.String(64), index=True)
    success = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)


class Member(db.Model):
    __tablename__ = "members"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    member_number = db.Column(db.String(20), unique=True, index=True)
    id_number = db.Column(db.String(40), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False, index=True)
    status = db.Column(db.String(20), default="active", nullable=False, index=True)  # pending|active|suspended|exited
    profile_photo = db.Column(db.String(80))
    is_demo = db.Column(db.Boolean, default=False, nullable=False)
    joined_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    admitted_at = db.Column(db.DateTime)
    admitted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    terms_accepted_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="member", foreign_keys=[user_id])
    contributions = db.relationship("Contribution", back_populates="member", order_by="Contribution.id")
    payments = db.relationship("Payment", back_populates="member", order_by="Payment.created_at")
    id_documents = db.relationship("IDDocument", back_populates="member", order_by="IDDocument.uploaded_at")

    @property
    def full_name(self):
        return self.user.full_name

    @property
    def email(self):
        return self.user.email

    @property
    def current_id_document(self):
        for doc in reversed(self.id_documents):
            if doc.is_current:
                return doc
        return None


class IDDocument(db.Model):
    __tablename__ = "id_documents"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    stored_name = db.Column(db.String(80), unique=True, nullable=False)
    original_name = db.Column(db.String(255))
    mime_type = db.Column(db.String(60), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    is_current = db.Column(db.Boolean, default=True, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    member = db.relationship("Member", back_populates="id_documents")


class Contribution(db.Model):
    """A contribution obligation for one member for one period (e.g. 'Membership capital 2026')."""
    __tablename__ = "contributions"
    __table_args__ = (UniqueConstraint("member_id", "period_label"),
                      CheckConstraint("required_cents > 0", name="ck_contribution_positive"))
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    period_label = db.Column(db.String(80), nullable=False)
    required_cents = db.Column(Money, nullable=False)
    due_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    member = db.relationship("Member", back_populates="contributions")
    payments = db.relationship("Payment", back_populates="contribution")


PAYMENT_STATUSES = ["Pending", "Successful", "Failed", "Cancelled", "Reversed"]


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_payment_positive"),)
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(32), unique=True, nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    contribution_id = db.Column(db.Integer, db.ForeignKey("contributions.id"), nullable=False, index=True)
    amount_cents = db.Column(Money, nullable=False)
    method = db.Column(db.String(20), nullable=False)          # mpesa | sandbox | bank | cash
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default="Pending", nullable=False, index=True)
    idempotency_key = db.Column(db.String(64), unique=True)
    provider = db.Column(db.String(20))
    provider_checkout_id = db.Column(db.String(100), unique=True)
    provider_receipt = db.Column(db.String(60), unique=True)  # M-PESA receipt / bank ref
    provider_result_code = db.Column(db.String(20))
    provider_result_desc = db.Column(db.String(255))
    provider_payload = db.Column(db.Text)
    notes = db.Column(db.String(255))
    initiated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    verified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    verified_at = db.Column(db.DateTime)
    failed_at = db.Column(db.DateTime)
    reversed_at = db.Column(db.DateTime)
    reversal_reason = db.Column(db.String(255))
    last_checked_at = db.Column(db.DateTime)

    member = db.relationship("Member", back_populates="payments")
    contribution = db.relationship("Contribution", back_populates="payments")
    initiated_by = db.relationship("User", foreign_keys=[initiated_by_id])
    verified_by = db.relationship("User", foreign_keys=[verified_by_id])
    transaction = db.relationship("Transaction", foreign_keys=[transaction_id])


class SandboxCharge(db.Model):
    """State held by the built-in *simulated* payment provider (development/demo only)."""
    __tablename__ = "sandbox_charges"
    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.String(64), unique=True, nullable=False)
    amount_cents = db.Column(Money, nullable=False)
    phone = db.Column(db.String(20))
    state = db.Column(db.String(20), default="pending", nullable=False)  # pending|approved|declined
    receipt = db.Column(db.String(30), unique=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    completed_at = db.Column(db.DateTime)


class Transaction(db.Model):
    """One financial event. Its effect on accounts is recorded as LedgerEntry lines
    (double entry) only once the transaction is approved ("posted")."""
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_txn_positive"),)
    id = db.Column(db.Integer, primary_key=True)
    txn_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    type = db.Column(db.String(40), nullable=False, index=True)
    category = db.Column(db.String(80), default="")
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(Money, nullable=False)
    direction = db.Column(db.String(10), default="in")   # in | out (only used by adjustment/other)
    cash_account = db.Column(db.String(30), default="CASH_MPESA")
    reference = db.Column(db.String(100), index=True)
    txn_date = db.Column(db.Date, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    approval_status = db.Column(db.String(20), default="pending", nullable=False, index=True)  # pending|approved|rejected
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.String(255))
    verification_status = db.Column(db.String(30), default="unverified", nullable=False)  # unverified|verified|provider_verified
    verified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime)
    evidence_note = db.Column(db.String(255))
    is_reversal = db.Column(db.Boolean, default=False, nullable=False)
    reverses_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    reversed_by_txn_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    posted_at = db.Column(db.DateTime)

    project = db.relationship("Project", foreign_keys=[project_id])
    member = db.relationship("Member", foreign_keys=[member_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    verified_by = db.relationship("User", foreign_keys=[verified_by_id])
    reverses = db.relationship("Transaction", foreign_keys=[reverses_id], remote_side=[id])
    lines = db.relationship("LedgerEntry", back_populates="transaction", order_by="LedgerEntry.id")


class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("debit_cents >= 0 AND credit_cents >= 0", name="ck_ledger_nonneg"),)
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False, index=True)
    account = db.Column(db.String(30), nullable=False, index=True)
    debit_cents = db.Column(Money, default=0, nullable=False)
    credit_cents = db.Column(Money, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    transaction = db.relationship("Transaction", back_populates="lines")


class Reconciliation(db.Model):
    __tablename__ = "reconciliations"
    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(30), nullable=False)
    statement_date = db.Column(db.Date, nullable=False)
    statement_balance_cents = db.Column(Money, nullable=False)
    ledger_balance_cents = db.Column(Money, nullable=False)
    difference_cents = db.Column(Money, nullable=False)
    notes = db.Column(db.Text)
    reconciled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reconciled_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    reconciled_by = db.relationship("User")


class ProjectPartner(db.Model):
    __tablename__ = "project_partners"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default="")
    contact_person = db.Column(db.String(150))
    contact_email = db.Column(db.String(254))
    contact_phone = db.Column(db.String(20))
    website = db.Column(db.String(255))
    is_public = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    users = db.relationship("User", back_populates="partner")
    projects = db.relationship("Project", back_populates="partner")


PROJECT_STATUSES = ["Proposed", "Under Review", "Due Diligence", "Approved", "Funding Pending",
                    "Active", "Delayed", "Completed", "Suspended", "Cancelled"]


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, index=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(80), default="")
    location = db.Column(db.String(150), default="")
    partner_id = db.Column(db.Integer, db.ForeignKey("project_partners.id"), index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    requested_cents = db.Column(Money, default=0, nullable=False)
    approved_cents = db.Column(Money, default=0, nullable=False)
    budget_cents = db.Column(Money, default=0, nullable=False)
    start_date = db.Column(db.Date)
    expected_end_date = db.Column(db.Date)
    actual_end_date = db.Column(db.Date)
    status = db.Column(db.String(30), default="Proposed", nullable=False, index=True)
    progress_percent = db.Column(db.Integer, default=0, nullable=False)
    risks = db.Column(db.Text, default="")
    review_notes = db.Column(db.Text)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    financial_review_notes = db.Column(db.Text)
    financial_reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    financial_reviewed_at = db.Column(db.DateTime)
    risk_assessment_notes = db.Column(db.Text)
    risk_level = db.Column(db.String(20))
    risk_assessed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    risk_assessed_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    is_public = db.Column(db.Boolean, default=True, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    partner = db.relationship("ProjectPartner", back_populates="projects")
    manager = db.relationship("User", foreign_keys=[manager_id])
    milestones = db.relationship("ProjectMilestone", back_populates="project", order_by="ProjectMilestone.due_date")
    updates = db.relationship("ProjectUpdate", back_populates="project", order_by="ProjectUpdate.created_at")
    expenses = db.relationship("ProjectExpense", back_populates="project", order_by="ProjectExpense.expense_date")
    revenues = db.relationship("ProjectRevenue", back_populates="project", order_by="ProjectRevenue.revenue_date")
    documents = db.relationship("Document", back_populates="project", order_by="Document.uploaded_at")


class ProjectMilestone(db.Model):
    __tablename__ = "project_milestones"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default="")
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="Planned", nullable=False)  # Planned|In Progress|Completed|Delayed
    completed_at = db.Column(db.DateTime)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=utcnow)

    project = db.relationship("Project", back_populates="milestones")


class ProjectUpdate(db.Model):
    """Timeline event for a project (proposal, approval, funding, progress, delay, ...)."""
    __tablename__ = "project_updates"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, default="")
    progress_percent = db.Column(db.Integer)
    is_public = db.Column(db.Boolean, default=True, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    project = db.relationship("Project", back_populates="updates")
    created_by = db.relationship("User")


class ProjectExpense(db.Model):
    __tablename__ = "project_expenses"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_expense_positive"),)
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(80), default="")
    amount_cents = db.Column(Money, nullable=False)
    expense_date = db.Column(db.Date, nullable=False)
    receipt_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))
    status = db.Column(db.String(20), default="Reported", nullable=False, index=True)  # Reported|Verified|Rejected
    reported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reported_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    verified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.String(255))
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))

    project = db.relationship("Project", back_populates="expenses")
    reported_by = db.relationship("User", foreign_keys=[reported_by_id])
    receipt = db.relationship("Document", foreign_keys=[receipt_document_id])


class ProjectRevenue(db.Model):
    __tablename__ = "project_revenues"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_revenue_positive"),)
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(120), default="")
    amount_cents = db.Column(Money, nullable=False)
    revenue_date = db.Column(db.Date, nullable=False)
    receipt_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))
    cash_account = db.Column(db.String(30), default="CASH_BANK")
    status = db.Column(db.String(20), default="Reported", nullable=False, index=True)
    reported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reported_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    verified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.String(255))
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))

    project = db.relationship("Project", back_populates="revenues")
    reported_by = db.relationship("User", foreign_keys=[reported_by_id])
    receipt = db.relationship("Document", foreign_keys=[receipt_document_id])


class Document(db.Model):
    """Privately stored file attached to a project, meeting or vote."""
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), index=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), index=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("votes.id"), index=True)
    kind = db.Column(db.String(30), nullable=False)  # report|receipt|photo|document|final_report|minutes
    title = db.Column(db.String(200), nullable=False)
    stored_name = db.Column(db.String(80), unique=True, nullable=False)
    original_name = db.Column(db.String(255))
    mime_type = db.Column(db.String(60), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    is_public = db.Column(db.Boolean, default=False, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    project = db.relationship("Project", back_populates="documents")
    uploaded_by = db.relationship("User")


DISTRIBUTION_STATUSES = ["Pending", "Approved", "Processing", "Paid", "Failed", "Reversed"]


class DistributionRun(db.Model):
    """One profit-distribution calculation for a project (a batch of Distribution rows)."""
    __tablename__ = "distribution_runs"
    id = db.Column(db.Integer, primary_key=True)
    run_number = db.Column(db.String(32), unique=True, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    record_date = db.Column(db.Date, nullable=False)
    net_distributable_cents = db.Column(Money, nullable=False)
    distributed_cents = db.Column(Money, default=0, nullable=False)
    remainder_cents = db.Column(Money, default=0, nullable=False)
    eligible_count = db.Column(db.Integer, default=0, nullable=False)
    rules_snapshot = db.Column(db.Text, nullable=False)  # JSON copy of the rules used
    status = db.Column(db.String(20), default="Pending", nullable=False)  # Pending|Approved|Processing|Completed|Cancelled
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)

    project = db.relationship("Project")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    distributions = db.relationship("Distribution", back_populates="run", order_by="Distribution.id")


class Distribution(db.Model):
    __tablename__ = "distributions"
    __table_args__ = (UniqueConstraint("run_id", "member_id"),
                      CheckConstraint("amount_cents >= 0", name="ck_distribution_nonneg"))
    id = db.Column(db.Integer, primary_key=True)
    dist_number = db.Column(db.String(32), unique=True, nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey("distribution_runs.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    amount_cents = db.Column(Money, nullable=False)
    basis_cents = db.Column(Money, default=0)  # member's verified contribution used for pro-rata
    status = db.Column(db.String(20), default="Pending", nullable=False, index=True)
    payment_method = db.Column(db.String(20))
    payment_reference = db.Column(db.String(100), unique=True)
    approver_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    payer_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    paid_at = db.Column(db.DateTime)
    failure_reason = db.Column(db.String(255))
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    run = db.relationship("DistributionRun", back_populates="distributions")
    member = db.relationship("Member")
    project = db.relationship("Project")
    approver = db.relationship("User", foreign_keys=[approver_id])
    payer = db.relationship("User", foreign_keys=[payer_id])


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(30), default="general")
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    sms_status = db.Column(db.String(20), default="not_sent")
    email_status = db.Column(db.String(20), default="not_sent")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    read_at = db.Column(db.DateTime)


class Announcement(db.Model):
    __tablename__ = "announcements"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_public = db.Column(db.Boolean, default=False, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)


class Report(db.Model):
    """A published report. Content is generated from the ledger on demand,
    so a published report always reflects recorded transactions."""
    __tablename__ = "reports"
    id = db.Column(db.Integer, primary_key=True)
    report_type = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    is_published = db.Column(db.Boolean, default=True, nullable=False)
    published_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    published_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class Meeting(db.Model):
    __tablename__ = "meetings"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    meeting_at = db.Column(db.DateTime, nullable=False, index=True)
    location = db.Column(db.String(200), default="")
    online_link = db.Column(db.String(500), default="")
    agenda = db.Column(db.Text, default="")
    minutes = db.Column(db.Text, default="")
    decisions = db.Column(db.Text, default="")
    follow_up_actions = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="Scheduled", nullable=False)  # Scheduled|Held|Cancelled
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    attendance = db.relationship("MeetingAttendance", back_populates="meeting")
    documents = db.relationship("Document", foreign_keys="Document.meeting_id")


class MeetingAttendance(db.Model):
    __tablename__ = "meeting_attendance"
    __table_args__ = (UniqueConstraint("meeting_id", "member_id"),)
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)  # Attending|Not attending|Attended|Absent
    recorded_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    meeting = db.relationship("Meeting", back_populates="attendance")
    member = db.relationship("Member")


class Vote(db.Model):
    """A proposal that eligible members vote on."""
    __tablename__ = "votes"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    budget_cents = db.Column(Money, default=0, nullable=False)
    risks = db.Column(db.Text, default="")
    eligibility = db.Column(db.String(20), default="fully_paid", nullable=False)  # fully_paid|active
    opens_at = db.Column(db.DateTime, nullable=False)
    closes_at = db.Column(db.DateTime, nullable=False)
    quorum_percent = db.Column(db.Integer, default=50, nullable=False)
    pass_percent = db.Column(db.Integer, default=50, nullable=False)  # % of yes among yes+no, must be exceeded
    status = db.Column(db.String(20), default="Open", nullable=False)  # Open|Closed|Cancelled
    result = db.Column(db.String(20))  # Passed|Rejected|No quorum
    yes_count = db.Column(db.Integer, default=0)
    no_count = db.Column(db.Integer, default=0)
    abstain_count = db.Column(db.Integer, default=0)
    eligible_count = db.Column(db.Integer, default=0)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    ballots = db.relationship("VoteBallot", back_populates="vote")
    documents = db.relationship("Document", foreign_keys="Document.vote_id")


class VoteBallot(db.Model):
    __tablename__ = "vote_ballots"
    __table_args__ = (UniqueConstraint("vote_id", "member_id"),)
    id = db.Column(db.Integer, primary_key=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("votes.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    choice = db.Column(db.String(10), nullable=False)  # yes|no|abstain
    cast_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    vote = db.relationship("Vote", back_populates="ballots")


CASE_STATUSES = ["Submitted", "Under Review", "Investigating", "Resolved", "Closed"]


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(32), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subject = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="Submitted", nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolution = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    responses = db.relationship("CaseResponse", foreign_keys="CaseResponse.ticket_id",
                                order_by="CaseResponse.created_at")


class Complaint(db.Model):
    """Complaints can be filed by anyone from the public Complaints page."""
    __tablename__ = "complaints"
    id = db.Column(db.Integer, primary_key=True)
    complaint_number = db.Column(db.String(32), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    phone = db.Column(db.String(20))
    subject = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="Submitted", nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolution = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    responses = db.relationship("CaseResponse", foreign_keys="CaseResponse.complaint_id",
                                order_by="CaseResponse.created_at")


class CaseResponse(db.Model):
    __tablename__ = "case_responses"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_tickets.id"), index=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaints.id"), index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    body = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    author = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    user_email = db.Column(db.String(254))
    role = db.Column(db.String(40))
    action = db.Column(db.String(80), nullable=False, index=True)
    record_type = db.Column(db.String(60), index=True)
    record_id = db.Column(db.String(60))
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    reason = db.Column(db.Text)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)


class AnomalyFlag(db.Model):
    """An alert for HUMAN review. A flag is not an accusation."""
    __tablename__ = "anomaly_flags"
    id = db.Column(db.Integer, primary_key=True)
    flag_type = db.Column(db.String(60), nullable=False, index=True)
    severity = db.Column(db.String(10), default="medium", nullable=False)
    description = db.Column(db.Text, nullable=False)
    record_type = db.Column(db.String(60))
    record_id = db.Column(db.String(60))
    status = db.Column(db.String(20), default="Open", nullable=False, index=True)  # Open|Reviewed|Dismissed
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)

    reviewed_by = db.relationship("User")


PROGRAM_STATUSES = ["Planned", "Open for applications", "Ongoing", "Closed"]


class Program(db.Model):
    """A YEDN program. Created and published by administrators - none are invented by the system."""
    __tablename__ = "programs"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    objectives = db.Column(db.Text, default="")
    eligibility = db.Column(db.Text, default="")
    application_info = db.Column(db.Text, default="")
    status = db.Column(db.String(30), default="Planned", nullable=False)
    is_published = db.Column(db.Boolean, default=False, nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, nullable=False)


OPPORTUNITY_TYPES = ["Scholarship", "Internship", "Job", "Grant", "Competition", "Training", "Other"]


class Opportunity(db.Model):
    """An external opportunity shared with members (scholarship, internship, job, grant, ...)."""
    __tablename__ = "opportunities"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    opportunity_type = db.Column(db.String(30), nullable=False)
    provider = db.Column(db.String(150), default="")
    description = db.Column(db.Text, nullable=False)
    eligibility = db.Column(db.Text, default="")
    how_to_apply = db.Column(db.Text, default="")
    link = db.Column(db.String(500), default="")
    deadline = db.Column(db.Date, index=True)
    members_only = db.Column(db.Boolean, default=False, nullable=False)
    is_published = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class StoredFile(db.Model):
    """File contents kept in the database when FILE_STORAGE=database.

    Hosts such as Render's free plan wipe the local disk on every restart, so
    uploaded ID documents, photos and project files are stored here instead.
    The other tables keep referring to files by their random `stored_name`.
    """
    __tablename__ = "stored_files"
    id = db.Column(db.Integer, primary_key=True)
    folder = db.Column(db.String(40), nullable=False, index=True)
    stored_name = db.Column(db.String(80), unique=True, nullable=False)
    mime_type = db.Column(db.String(60), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class OrganizationSetting(db.Model):
    __tablename__ = "organization_settings"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))


# ---------------------------------------------------------------------------
# Guards: financial records are append-only.
# ---------------------------------------------------------------------------
class ImmutableRecordError(Exception):
    pass


@event.listens_for(LedgerEntry, "before_update")
def _ledger_no_update(mapper, connection, target):
    raise ImmutableRecordError("Ledger entries cannot be changed. Post a reversal instead.")


def _no_delete(mapper, connection, target):
    raise ImmutableRecordError(f"{type(target).__name__} records cannot be deleted. Use a reversal.")


for _model in (LedgerEntry, Transaction, Payment, Distribution, AuditLog, ProjectExpense, ProjectRevenue):
    event.listen(_model, "before_delete", _no_delete)


@event.listens_for(Transaction, "before_update")
def _txn_amount_locked(mapper, connection, target):
    state = db.inspect(target)
    for field in ("amount_cents", "type", "cash_account", "direction"):
        hist = state.attrs[field].history
        if hist.has_changes() and hist.deleted and hist.deleted[0] is not None:
            raise ImmutableRecordError(f"Transaction {field} cannot be changed after creation.")


@event.listens_for(Payment, "before_update")
def _payment_amount_locked(mapper, connection, target):
    hist = db.inspect(target).attrs["amount_cents"].history
    if hist.has_changes() and hist.deleted and hist.deleted[0] is not None:
        raise ImmutableRecordError("Payment amounts cannot be changed.")


@event.listens_for(AuditLog, "before_update")
def _audit_no_update(mapper, connection, target):
    raise ImmutableRecordError("Audit log entries cannot be changed.")
