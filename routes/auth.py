"""Registration, login (with rate limiting and staff 2FA), logout and account security."""
import os
import re
from datetime import timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db
from helpers import (client_ip, normalize_phone, password_problems, password_stamp, utcnow, valid_email)
from i18n import SUPPORTED_LANGUAGES
from models import IDDocument, LoginAttempt, Member, Role, User
from permissions import login_required
from services import anomaly_service, audit_service, contribution_service, file_service, twofactor_service
from services.settings_service import get_bool, get_setting

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("timing-equaliser-not-a-password")


def home_for(user):
    if user.role_name == "member":
        return url_for("members.dashboard")
    if user.role_name == "partner":
        return url_for("partner.dashboard")
    return url_for("admin.dashboard")


def _too_many_attempts(email):
    cfg = current_app.config
    since = utcnow() - timedelta(minutes=cfg["LOGIN_WINDOW_MINUTES"])
    by_email = LoginAttempt.query.filter(LoginAttempt.email == email, LoginAttempt.success.is_(False),
                                         LoginAttempt.created_at >= since).count()
    by_ip = LoginAttempt.query.filter(LoginAttempt.ip_address == client_ip(), LoginAttempt.success.is_(False),
                                      LoginAttempt.created_at >= since).count()
    return by_email >= cfg["LOGIN_MAX_ATTEMPTS"] or by_ip >= cfg["LOGIN_MAX_ATTEMPTS"] * 4


def _record_attempt(email, success):
    db.session.add(LoginAttempt(email=email, ip_address=client_ip(), success=success))


def _finish_login(user):
    session.clear()
    session["user_id"] = user.id
    session["pw_stamp"] = password_stamp(user)
    session["last_seen"] = utcnow().timestamp()
    session["lang"] = user.language or "en"
    session.permanent = True
    user.last_login_at = utcnow()
    audit_service.log("auth.login", "user", user.id, user=user)
    db.session.commit()


def _safe_next(url):
    if url and url.startswith("/") and not url.startswith("//") and not url.startswith("/\\"):
        return url
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(home_for(g.user))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        password = request.form.get("password") or ""
        if _too_many_attempts(email):
            return render_template("errors/429.html"), 429
        user = User.query.filter(func.lower(User.email) == email).first()
        ok = user.check_password(password) if user else (check_password_hash(_DUMMY_HASH, password) and False)
        if not ok or not user.is_active:
            _record_attempt(email, False)
            anomaly_service.check_failed_logins(email, user)
            db.session.commit()
            flash("Email or password is not correct.", "error")
            return render_template("auth/login.html", email=email), 401
        _record_attempt(email, True)
        if user.is_staff and user.totp_enabled:
            next_url = session.get("next_url")
            session.clear()
            session["pending_2fa_user"] = user.id
            session["pending_2fa_at"] = utcnow().timestamp()
            session["next_url"] = next_url
            db.session.commit()
            return redirect(url_for("auth.two_factor"))
        next_url = _safe_next(session.get("next_url"))
        _finish_login(user)
        flash(f"Welcome, {user.full_name}.", "success")
        return redirect(next_url or home_for(user))
    return render_template("auth/login.html", email="")


@bp.route("/login/2fa", methods=["GET", "POST"])
def two_factor():
    user_id = session.get("pending_2fa_user")
    started = session.get("pending_2fa_at", 0)
    if not user_id or utcnow().timestamp() - started > 300:
        session.clear()
        flash("Please log in again.", "info")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, user_id)
    if request.method == "POST":
        key = f"2fa:{user.email}"
        if _too_many_attempts(key):
            session.clear()
            return render_template("errors/429.html"), 429
        if twofactor_service.verify(user, request.form.get("code")):
            _record_attempt(key, True)
            next_url = _safe_next(session.get("next_url"))
            _finish_login(user)
            return redirect(next_url or home_for(user))
        _record_attempt(key, False)
        audit_service.log("auth.2fa_failed", "user", user.id, user=user)
        anomaly_service.check_failed_logins(key, user)
        db.session.commit()
        flash("That code is not correct. Codes change every 30 seconds.", "error")
    return render_template("auth/two_factor.html")


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit_service.log("auth.logout", "user", g.user.id)
        db.session.commit()
    session.clear()
    flash("You have logged out.", "info")
    return redirect(url_for("public.home"))


