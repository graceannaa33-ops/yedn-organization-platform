# Youth Enterprise & Development Network (YEDN)

**"Empowering Youth. Building Enterprises. Creating Opportunities."**

The website and member platform of Youth Enterprise & Development Network
(YEDN), a youth-led organization. It is a complete contribution, project and
transparency system: members register, upload an ID
document and pay their contribution in full or in parts; the organisation
funds approved projects, tracks their revenue and expenses, distributes
verified profit under documented rules, and publishes transparent figures.

**Stack: Python 3 · Flask · Jinja2 · HTML · CSS · SQL (SQLite / PostgreSQL).**
There is **no JavaScript, no PHP, no Node.js, no npm and no build step.**

---

## Contents

1. [What it does](#what-it-does)
2. [Why no JavaScript or PHP](#why-no-javascript-or-php)
3. [Project structure](#project-structure)
4. [Install and run on Windows](#install-and-run-on-windows)
5. [Environment variables](#environment-variables)
6. [Database and migrations](#database-and-migrations)
7. [Demo (seed) data](#demo-seed-data)
8. [How the money works](#how-the-money-works)
9. [Payments (M-PESA)](#payments-m-pesa)
10. [SMS and email](#sms-and-email)
11. [Distribution rules](#distribution-rules)
12. [Roles](#roles)
13. [Security](#security)
14. [File storage](#file-storage)
15. [Production deployment on Render](#production-deployment-on-render)
16. [Backups](#backups)
17. [Testing](#testing)
18. [Troubleshooting](#troubleshooting)

---

## What it does

| Area | Features |
|---|---|
| Public | Home (YEDN hero, live statistics), About (description, vision, mission, objectives, values, founding), What We Do, How YEDN Works (10 steps), Programs, Opportunities, Membership, Member Rights & Responsibilities, Governance, Organization Structure, Responsible Project Funding, Projects, Partners, Transparency centre, published Reports, FAQ, Contact, Terms, Privacy, Risk Disclosure, Contribution / Distribution / Refund / Complaints policies, public Complaints with status lookup |
| Members | Dashboard (member ID, details, required / paid / remaining, status, progress), profile & ID re-upload, contributions, payment history with running totals, M-PESA payments, receipts (HTML + PDF), statement (HTML, CSV, PDF), projects, distributions, notifications, reports, meetings (RSVP), voting, support tickets, account settings |
| Admin | Programs and Opportunities (create/publish), organization identity settings, dashboard with CSS charts, members (search/filter, admit/suspend, ID documents), payments, manual bank/cash payments, ledger, financial position, approvals queue, reconciliation, projects & approval workflow, partners, distributions, reports & publishing, meetings, voting, support, complaints, announcements, anomaly alerts, audit log, staff & roles, settings |
| Partners | Separate portal: assigned projects only; progress updates, milestones, expenses, revenue, reports, receipts, photos, documents |

There is **no KYC system**: registration collects member information and an ID
document, nothing more.

## YEDN identity and content

- **Names:** the full name *Youth Enterprise & Development Network* (with "(YEDN)") is used on formal pages, reports, receipts, statements and PDFs; **YEDN** is used in navigation, dashboards, the logo mark, SMS and email subjects.
- **Everything is editable** in **Admin → Settings → Identity**: name, short name, tagline, supporting message, description, vision, mission, founding text, website, registration information, leadership, founding team and university relationship. Contact details (email, phone, address) are in **General**. Reports and receipts print whatever is configured there; nothing is hard-coded.
- **Nothing is invented.** No founders, leaders, registration numbers, partners, programs or university links are shown until an administrator enters official information. Programs and opportunities appear publicly only when published. A shortcut adds the twelve YEDN program areas as *unpublished drafts*.
- **Honest numbers.** Homepage statistics (registered members, active projects, projects under implementation, completed projects, total verified contributions) are counted from the database on every page load; with no data they show 0.
- **Legal safety.** Default texts never claim guaranteed returns, guaranteed funding, government or university endorsement, bank or SACCO status, an investment licence, regulatory approval, tax exemption or registered-NGO status. A test (`tests/test_identity.py`) scans the source for such claims and for the old placeholder names.
- **Existing databases:** `flask --app app db upgrade` adds the Programs and Opportunities tables and replaces the old placeholder name, tagline and demo contact details with the YEDN identity. Financial data is not touched.

## Why no JavaScript or PHP

The brief asks for an application that is simple to host, simple to learn and
hard to break. Everything a browser does here is a normal link or a normal
HTML form `POST` handled by Flask:

- Menus use a CSS-only checkbox toggle; FAQs and extra forms use `<details>`.
- Charts are CSS bars and `<progress>` elements.
- A pending payment page refreshes itself with `<meta http-equiv="refresh">`.
- Every page is sent with `Content-Security-Policy: script-src 'none'`, so the
  browser itself would refuse to run JavaScript even if some were added.
- A test (`tests/test_permissions.py::test_no_javascript_or_php_anywhere`)
  fails if any `.js`, `.php` or `.ts` file, `<script>` tag, inline event
  handler or `javascript:` URL appears in the project.

M-PESA Express (STK Push) is entirely server-to-server, so **no JavaScript
payment SDK is needed**.

## Project structure

```
organization-platform/
├── app.py                  # create_app(), hooks, error pages, CLI commands
├── wsgi.py                 # production entry point: `gunicorn wsgi:app`
├── render.yaml             # Render Blueprint (web service + PostgreSQL)
├── DEPLOY_RENDER.md        # beginner deployment guide
├── config.py               # settings from environment variables
├── extensions.py           # db, migrate, csrf objects
├── models.py               # all database tables + "no delete / no edit" guards for money records
├── helpers.py              # money (integer cents), dates, pagination
├── permissions.py          # roles and the permission table
├── i18n.py                 # translation helper (English now, Kiswahili ready)
├── default_content.py      # default legal/policy text (editable in Settings)
├── seed.py                 # DEMO data
├── routes/                 # auth, members, payments, projects, finance, distributions,
│                           # reports, transparency, partner, admin, community, public
├── services/               # payment, notification, ledger, distribution, report, project,
│                           # contribution, governance, anomaly, audit, file, settings, 2FA
├── templates/              # Jinja2 HTML (auth, member, admin, projects, partner, finance, public, errors)
├── static/css/main.css     # the only stylesheet
├── translations/sw.json    # Kiswahili starter catalogue
├── uploads/                # PRIVATE files (never served directly)
├── migrations/             # Flask-Migrate / Alembic
├── tests/                  # pytest suite (90 tests)
├── requirements.txt  .env.example  .gitignore  Procfile
```

## Install and run on Windows

You need Python 3.11 or newer.

```bat
cd organization-platform
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and set `SECRET_KEY` to a long random value. Then:

```bat
flask --app app db upgrade        :: create the database tables (runs the migrations)
flask --app app seed-demo         :: optional: load DEMO data
python app.py                     :: start on http://127.0.0.1:5000
```

For a real organisation, skip `seed-demo` and create the first Super Admin:

```bat
flask --app app create-admin
```

On macOS/Linux use `source venv/bin/activate` and `cp` instead of `copy`.

## Environment variables

See `.env.example`. The important ones:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` or `production` (production enables secure cookies, HSTS, refuses the payment simulator and a weak `SECRET_KEY`) |
| `SECRET_KEY` | Signs sessions. Long and random. |
| `DATABASE_URL` | Empty = SQLite in `instance/app.db`. `postgres://…` URLs are converted automatically. |
| `PAYMENT_PROVIDER` | `sandbox` (simulator) or `mpesa` |
| `PAYMENT_PUBLIC_KEY` / `PAYMENT_SECRET_KEY` | M-PESA Daraja consumer key / secret |
| `MPESA_ENV`, `MPESA_SHORTCODE`, `MPESA_PASSKEY` | Daraja environment, paybill/till, passkey |
| `MPESA_CALLBACK_TOKEN` | Random secret that forms part of the callback URL |
| `APP_BASE_URL` | Public https address, used to build the callback URL |
| `SMS_PROVIDER`, `SMS_API_KEY`, `SMS_USERNAME`, `SMS_SENDER_ID` | Africa's Talking SMS (`console` = log only) |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_USE_TLS`, `MAIL_FROM` | SMTP email (empty server = log only) |
| `ORGANIZATION_NAME`, `ORGANIZATION_EMAIL`, `ORGANIZATION_PHONE` | Defaults until changed in Settings |
| `FILE_STORAGE` | `local` (uploads/ folder) or `database` (inside PostgreSQL — use on Render free plan) |
| `UPLOAD_FOLDER` | Where private files live when `FILE_STORAGE=local` |
| `ALLOW_SANDBOX_PAYMENTS`, `SEED_DEMO_DATA`, `DEMO_PASSWORD`, `SHOW_DEMO_LOGINS`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Public test deployments — see DEPLOY_RENDER.md |
| `TRUST_PROXY` | `true` behind Render/NGINX so client IPs and https are detected |

Never commit `.env` — it is in `.gitignore`. Secrets are never stored in the database.

## Database and migrations

- SQLAlchemy models are in `models.py`; all foreign keys and the main lookup columns are indexed.
- Development uses SQLite; production uses PostgreSQL. The same code runs on both and the full test suite has been run against both.
- After changing a model: `flask --app app db migrate -m "describe change"` then `flask --app app db upgrade`.
- `flask --app app init-db` creates tables directly without migrations (handy for experiments only).

## Demo (seed) data

`flask --app app seed-demo` creates clearly labelled `[DEMO]` data and shows a
"DEMO SITE" banner on every page. All demo accounts use the password
**`DemoPass2026!`**:

| Login | Shows |
|---|---|
| `superadmin@demo.org` | Super Admin |
| `finance@demo.org` | Finance Officer |
| `pm@demo.org` | Project Manager |
| `auditor@demo.org` | Auditor (read-only) |
| `partner@demo.org` | Project Partner (Greenfields Cooperative) |
| `member1@demo.org` | Fully paid via 2,000 + 3,000 + 5,000 |
| `member2@demo.org` | Fully paid by verified bank deposit |
| `member3@demo.org` | Partially paid (4,000 of 10,000) |
| `member4@demo.org` | Unpaid, one cancelled attempt |
| `member5@demo.org` | Paid in full **after the deadline** — excluded from the poultry distribution by the demo "future only" rule |

Also included: an active project with verified expenses, revenue and a
distribution (one share paid, one approved), a completed project with a final
report, a bank fee, a reconciliation, meetings, an open vote, an announcement,
a support ticket, a complaint and two published reports.

## How the money works

- **Integer cents everywhere.** `KSh 2,000.50` is stored as `200050`. Floats are never used.
- **Double-entry ledger.** Every financial event is a `Transaction`; when approved it is *posted* as two `LedgerEntry` lines (debit + credit). The dashboard warns if debits ever stop equalling credits.
- **No editable balances.** Balances are always sums of ledger lines. The financial position page shows:
  `Opening balance + verified income − verified outflows + approved adjustments = calculated balance`.
- **Maker / checker.** With the two-person rule on (default), manual transactions, project funding releases, bank/cash payments, partner-reported expenses/revenue and distributions must be approved by someone other than the person who entered them.
- **Corrections are reversals.** Payments, transactions and ledger lines cannot be deleted, and amounts cannot be edited (enforced in `models.py`). A mistake is corrected with a reversal transaction that keeps the original on record.
- **Contribution maths.** `Remaining = required − verified (Successful) payments`. Pending, failed, cancelled and reversed payments never count.
- **Project result.** `Net result = verified revenue − verified expenses`. Revenue is never called profit; losses show as losses.
- **Bank figures are honest.** There is no live bank feed, so the Transparency page labels balances as organisation-reported and shows the "Last reconciled" date from the Reconciliation page.

## Payments (M-PESA)

Flow (no JavaScript):

```
Pay form (POST) → Flask creates a Pending payment with a unique reference and
idempotency key → STK Push to Safaricom → member enters PIN on phone →
Safaricom calls /payments/callback/mpesa/<MPESA_CALLBACK_TOKEN> →
server ASKS SAFARICOM AGAIN (STK status query) → only then: Successful,
ledger entry, receipt, SMS/email, audit log.
```

Protections: secret callback URL; every callback re-confirmed by an
independent status query; amount must match the request; unique M-PESA
receipt numbers (a re-used receipt raises an alert and is not counted);
unique checkout IDs; idempotency keys so double-clicks never create two
payments; row locking on PostgreSQL; overpayment blocked; repeated failures
flagged for review.

**Going live:** create a Daraja app at developer.safaricom.co.ke, set
`PAYMENT_PROVIDER=mpesa` and the `MPESA_*` / `PAYMENT_*` variables, set
`APP_BASE_URL` to your public https address and a long random
`MPESA_CALLBACK_TOKEN`. Test with `MPESA_ENV=sandbox` first.

**Sandbox simulator:** with `PAYMENT_PROVIDER=sandbox` (development only) a
"simulated phone" page lets you approve or decline a payment (PIN `1234`).
It goes through exactly the same verification code. It is disabled when
`APP_ENV=production`.

**Bank / cash:** a finance officer records the slip number (duplicates are
refused); a second officer verifies it against the statement.

## SMS and email

`services/notification_service.py` sends from Python:

- SMS via Africa's Talking (`SMS_PROVIDER=africastalking`, `SMS_USERNAME=sandbox` for their sandbox).
- Email via any SMTP server.
- Without credentials, messages are written to the server log, and every message is also stored as an in-app notification.

Message wording (for example *"Payment received: KSh 2,000. Total contribution: KSh 5,000. Remaining contribution: KSh 5,000."*) is editable in **Settings → Notification templates**.

## Distribution rules

The system **makes no assumptions about eligibility.** Until a Super Admin
sets all three rules in **Settings → Distribution rules**, no distribution can
be calculated.

1. **Eligibility basis (on the record date):** fully paid members only, *or* any member with a verified contribution.
2. **Allocation:** equal shares, *or* pro-rata to each member's verified contribution.
3. **Members who complete their contribution after the deadline:**
   - *current and future* — treated like everyone else once fully paid;
   - *future only* — they share only in projects first funded **after** they completed;
   - *none* — they do not share in distributions.

The chosen rules are shown in plain language on the public Distribution Rules
page and copied into every distribution run, so later rule changes never
rewrite history. Only verified net project results (minus earlier
distributions) can be distributed; shares are rounded down and leftover cents
stay with the organisation. Each run is calculated → approved by a second
officer → paid line by line with a unique payment reference → visible on the
member's dashboard. Paid lines can be reversed.

## Roles

| Role | Can |
|---|---|
| Super Admin | Everything, including settings, staff, ID documents, project approval, votes |
| Finance Officer | Payments, ledger, reconciliation, funding releases, verifying partner reports, distributions |
| Project Manager | Projects, reviews, risk assessment, milestones, documents, meetings, announcements |
| Auditor | Read-only access to finance, reports and the audit log |
| Project Partner | Only their organisation's projects |
| Member | Their own account |

Every check is done on the server (`permissions.py`). The full matrix is shown
on **Staff & Roles**.

## Security

- Passwords hashed with Werkzeug (scrypt); minimum 10 characters with letters and numbers.
- CSRF tokens on every form (Flask-WTF).
- Cookies: HttpOnly, SameSite=Lax, Secure in production; 30-minute idle timeout; sessions end when the password changes.
- Login rate limiting (5 failures per 15 minutes per email, 20 per IP → HTTP 429); alerts on repeated staff failures.
- Optional or mandatory **2FA for staff** (authenticator-app codes, typed setup key — no QR library or JavaScript needed).
- SQL injection: all queries go through SQLAlchemy with bound parameters.
- Output escaping: Jinja2 autoescaping; user text is escaped before line breaks are added.
- Strict security headers (CSP with `script-src 'none'`, `X-Frame-Options`, `nosniff`, HSTS in production).
- CSV exports neutralise spreadsheet formulas.
- Anomaly checks (duplicate references, possible duplicate transactions, repeated failed payments, unusual admin activity, unusual distributions, shared phone numbers) create alerts for **human review** — they never accuse or block anyone.
- Audit log of every important action: user, role, action, time, record, old/new values, reason, IP. Entries cannot be edited or deleted.
- The website does not claim any licence or regulatory approval. Have the legal texts reviewed before launch.

## File storage

- ID documents, photos and project files are saved in `UPLOAD_FOLDER` (default `uploads/`), which is **not** the static folder — there is no public URL for them.
- Only PDF/JPG/JPEG/PNG; the file's first bytes must match its type; size limit (default 5 MB); random file names; never executed.
- Files are delivered only by authorised routes with `no-store`, `noindex` and sandbox headers. Only Super Admins (and the member themselves) can open an ID document, and every viewing is audit-logged.
- Project photos/documents become public only when a project manager marks them public.

## Production deployment on Render

**Step-by-step beginner guide (Windows + GitHub + Render): see [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md).**

Summary:

- `render.yaml` (Render Blueprint) creates a free web service and a free PostgreSQL database.
- Entry point: `wsgi.py` → `gunicorn wsgi:app`. Python is pinned to 3.11.9 (`.python-version` and `PYTHON_VERSION`).
- Start command: `flask --app app prepare-deploy && gunicorn wsgi:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT`.
  `prepare-deploy` applies migrations, loads demo data if `SEED_DEMO_DATA=true` (never twice) and creates the
  first Super Admin from `ADMIN_EMAIL` / `ADMIN_PASSWORD` — needed because the free plan has no shell. It is safe to run on every start.
- Uploads: `FILE_STORAGE=database` stores ID documents, photos and project files in PostgreSQL, because the free
  plan's disk is wiped on every restart. On a paid plan you may instead attach a persistent disk and use
  `FILE_STORAGE=local` with `UPLOAD_FOLDER` pointing at the disk.
- Test deployments: `ALLOW_SANDBOX_PAYMENTS=true` enables the payment simulator in production (no real money);
  `SHOW_DEMO_LOGINS=false` keeps the demo password off the public login page; `DEMO_PASSWORD` sets it.
- Health check: `/healthz`.

Any other Linux Python host works the same way: Python + Gunicorn + PostgreSQL + environment variables.

## Backups

**Daily database backup (PostgreSQL)** — run from a scheduled job (cron, Render cron job, Windows Task Scheduler):

```bash
pg_dump --format=custom --no-owner "$DATABASE_URL" > backups/db-$(date +%F).dump
```

Render's paid PostgreSQL plans also keep automatic daily backups — still keep your own copy off-site.

**Uploaded files** — back up `UPLOAD_FOLDER` daily:

```bash
tar -czf backups/uploads-$(date +%F).tar.gz -C /var/data uploads
```

**Retention** — keep 7 daily, 4 weekly and 12 monthly copies; store at least one copy in a different place (e.g. encrypted cloud storage). Backups contain ID documents: encrypt them and limit who can access them.

**Restore**

```bash
createdb restored_db
pg_restore --no-owner --dbname=restored_db backups/db-2026-10-01.dump
tar -xzf backups/uploads-2026-10-01.tar.gz -C /var/data
```

Point `DATABASE_URL` at the restored database and run `flask --app app db upgrade`.

**Recovery testing** — once a month restore the latest backup into a separate test database, start the app against it, log in, and check that the dashboard totals, the ledger check ("debits equal credits") and a sample of ID documents match production. Record the date and result.

SQLite (development): copy `instance/app.db` while the app is stopped.

## Testing

```bash
pytest                      # 90 tests on an in-memory SQLite database
```

Against PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost/testdb pytest
```

Covered: registration, login/logout, password hashing, rate limiting, session
timeout, 2FA, CSRF, ID upload validation and private storage, member
dashboard, partial and full payments, remaining contribution, server-side
payment verification (including a forged M-PESA callback), idempotency,
duplicate payment protection, manual payments, reversals, ledger balance
formula, immutability, two-person approvals, insufficient-funds checks,
projects and the approval workflow, funding releases, partner updates,
expenses, revenue, profit/loss, distributions (all rule combinations,
approval, payment, reversal), permissions for every role, partner isolation,
privacy of public pages and reports, audit logging, every report in HTML/CSV/PDF,
member statements, meetings, voting, tickets, complaints, maintenance mode,
error pages, the no-JavaScript rule, and the YEDN identity (branding on every
public page, database-driven statistics, programs/opportunities publishing,
branded PDFs and notifications, no unsupported legal claims).

## Troubleshooting

| Problem | Fix |
|---|---|
| `flask` not found | Activate the virtual environment first (`venv\Scripts\activate`). |
| `ZoneInfoNotFoundError` on Windows | `pip install tzdata` (already in requirements). |
| "Set a strong SECRET_KEY" on start | You set `APP_ENV=production`; add a real `SECRET_KEY`. |
| Tables missing / `no such table` | Run `flask --app app db upgrade`. |
| Form says "Your form expired" | The CSRF token is tied to your session; reload the page and resubmit. Check the clock and that cookies are allowed. |
| Locked out after failed logins | Wait 15 minutes, or an admin can reset the password. |
| Payments page says payments are not available | Payment provider not configured; check `PAYMENT_PROVIDER` and the M-PESA variables. The sandbox is disabled in production. |
| M-PESA payment stays Pending | Check `APP_BASE_URL` is public https and matches the Daraja app; the member can press "Check status now", which asks Safaricom directly. |
| SMS/email not arriving | Without credentials they are only logged. Check provider keys, `SMS_SENDER_ID` approval, and SMTP settings. |
| Uploaded files disappear on Render | Set `FILE_STORAGE=database` (Render free plan wipes the disk on restart). |
| "Distribution rules are not configured" | A Super Admin must set them in Settings → Distribution rules. This is deliberate. |
| Ledger check warning on the dashboard | Debits ≠ credits should never happen; restore from backup or investigate recent database changes made outside the app. |
