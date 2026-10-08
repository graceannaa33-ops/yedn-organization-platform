"""Application entry point.

Development:   python app.py
Production:    gunicorn app:app
"""
import logging
import os

import click
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError
from markupsafe import Markup, escape

from config import Config
from extensions import csrf, db, migrate
from helpers import format_date, format_datetime, format_money, mask_id_number, mask_phone, password_stamp, utcnow
from i18n import SUPPORTED_LANGUAGES, current_language, gettext
from permissions import ROLE_LABELS, has_permission


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if app.config["APP_ENV"] == "production" and app.config["SECRET_KEY"].startswith("dev-only"):
        raise RuntimeError("Set a strong SECRET_KEY environment variable in production.")
    if app.config["TRUST_PROXY"]:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations"))
    csrf.init_app(app)

    import models  # noqa: F401  (register models with SQLAlchemy)
    from routes import register_blueprints
    register_blueprints(app)

    @app.route("/healthz")
    def healthz():
        """Used by Render to check the site is up (also checks the database connection)."""
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok"}

    _register_hooks(app)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)
    return app


# ---------------------------------------------------------------- request hooks
def _register_hooks(app):
    from models import User
    from services.settings_service import get_bool

    @app.before_request
    def load_user_and_guard():
        g.user = None
        lang = request.args.get("lang")
        if lang in SUPPORTED_LANGUAGES:
            session["lang"] = lang
        user_id = session.get("user_id")
        if user_id:
            last_seen = session.get("last_seen")
            timeout = app.permanent_session_lifetime.total_seconds()
            now = utcnow().timestamp()
            if last_seen and now - last_seen > timeout:
                session.clear()
                flash("Your session expired after a period of inactivity. Please log in again.", "info")
            else:
                user = db.session.get(User, user_id)
                # Sessions die when the password changes or the account is disabled
                if user and user.is_active and session.get("pw_stamp") == password_stamp(user):
                    g.user = user
                    session["last_seen"] = now
                    session.permanent = True
                else:
                    session.clear()
        endpoint = request.endpoint or ""
        if endpoint.startswith("static") or endpoint in ("public.logo", "healthz"):
            return None
        if get_bool("maintenance_mode") and not (g.user and g.user.is_staff) \
                and not endpoint.startswith("auth.") and endpoint != "payments.mpesa_callback":
            return render_template("errors/maintenance.html"), 503
        # Staff must set up 2FA when the organisation requires it
        if g.user and g.user.is_staff and get_bool("require_admin_2fa") and not g.user.totp_enabled \
                and endpoint not in ("auth.two_factor_setup", "auth.logout"):
            return redirect(url_for("auth.two_factor_setup"))
        return None

    @app.after_request
    def security_headers(response):
        # script-src 'none' enforces the no-JavaScript rule in the browser itself.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'none'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; style-src 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config["APP_ENV"] == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if g.get("user") is not None:
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response


# ---------------------------------------------------------------- templates
def _register_template_helpers(app):
    import default_content
    from services.settings_service import fill, get_setting, org_full, org_short

    def paragraphs(text):
        """Plain text -> safe HTML paragraphs and bullet lists (input is escaped)."""
        html = []
        for block in (text or "").replace("\r\n", "\n").split("\n\n"):
            lines = [ln for ln in block.split("\n") if ln.strip()]
            if not lines:
                continue
            bullets = [ln[2:] for ln in lines if ln.startswith("- ")]
            others = [ln for ln in lines if not ln.startswith("- ")]
            if others:
                if len(others) == 1 and bullets and len(others[0]) < 60:
                    html.append(f"<h3>{escape(others[0])}</h3>")
                else:
                    html.append("<p>" + "<br>".join(str(escape(o)) for o in others) + "</p>")
            if bullets:
                html.append("<ul>" + "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul>")
        return Markup("".join(html))

    app.jinja_env.filters.update(money=format_money, dt=format_datetime, date=format_date,
                                 mask_id=mask_id_number, mask_phone=mask_phone, paragraphs=paragraphs)

    @app.context_processor
    def inject():
        unread = 0
        if g.get("user") is not None:
            from models import Notification
            unread = Notification.query.filter_by(user_id=g.user.id, read_at=None).count()
        return {"current_user": g.get("user"), "can": lambda p: has_permission(g.get("user"), p),
                "setting": get_setting, "ROLE_LABELS": ROLE_LABELS, "_": gettext,
                "LANGUAGES": SUPPORTED_LANGUAGES, "current_lang": current_language(),
                "unread_count": unread, "is_demo": get_setting("is_demo_data") == "1",
                "show_demo_logins": app.config.get("SHOW_DEMO_LOGINS", False),
                "demo_password": app.config.get("DEMO_PASSWORD"),
                "sandbox_test_mode": app.config["APP_ENV"] == "production" and app.config.get("ALLOW_SANDBOX_PAYMENTS"),
                "org_name": get_setting("organization_name"), "org_short": org_short(), "org_full": org_full(),
                "content": default_content, "fill": fill}