ID_RE = re.compile(r"^[A-Z0-9]{5,20}$")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(home_for(g.user))
    form = {k: (request.form.get(k) or "").strip() for k in ("full_name", "id_number", "phone", "email")}
    if request.method == "GET":
        return render_template("auth/register.html", form=form, errors={})

    errors = {}
    full_name = form["full_name"]
    id_number = re.sub(r"[\s-]", "", form["id_number"]).upper()
    phone = normalize_phone(form["phone"])
    email = form["email"].lower()
    password = request.form.get("password") or ""
    if len(full_name) < 3 or len(full_name) > 150:
        errors["full_name"] = "Enter your full name."
    if not ID_RE.match(id_number):
        errors["id_number"] = "Enter a valid ID or passport number (letters and numbers only)."
    elif Member.query.filter_by(id_number=id_number).first():
        errors["id_number"] = "An account with this ID number already exists. Contact support if this is you."
    if not phone:
        errors["phone"] = "Enter a valid phone number, e.g. 0712345678."
    if not valid_email(email):
        errors["email"] = "Enter a valid email address."
    elif User.query.filter(func.lower(User.email) == email).first():
        errors["email"] = "An account with this email already exists."
    problems = password_problems(password)
    if problems:
        errors["password"] = " ".join(problems)
    elif password != request.form.get("confirm_password"):
        errors["password"] = "The two passwords do not match."
    if request.form.get("agree") != "yes":
        errors["agree"] = "You must agree to the Terms and Privacy Policy."
    if not file_service.has_file(request.files.get("id_document")):
        errors["id_document"] = "Upload a copy of your ID or passport (PDF, JPG or PNG)."
    if errors:
        return render_template("auth/register.html", form=form, errors=errors), 400

    saved = []
    try:
        id_file = file_service.save_upload(request.files["id_document"], "id_documents")
        saved.append(("id_documents", id_file["stored_name"]))
        photo_name = None
        if file_service.has_file(request.files.get("profile_photo")):
            photo = file_service.save_upload(request.files["profile_photo"], "profile_photos", file_service.IMAGE_TYPES)
            saved.append(("profile_photos", photo["stored_name"]))
            photo_name = photo["stored_name"]
    except file_service.UploadError as exc:
        _remove(saved)
        field = "profile_photo" if saved else "id_document"
        errors[field] = str(exc)
        return render_template("auth/register.html", form=form, errors=errors), 400

    try:
        user = User(email=email, full_name=full_name, role=Role.query.filter_by(name="member").one(),
                    language=session.get("lang", "en"))
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        status = "pending" if get_bool("require_membership_approval") else "active"
        member = Member(user_id=user.id, id_number=id_number, phone=phone, status=status,
                        profile_photo=photo_name, terms_accepted_at=utcnow(),
                        admitted_at=None if status == "pending" else utcnow())
        db.session.add(member)
        db.session.flush()
        member.member_number = f"MBR-{member.id:05d}"
        db.session.add(IDDocument(member_id=member.id, original_name=id_file["original_name"],
                                  stored_name=id_file["stored_name"], mime_type=id_file["mime_type"],
                                  size_bytes=id_file["size_bytes"], sha256=id_file["sha256"]))
        contribution_service.ensure_current_contribution(member)
        audit_service.log("member.registered", "member", member.member_number,
                          new={"email": email, "status": status}, user=user)
        anomaly_service.check_duplicate_account(member)
        db.session.commit()
    except Exception:
        db.session.rollback()
        _remove(saved)
        raise
    _finish_login(user)
    if status == "pending":
        flash("Registration received. Your membership is waiting for approval by the organisation.", "info")
    else:
        flash(f"Welcome! Your member ID is {member.member_number}.", "success")
    return redirect(url_for("members.dashboard"))


def _remove(saved):
    for folder, name in saved:
        try:
            os.remove(os.path.join(file_service.folder_path(folder), name))
        except OSError:
            pass


# ---------------------------------------------------------------- account
@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = g.user
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            if not user.check_password(request.form.get("current_password")):
                flash("Your current password is not correct.", "error")
            else:
                new = request.form.get("new_password") or ""
                problems = password_problems(new)
                if problems:
                    flash(" ".join(problems), "error")
                elif new != request.form.get("confirm_password"):
                    flash("The new passwords do not match.", "error")
                else:
                    user.set_password(new)
                    audit_service.log("auth.password_changed", "user", user.id)
                    db.session.commit()
                    session["pw_stamp"] = password_stamp(user)
                    flash("Password changed. Other devices have been logged out.", "success")
        elif action == "language":
            lang = request.form.get("language")
            if lang in SUPPORTED_LANGUAGES:
                user.language = lang
                session["lang"] = lang
                db.session.commit()
                flash("Language saved.", "success")
        elif action == "disable_2fa" and user.totp_enabled:
            if get_bool("require_admin_2fa"):
                flash("Two-factor authentication is required by the organisation and cannot be turned off.", "error")
            elif twofactor_service.verify(user, request.form.get("code")):
                user.totp_enabled, user.totp_secret = False, None
                audit_service.log("auth.2fa_disabled", "user", user.id)
                db.session.commit()
                flash("Two-factor authentication turned off.", "info")
            else:
                flash("That code is not correct.", "error")
        return redirect(url_for("auth.account"))
    return render_template("auth/account.html")


@bp.route("/account/two-factor", methods=["GET", "POST"])
@login_required
def two_factor_setup():
    user = g.user
    if not user.is_staff:
        abort(403)
    if user.totp_enabled:
        return redirect(url_for("auth.account"))
    secret = session.get("totp_setup_secret") or twofactor_service.new_secret()
    session["totp_setup_secret"] = secret
    if request.method == "POST":
        user.totp_secret = secret
        if twofactor_service.verify(user, request.form.get("code")):
            user.totp_enabled = True
            session.pop("totp_setup_secret", None)
            audit_service.log("auth.2fa_enabled", "user", user.id)
            db.session.commit()
            flash("Two-factor authentication is now on.", "success")
            return redirect(url_for("auth.account"))
        user.totp_secret = None
        user.totp_last_counter = None
        flash("That code is not correct. Check your phone's time and try again.", "error")
    user_tmp = type("U", (), {"email": user.email, "totp_secret": secret})()
    return render_template("auth/two_factor_setup.html", secret=twofactor_service.formatted_secret(secret),
                           uri=twofactor_service.provisioning_uri(user_tmp, get_setting("organization_name")),
                           required=get_bool("require_admin_2fa"))
