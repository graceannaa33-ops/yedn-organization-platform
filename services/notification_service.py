"""In-app notifications plus SMS and email, all sent from Python.

If SMS or email credentials are not configured, messages are written to the
server log instead of being sent, so development works without accounts.
"""
import logging
import smtplib
from email.message import EmailMessage

import requests
from flask import current_app

from extensions import db
from models import Notification
from services.settings_service import get_bool, get_setting, org_short

log = logging.getLogger("notifications")


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template_key, **values):
    values.setdefault("short", org_short())
    values.setdefault("org", get_setting("organization_name"))
    return get_setting(template_key).format_map(_SafeDict(values))


def send_sms(phone, message):
    cfg = current_app.config
    if not phone:
        return "no_phone"
    if cfg["SMS_PROVIDER"] != "africastalking" or not cfg["SMS_API_KEY"]:
        log.info("SMS (not sent - console mode) to %s: %s", phone, message)
        return "logged"
    username = cfg["SMS_USERNAME"] or "sandbox"
    host = "api.sandbox.africastalking.com" if username == "sandbox" else "api.africastalking.com"
    data = {"username": username, "to": "+" + phone.lstrip("+"), "message": message}
    if cfg["SMS_SENDER_ID"]:
        data["from"] = cfg["SMS_SENDER_ID"]
    try:
        resp = requests.post(f"https://{host}/version1/messaging", data=data, timeout=10,
                             headers={"apiKey": cfg["SMS_API_KEY"], "Accept": "application/json"})
        resp.raise_for_status()
        return "sent"
    except requests.RequestException as exc:
        log.warning("SMS failed to %s: %s", phone, exc)
        return "failed"


def send_email(to, subject, body):
    cfg = current_app.config
    if not to:
        return "no_email"
    if not cfg["MAIL_SERVER"]:
        log.info("EMAIL (not sent - no MAIL_SERVER) to %s: %s | %s", to, subject, body)
        return "logged"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"]
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=15) as smtp:
            if cfg["MAIL_USE_TLS"]:
                smtp.starttls()
            if cfg["MAIL_USERNAME"]:
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(msg)
        return "sent"
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email failed to %s: %s", to, exc)
        return "failed"


def _email_body(user, body):
    from services.settings_service import get_setting, org_full
    lines = [f"Dear {user.full_name},", "", body, "", "--", org_full(), get_setting("organization_tagline")]
    for key in ("contact_email", "contact_phone", "organization_website"):
        if get_setting(key):
            lines.append(get_setting(key))
    return "\n".join(lines)


def notify(user, title, body, kind="general", sms=False, email=False):
    note = Notification(user_id=user.id, title=title, body=body, kind=kind)
    if sms and get_bool("sms_enabled"):
        phone = user.member.phone if user.member else None
        note.sms_status = send_sms(phone, body)
    if email and get_bool("email_enabled"):
        note.email_status = send_email(user.email, f"{org_short()}: {title}", _email_body(user, body))
    db.session.add(note)
    return note
