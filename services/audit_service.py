"""Audit logging. Every important administrative action calls log()."""
import json

from flask import g, has_request_context

from extensions import db
from helpers import client_ip
from models import AuditLog


def _text(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def log(action, record_type=None, record_id=None, old=None, new=None, reason=None, user=None):
    if user is None and has_request_context():
        user = g.get("user")
    entry = AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else "system",
        role=user.role_name if user else "system",
        action=action,
        record_type=record_type,
        record_id=str(record_id) if record_id is not None else None,
        old_value=_text(old),
        new_value=_text(new),
        reason=reason,
        ip_address=client_ip() if has_request_context() else None,
    )
    db.session.add(entry)
    return entry
