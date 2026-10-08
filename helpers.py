"""Small shared helpers: money, time, request info, pagination.

MONEY RULE: every amount is stored as an INTEGER number of cents
(KSh 2,000.50 -> 200050). Floats are never used for money.
"""
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from flask import current_app, request


class MoneyError(ValueError):
    pass


def parse_money(text, allow_zero=False):
    """Convert user input like "2,000" or "2000.50" into integer cents."""
    if text is None:
        raise MoneyError("Enter an amount.")
    cleaned = str(text).strip().replace(",", "").replace(" ", "")
    for prefix in ("KSh", "KSH", "Ksh", "KES"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    if not re.fullmatch(r"\d{1,12}(\.\d{1,2})?", cleaned):
        raise MoneyError("Enter a valid amount, e.g. 2000 or 2000.50.")
    value = Decimal(cleaned)
    cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents == 0 and not allow_zero:
        raise MoneyError("The amount must be greater than zero.")
    return cents


def cents_to_decimal(cents):
    return (Decimal(int(cents or 0)) / Decimal(100)).quantize(Decimal("0.01"))


def format_money(cents, currency=None, show_currency=True):
    """Format integer cents for display: 200000 -> 'KSh 2,000'."""
    cents = int(cents or 0)
    negative = cents < 0
    cents = abs(cents)
    whole, frac = divmod(cents, 100)
    text = f"{whole:,}" if frac == 0 else f"{whole:,}.{frac:02d}"
    if negative:
        text = "-" + text
    if not show_currency:
        return text
    if currency is None:
        try:
            from services.settings_service import get_setting
            currency = get_setting("currency")
        except Exception:  # pragma: no cover - outside app context
            currency = "KSh"
    return f"{currency} {text}"


def utcnow():
    """Naive UTC datetime (stored consistently in SQLite and PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_time(dt):
    if dt is None:
        return None
    tz = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Africa/Nairobi"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def format_datetime(dt, fmt="%d %b %Y, %H:%M"):
    dt = local_time(dt)
    return dt.strftime(fmt) if dt else ""


def format_date(d, fmt="%d %b %Y"):
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = local_time(d)
    return d.strftime(fmt)


def client_ip():
    return (request.remote_addr or "unknown")[:64] if request else None


def new_reference(prefix):
    """Unique, unguessable human-readable reference e.g. PAY-7F3A9C21B4D0."""
    return f"{prefix}-{secrets.token_hex(6).upper()}"


def mask_id_number(value):
    if not value:
        return ""
    value = str(value)
    return "*" * max(len(value) - 3, 0) + value[-3:]


def mask_phone(value):
    if not value:
        return ""
    value = str(value)
    return value[:4] + "*" * max(len(value) - 7, 0) + value[-3:]


def normalize_phone(raw):
    """Normalise Kenyan numbers to 2547XXXXXXXX / 2541XXXXXXXX. Returns None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits
    if re.fullmatch(r"254[71]\d{8}", digits):
        return digits
    # allow other international numbers (8-15 digits) for non-Kenyan members
    if re.fullmatch(r"\d{8,15}", digits) and not digits.startswith("0"):
        return digits
    return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value):
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))


def password_problems(password):
    problems = []
    if len(password or "") < 10:
        problems.append("Password must be at least 10 characters long.")
    if not re.search(r"[A-Za-z]", password or "") or not re.search(r"\d", password or ""):
        problems.append("Password must contain letters and numbers.")
    return problems


class Page:
    """Very small server-side paginator for SQLAlchemy select() queries."""

    def __init__(self, query, page, per_page=25):
        from extensions import db
        from sqlalchemy import func, select

        self.page = max(int(page or 1), 1)
        self.per_page = per_page
        count_q = select(func.count()).select_from(query.order_by(None).subquery())
        self.total = db.session.execute(count_q).scalar_one()
        self.pages = max((self.total + per_page - 1) // per_page, 1)
        if self.page > self.pages:
            self.page = self.pages
        self.items = db.session.execute(
            query.limit(per_page).offset((self.page - 1) * per_page)
        ).scalars().all()

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def to_decimal_percent(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return d


def password_stamp(user):
    """Changes whenever the password changes, which logs out other sessions."""
    import hashlib
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]
