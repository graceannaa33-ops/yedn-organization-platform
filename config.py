"""Application configuration.

All secrets come from environment variables (see .env.example).
Nothing secret is ever hard-coded here.
"""
import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _database_url(env_name="DATABASE_URL", default=None):
    url = os.environ.get(env_name, "").strip()
    if not url:
        return default or "sqlite:///" + os.path.join(BASE_DIR, "instance", "app.db")
    # Render/Heroku give "postgres://" - SQLAlchemy needs "postgresql+psycopg://"
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-only-insecure-key-change-me"

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Sessions & cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", APP_ENV == "production")
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=int(os.environ.get("SESSION_TIMEOUT_MINUTES", "30")))
    WTF_CSRF_TIME_LIMIT = None  # token lives as long as the session

    # Uploads (stored OUTSIDE the static folder - never publicly served)
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER") or os.path.join(BASE_DIR, "uploads")
    # Where uploaded files are kept: "local" = UPLOAD_FOLDER on disk (good on your own PC),
    # "database" = inside the database (needed on hosts without a persistent disk, e.g. Render free plan).
    FILE_STORAGE = os.environ.get("FILE_STORAGE", "local").strip().lower()
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "5")) * 1024 * 1024
    MAX_CONTENT_LENGTH = MAX_FILE_SIZE * 2 + 1024 * 1024  # ID + photo + form fields

    # Login protection
    LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_WINDOW_MINUTES = int(os.environ.get("LOGIN_WINDOW_MINUTES", "15"))

    # Payments: "sandbox" (built-in simulator, development only) or "mpesa"
    PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "sandbox")
    PAYMENT_PUBLIC_KEY = os.environ.get("PAYMENT_PUBLIC_KEY", "")   # M-PESA consumer key
    PAYMENT_SECRET_KEY = os.environ.get("PAYMENT_SECRET_KEY", "")   # M-PESA consumer secret
    MPESA_ENV = os.environ.get("MPESA_ENV", "sandbox")              # sandbox | production
    MPESA_SHORTCODE = os.environ.get("MPESA_SHORTCODE", "")
    MPESA_PASSKEY = os.environ.get("MPESA_PASSKEY", "")
    MPESA_CALLBACK_TOKEN = os.environ.get("MPESA_CALLBACK_TOKEN", "")
    # Render sets RENDER_EXTERNAL_URL (e.g. https://yedn-platform.onrender.com) automatically.
    APP_BASE_URL = (os.environ.get("APP_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
                    or "http://127.0.0.1:5000")

    # SMS: "console" (log only) or "africastalking"
    SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "console")
    SMS_API_KEY = os.environ.get("SMS_API_KEY", "")
    SMS_USERNAME = os.environ.get("SMS_USERNAME", "")
    SMS_SENDER_ID = os.environ.get("SMS_SENDER_ID", "")

    # Email (SMTP). Empty MAIL_SERVER = emails are logged, not sent.
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587") or 587)
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_USE_TLS = _bool("MAIL_USE_TLS", True)
    MAIL_FROM = os.environ.get("MAIL_FROM", "") or os.environ.get("MAIL_USERNAME", "")

    ORGANIZATION_NAME = os.environ.get("ORGANIZATION_NAME", "Youth Enterprise & Development Network")
    ORGANIZATION_EMAIL = os.environ.get("ORGANIZATION_EMAIL", "")
    ORGANIZATION_PHONE = os.environ.get("ORGANIZATION_PHONE", "")

    DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Africa/Nairobi")
    TRUST_PROXY = _bool("TRUST_PROXY", APP_ENV == "production")

    # Public test deployments
    # Allow the built-in payment SIMULATOR even when APP_ENV=production (no real money moves).
    ALLOW_SANDBOX_PAYMENTS = _bool("ALLOW_SANDBOX_PAYMENTS", False)
    # Show the demo account list and password on the login page (only when demo data is loaded).
    SHOW_DEMO_LOGINS = _bool("SHOW_DEMO_LOGINS", APP_ENV != "production")
    # Used by `flask prepare-deploy` at start-up on hosts without a shell (see DEPLOY_RENDER.md)
    SEED_DEMO_DATA = _bool("SEED_DEMO_DATA", False)
    DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "") or "DemoPass2026!"
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
    ADMIN_NAME = os.environ.get("ADMIN_NAME", "") or "Super Admin"
    TESTING = False


class TestConfig(Config):
    TESTING = True
    APP_ENV = "testing"
    SECRET_KEY = "test-secret"
    SQLALCHEMY_DATABASE_URI = _database_url("TEST_DATABASE_URL", "sqlite://")
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    PAYMENT_PROVIDER = "sandbox"
    SMS_PROVIDER = "console"
    MAIL_SERVER = ""
    TRUST_PROXY = False
    FILE_STORAGE = os.environ.get("TEST_FILE_STORAGE", "local")
    SHOW_DEMO_LOGINS = True
