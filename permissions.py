"""Role-based permissions, checked on the SERVER for every protected route.

Roles
-----
super_admin      Full control.
finance_officer  Payments, ledger, reconciliation, distributions.
project_manager  Projects, milestones, project reports.
auditor          READ-ONLY access to finance, reports and audit logs.
partner          Only projects assigned to their partner organisation.
member           Their own account only.
"""
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "finance_officer": "Finance Officer",
    "project_manager": "Project Manager",
    "auditor": "Auditor",
    "partner": "Project Partner",
    "member": "Member",
}

STAFF_ROLES = {"super_admin", "finance_officer", "project_manager", "auditor"}

SA, FO, PM, AU = "super_admin", "finance_officer", "project_manager", "auditor"

# permission -> roles allowed. Auditor never appears on a write permission.
PERMISSIONS = {
    "view_admin": {SA, FO, PM, AU},
    "view_members": {SA, FO, AU},
    "manage_members": {SA},
    "view_id_documents": {SA},
    "view_finance": {SA, FO, AU},
    "record_payment": {SA, FO},
    "verify_payment": {SA, FO},
    "reverse_payment": {SA, FO},
    "create_transaction": {SA, FO},
    "approve_transaction": {SA, FO},
    "reconcile": {SA, FO},
    "view_projects_admin": {SA, FO, PM, AU},
    "manage_projects": {SA, PM},
    "risk_assessment": {SA, PM},
    "financial_review": {SA, FO},
    "approve_project": {SA},
    "release_funding": {SA, FO},
    "verify_project_finance": {SA, FO},
    "manage_partners": {SA, PM},
    "view_distributions": {SA, FO, AU},
    "manage_distributions": {SA, FO},
    "approve_distribution": {SA, FO},
    "view_reports": {SA, FO, PM, AU},
    "publish_reports": {SA, FO},
    "view_audit": {SA, AU},
    "manage_meetings": {SA, FO, PM},
    "manage_votes": {SA},
    "manage_support": {SA, FO, PM},
    "view_support": {SA, FO, PM, AU},
    "manage_announcements": {SA, PM},
    "manage_content": {SA, PM},
    "view_flags": {SA, FO, AU},
    "review_flags": {SA, FO},
    "manage_settings": {SA},
    "manage_staff": {SA},
}


def has_permission(user, permission):
    if user is None or not user.is_active:
        return False
    return user.role_name in PERMISSIONS.get(permission, set())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            session["next_url"] = request.full_path if request.method == "GET" else None
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def permission_required(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.get("user") is None:
                return redirect(url_for("auth.login"))
            if not has_permission(g.user, permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.get("user") is None:
                return redirect(url_for("auth.login"))
            if g.user.role_name not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