# ---------------------------------------------------------------- errors
def _register_error_handlers(app):
    log = logging.getLogger("errors")

    def page(code, template=None):
        def handler(error):
            if code == 500:
                db.session.rollback()
                log.exception("Server error on %s", request.path)
            return render_template(template or f"errors/{code}.html"), code
        return handler

    for code in (400, 401, 403, 404, 405, 429, 500):
        app.register_error_handler(code, page(code, "errors/404.html" if code == 405 else None))
    app.register_error_handler(413, lambda e: (render_template("errors/upload_error.html",
                                                              message="The upload is too large."), 413))

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        return render_template("errors/400.html",
                               message="Your form expired or was not valid. Please go back, reload the page and try again."), 400


# ---------------------------------------------------------------- CLI
def _register_cli(app):
    @app.cli.command("seed-demo")
    def seed_demo():
        """Load clearly-labelled DEMO data (users, members, projects, money)."""
        from seed import run_seed
        run_seed()

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option()
    def create_admin(email, name, password):
        """Create the first Super Admin for a real (non-demo) installation."""
        from helpers import password_problems
        from models import Role, User
        from seed import ensure_roles
        problems = password_problems(password)
        if problems:
            raise click.ClickException(" ".join(problems))
        ensure_roles()
        if User.query.filter_by(email=email.lower()).first():
            raise click.ClickException("A user with that email already exists.")
        user = User(email=email.lower().strip(), full_name=name.strip(),
                    role=Role.query.filter_by(name="super_admin").one())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Super Admin {email} created.")

    @app.cli.command("prepare-deploy")
    def prepare_deploy():
        """Run at every start on a host without a shell (Render free plan):
        1) apply database migrations, 2) load demo data if SEED_DEMO_DATA=true,
        3) create the first Super Admin from ADMIN_EMAIL / ADMIN_PASSWORD if set.
        Every step is safe to repeat - nothing is overwritten or deleted."""
        from flask_migrate import stamp, upgrade
        tables = set(db.inspect(db.engine).get_table_names())
        if "users" in tables and "alembic_version" not in tables:
            # Tables were made with "init-db" (create_all), not migrations: create any missing
            # tables, then record the database as up to date instead of re-creating everything.
            db.create_all()
            stamp()
            click.echo("Existing tables found; migration history recorded.")
        else:
            upgrade()
            click.echo("Database migrations applied.")
        from seed import ensure_roles
        ensure_roles()
        db.session.commit()
        if app.config["SEED_DEMO_DATA"]:
            from seed import run_seed
            run_seed()
        email, password = app.config["ADMIN_EMAIL"], app.config["ADMIN_PASSWORD"]
        if email and password:
            from helpers import password_problems
            from models import Role, User
            if User.query.filter_by(email=email).first():
                click.echo(f"Admin account {email} already exists - left unchanged.")
            elif password_problems(password):
                click.echo("ADMIN_PASSWORD is too weak (10+ characters, letters and numbers). Admin not created.")
            else:
                user = User(email=email, full_name=app.config["ADMIN_NAME"],
                            role=Role.query.filter_by(name="super_admin").one())
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                click.echo(f"Super Admin {email} created.")
        click.echo("Ready.")

    @app.cli.command("init-db")
    def init_db():
        """Create tables directly (alternative to 'flask db upgrade')."""
        db.create_all()
        from seed import ensure_roles
        ensure_roles()
        db.session.commit()
        click.echo("Database tables created.")


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
