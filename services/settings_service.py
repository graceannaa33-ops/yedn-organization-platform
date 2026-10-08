"""Organisation settings stored in the database (non-secret values only).

Secrets (API keys, passwords) live ONLY in environment variables.
"""
from flask import current_app, g

from default_content import DEFAULTS_TEXT, ORG_NAME, ORG_SHORT, SUPPORTING, TAGLINE
from extensions import db
from helpers import utcnow
from models import OrganizationSetting

DEFAULTS = {
    "organization_name": None,          # falls back to ORGANIZATION_NAME env var, then the YEDN name
    "organization_short_name": ORG_SHORT,
    "organization_tagline": TAGLINE,
    "organization_supporting_message": SUPPORTING,
    "organization_website": "",
    "organization_registration": "",    # official registration details - leave empty until verified
    "university_relationship": "",      # only fill in once a formal relationship is confirmed
    "leadership_text": "",              # official leadership information (no names are invented)
    "founding_team_text": "",           # official founding team information
    "contact_email": None,
    "contact_phone": None,
    "contact_address": "",
    "logo": "",
    "currency": "KSh",
    "contribution_amount_cents": "1000000",       # KSh 10,000
    "contribution_period": "one-off",             # one-off | annual | quarterly | monthly
    "contribution_period_label": "Membership contribution",
    "contribution_due_date": "",                  # YYYY-MM-DD deadline (used by late-completion rule)
    "min_payment_cents": "1000",                  # KSh 10
    "require_membership_approval": "0",
    "require_two_person_approval": "1",
    "require_admin_2fa": "0",
    "payment_provider": "",                       # empty = use PAYMENT_PROVIDER env var
    "sms_enabled": "1",
    "email_enabled": "1",
    "tpl_payment_received": ("{short}: Payment received: {amount}. Total contribution: {total}. "
                             "Remaining contribution: {remaining}. Ref {reference}."),
    "tpl_payment_failed": "{short}: Your payment {reference} of {amount} was not completed: {reason}.",
    "tpl_distribution_paid": "{short}: Distribution paid: {amount} from project {project}. Ref {reference}.",
    "tpl_payment_reversed": "{short}: Payment {reference} of {amount} was reversed. Reason: {reason}.",
    # Distribution rules - deliberately EMPTY until an administrator documents them.
    "dist_eligibility_basis": "",      # fully_paid | any_contribution
    "dist_allocation_method": "",      # equal | pro_rata
    "dist_late_policy": "",            # current_and_future | future_only | none
    "dist_rules_notes": "",
    "project_categories": ("Agriculture, Technology, Digital businesses, Education, Manufacturing, Retail, Services, "
                           "Youth enterprises, Community development, Innovation, Other approved sectors"),
    "maintenance_mode": "0",
    "maintenance_message": "We are carrying out maintenance. Please try again soon.",
    "last_reconciled_at": "",
    "is_demo_data": "0",
}
DEFAULTS.update(DEFAULTS_TEXT)


def _cache():
    if "settings_cache" not in g:
        g.settings_cache = {row.key: row.value for row in OrganizationSetting.query.all()}
    return g.settings_cache


def get_setting(key):
    value = _cache().get(key)
    if value is not None:
        return value
    default = DEFAULTS.get(key, "")
    if default is None:
        env_map = {
            "organization_name": "ORGANIZATION_NAME",
            "contact_email": "ORGANIZATION_EMAIL",
            "contact_phone": "ORGANIZATION_PHONE",
        }
        value = current_app.config.get(env_map.get(key, ""), "") or ""
        if key == "organization_name" and not value:
            value = ORG_NAME
        return value
    return default


def get_int(key, default=0):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return default


def get_bool(key):
    return str(get_setting(key)).strip() in ("1", "true", "yes", "on")


def set_setting(key, value, user=None):
    value = "" if value is None else str(value)
    row = db.session.get(OrganizationSetting, key)
    if row is None:
        row = OrganizationSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    row.updated_at = utcnow()
    row.updated_by_id = user.id if user else None
    _cache()[key] = value


def org_short():
    return get_setting("organization_short_name") or get_setting("organization_name")


def org_full():
    """'Youth Enterprise & Development Network (YEDN)' - used on formal pages and documents."""
    name, short = get_setting("organization_name"), get_setting("organization_short_name")
    return f"{name} ({short})" if short and short != name else name


def fill(text):
    """Replace {org} and {short} placeholders in editable text."""
    return (text or "").replace("{org}", get_setting("organization_name")).replace("{short}", org_short())


def org_text(key):
    """Setting text with {org} / {short} replaced by the organisation's names."""
    return fill(get_setting(key))


def project_categories():
    return [c.strip() for c in get_setting("project_categories").split(",") if c.strip()]
