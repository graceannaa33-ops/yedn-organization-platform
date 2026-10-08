# Put the YEDN website online (GitHub + Render) — step by step for Windows

Goal: a public link such as `https://yedn-platform.onrender.com` that testers can open,
register on, log in to and report problems from.

Everything below uses **Windows Command Prompt (CMD)**. Type one command, press Enter,
wait for it to finish, then type the next. Lines starting with `REM` are comments — do
not type them.

---

## What you will end up with

| Piece | What it is | Cost |
|---|---|---|
| GitHub repository | Online copy of your code (no passwords, no database, no uploads) | Free |
| Render **web service** `yedn-platform` | Runs the website with Gunicorn on Python 3.11.9 | Free |
| Render **PostgreSQL** `yedn-db` | The online database (members, payments, uploaded files) | Free for 30 days |

**Yes, you need the Render PostgreSQL database.** The free web service's disk is wiped
every time it restarts, so SQLite and the `uploads/` folder cannot be used online. The
`render.yaml` file creates the database for you and stores uploaded files (ID documents,
photos, project files) inside it.

Free-plan limits you should know (from [Render's free plan page](https://render.com/docs/free)):

- The site **sleeps after 15 minutes** without visitors. The next visit takes about
  **1 minute** to wake it. Tell testers to wait.
- **The free database expires 30 days after it is created** (then a 14-day grace period
  before it is deleted). Before then, upgrade it to a paid plan in Render, or the test
  data is lost. ([Render changelog](https://render.com/changelog/free-postgresql-instances-now-expire-after-30-days-previously-90))
- Free database size: 1 GB. Only one free database per Render account.
- No shell/console on the free plan — that is why the app sets itself up automatically
  when it starts (migrations, demo data, your admin account).

---

## Part 1 — Put the updated files into your project folder

1. Unzip `yedn-platform-render.zip` into your **Downloads** folder. You get a folder
   `Downloads\yedn-render-update\organization-platform`.
2. Open **Command Prompt** (press the Windows key, type `cmd`, press Enter).
3. Copy the updated files over your project. This keeps your `venv`, your local database
   (`instance`), your local uploads and your `.env`:

```bat
robocopy "%USERPROFILE%\Downloads\yedn-render-update\organization-platform" "C:\Users\User\Downloads\yedn-platform\organization-platform" /E /XD venv instance uploads __pycache__ .git /XF .env
```

> `seed.py` is replaced by the new version. It already includes your fix — `python seed.py`
> still runs the seed.

---

## Part 2 — Check it still works on your computer

```bat
cd /d C:\Users\User\Downloads\yedn-platform\organization-platform
```
```bat
venv\Scripts\activate
```
```bat
pip install -r requirements.txt
```
```bat
flask --app app db upgrade
```
(This only **adds** one new table, `stored_files`. Your local SQLite data is kept.)
```bat
python -m pytest -q
```
You should see `90 passed`. Then:
```bat
python app.py
```
Open `http://127.0.0.1:5000/` in your browser. When finished, press `Ctrl + C` in CMD.

---

## Part 3 — Put the code on GitHub

### 3.1 One-time setup

1. Install **Git for Windows** from https://git-scm.com/download/win (accept the defaults).
   Close and reopen Command Prompt afterwards.
2. Create a free account at https://github.com if you don't have one.
3. Tell Git who you are (use your own name and the email of your GitHub account):

```bat
git --version
```
```bat
git config --global user.name "Your Name"
```
```bat
git config --global user.email "you@example.com"
```

### 3.2 Create the repository on GitHub

1. Go to https://github.com/new
2. Repository name: `yedn-platform`
3. Choose **Private** (recommended).
4. Do **not** tick "Add a README", ".gitignore" or "license".
5. Click **Create repository**. Keep the page open.

### 3.3 Send your code to GitHub

```bat
cd /d C:\Users\User\Downloads\yedn-platform\organization-platform
```
```bat
git init
```
```bat
git branch -M main
```
```bat
git add .
```
```bat
git status
```

**Check the list printed by `git status` before continuing.** It must **not** contain
`.env`, `venv/`, `instance/`, any `.db` file, or files inside `uploads/` (except
`uploads/.gitkeep`). The `.gitignore` file blocks them; if you ever see them, stop and
ask for help.

```bat
git commit -m "YEDN platform ready for Render"
```

Replace `YOUR-GITHUB-USERNAME` with your GitHub username:

```bat
git remote add origin https://github.com/YOUR-GITHUB-USERNAME/yedn-platform.git
```
```bat
git push -u origin main
```

A window opens asking you to sign in to GitHub — sign in and approve. When the push
finishes, refresh the GitHub page: your files are there.

---

## Part 4 — Create the website on Render

1. Go to https://render.com and click **Get Started**. Sign up **with GitHub** (easiest).
2. In the Render dashboard click **New +** → **Blueprint**.
3. Click **Connect** next to your `yedn-platform` repository. (If it is not listed, click
   *Configure GitHub* / *Connect account* and give Render access to that repository.)
4. Render reads `render.yaml` and shows:
   - a web service **yedn-platform** (free)
   - a PostgreSQL database **yedn-db** (free)
5. Blueprint name: `yedn`. Branch: `main`.
6. Render asks you to fill in **three values** (they are secrets, so they are not in GitHub):

| Key | What to type |
|---|---|
| `DEMO_PASSWORD` | A password for the 10 demo accounts, e.g. `YednTest-2026-Kericho` (10+ characters, letters and numbers). Share it only with people who should use demo accounts. |
| `ADMIN_EMAIL` | **Your own** email — this becomes your Super Admin login |
| `ADMIN_PASSWORD` | A strong password only you know (10+ characters, letters and numbers) |

7. Click **Apply** (or **Deploy Blueprint**).
8. Click the **yedn-platform** service and open **Logs**. The first build takes about
   3–6 minutes. Success looks like:

```
Database migrations applied.
Demo data loaded. ...
Super Admin you@example.com created.
Ready.
Listening at: http://0.0.0.0:10000
==> Your service is live 🎉
```

9. Your public address is shown at the top of the service page, e.g.
   **`https://yedn-platform.onrender.com`** (Render adds a few letters if the name is
   taken). Open it — that is the link you send to testers.

### Settings created by `render.yaml` (for reference)

You don't need to type these — the Blueprint sets them. If you ever create the service by
hand instead (**New + → Web Service**), use exactly these:

| Setting | Value |
|---|---|
| Runtime / Language | Python 3 |
| Branch | `main` |
| Root directory | *(leave empty)* |
| Build command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start command | `flask --app app prepare-deploy && gunicorn wsgi:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT` |
| Instance type | Free |
| Health check path | `/healthz` |
| Region | Frankfurt (closest Render region to Kenya; web service and database must be in the same region) |

Environment variables:

| Key | Value | Why |
|---|---|---|
| `PYTHON_VERSION` | `3.11.9` | Same Python as your PC (Render's default is newer) |
| `APP_ENV` | `production` | Secure cookies, HTTPS-only settings |
| `SECRET_KEY` | *generated by Render* | Signs logins. Never share it. |
| `DATABASE_URL` | *from the yedn-db database (Internal URL)* | Connects to PostgreSQL |
| `FILE_STORAGE` | `database` | Keeps uploads safe when the disk is wiped |
| `TRUST_PROXY` | `true` | Render sits in front of the app |
| `ALLOW_SANDBOX_PAYMENTS` | `true` | Payments use the simulator — **no real money** |
| `SEED_DEMO_DATA` | `true` | Loads demo data once (never twice) |
| `SHOW_DEMO_LOGINS` | `false` | Demo password is not printed on the public login page |
| `DEMO_PASSWORD` | *you type it* | Password for demo accounts |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | *you type them* | Your own Super Admin |
| `ADMIN_NAME` | `YEDN Administrator` | Display name |
| `PAYMENT_PROVIDER` | `sandbox` | Simulated M-PESA |
| `SMS_PROVIDER` | `console` | SMS written to the logs, not sent |
| `ORGANIZATION_NAME` | `Youth Enterprise & Development Network` | Default name |

Optional later: `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`
(real emails), the M-PESA keys (real payments), `SMS_API_KEY` etc. See `.env.example`.

---

## Part 5 — First things to do on the live site

1. Open your link and log in with **ADMIN_EMAIL / ADMIN_PASSWORD**.
2. **Settings → General**: enter the official contact email, phone and address.
3. **Settings → Identity**: check the texts; add the website when you have one.
4. Try it yourself: log out, click **Join YEDN**, register a test member with a sample
   PDF/JPG, make a simulated payment (simulator PIN **1234**).

---

## Part 6 — Message to send to testers

> Hi! Please help test the YEDN website: **https://yedn-platform.onrender.com**
>
> - The first time you open it, it can take **up to 1 minute** to load (free hosting).
> - Click **Join YEDN** to create an account. **Use made-up details** and upload **any
>   sample picture or PDF** — please **do not upload your real ID**.
> - Payments are **simulated** — no real money. On the "simulated phone" use PIN **1234**.
> - Please report problems using the **Complaints** page (bottom of every page), or send
>   me: the page address, what you clicked, what you expected, and a screenshot.

---

## Part 7 — Updating the website later

After changing code on your PC (and checking it with `python app.py`):

```bat
cd /d C:\Users\User\Downloads\yedn-platform\organization-platform
```
```bat
git add .
```
```bat
git commit -m "Describe what you changed"
```
```bat
git push
```

Render notices the push and redeploys automatically (about 3–5 minutes). Database
migrations run by themselves at start-up. Data in the online database is kept.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| `git` is not recognized | Install Git for Windows, then close and reopen CMD. |
| `git push` asks for a password and fails | Use the browser sign-in window that appears; GitHub no longer accepts your account password in CMD. |
| `git status` shows `.env` or `venv` | Stop. Make sure the `.gitignore` file from this update is in the folder, then run `git rm -r --cached .` and `git add .` again. |
| Render build fails on `psycopg` or `reportlab` | Check `PYTHON_VERSION` is `3.11.9` in the service's **Environment** tab. |
| Logs show `Set a strong SECRET_KEY` | The `SECRET_KEY` variable is missing — add one in the Environment tab (Generate). |
| Logs show a database connection error | Make sure `DATABASE_URL` is the database's **Internal** URL and both are in the same region. |
| "Your form expired or was not valid" | Reload the page and submit again (it happens if the page was open while the site slept). |
| Site takes a minute to load | Normal on the free plan after 15 minutes of no visitors. |
| Admin account not created | `ADMIN_PASSWORD` must be 10+ characters with letters and numbers. Fix it in Environment → **Save, rebuild and deploy**. |
| Payments say "not available" | `ALLOW_SANDBOX_PAYMENTS` must be `true` and `PAYMENT_PROVIDER` `sandbox`. |
| Database will expire soon (Render emails you) | In Render open **yedn-db → Upgrade** to a paid plan before the 30 days end, or the data is deleted. |

Your local computer setup is unchanged: `python app.py` still uses SQLite and the
`uploads/` folder.
