# BNH Compliance Assistant (agenticPM)

An AI assistant that monitors Portfolio Managers' monthly reporting for BNH System 3. It reads every monthly
update, checks it against BNH's reporting rules, and uses an AI reviewer to catch problems that rules miss. It
sends routine reminders by itself and refers anything uncertain or serious to the Chief of Staff, who decides
with one click. Every action is recorded in a tamper-evident audit log.

- **Design document:** [`docs/Design-One-Pager.md`](docs/Design-One-Pager.md)
- **Functional requirements (FRD):** [`docs/FRD.md`](docs/FRD.md)

---

## Signing in

There are **no passwords** and **no sign-in emails**. Enter your email address on the sign-in page and select
**Get a sign-in code**: the 6-digit code appears on the page, already filled in, so you just select **Sign in**.
Codes expire after 10 minutes and work once. Only the accounts listed below (or added by the Chief of Staff) can
sign in; an unknown address is refused.

This is set by `AUTH_CODE_DELIVERY="screen"` (local `.env` and Vercel). To go back to emailing codes (with the
requesting IP address and device in the email, and an alert when a sign-in completes from a different IP), set it
to `email`.

### Seeded accounts

These are the accounts in the live database (https://bnh-agenticpm.vercel.app):

| Email | Name | Role | Portfolio |
|---|---|---|---|
| `usorohdev@gmail.com` | Chief of Staff | Chief of Staff | All portfolios (full app) |
| `usotex@gmail.com` | Test PM | Portfolio manager | P01 Lagos Real Estate Fund I |
| `tunde.bakare@bnh.example` | Tunde Bakare | Portfolio manager | P02 Agri-Value Chain Fund |
| `ngozi.eze@bnh.example` | Ngozi Eze | Portfolio manager | P03 Fintech Ventures II |
| `ibrahim.musa@bnh.example` | Ibrahim Musa | Portfolio manager | P04 Infrastructure Debt Fund |
| `funmi.adeyemi@bnh.example` | Funmi Adeyemi | Portfolio manager | P05 Healthcare Growth Fund |
| `chinedu.obi@bnh.example` | Chinedu Obi | Portfolio manager | P06 Renewable Energy Platform |
| `amina.yusuf@bnh.example` | Amina Yusuf | Portfolio manager | P07 Consumer Retail Holdings |
| `segun.afolabi@bnh.example` | Segun Afolabi | Portfolio manager | P08 Logistics & Ports Fund |
| `adaeze.okafor@bnh.example` | Adaeze Okafor | Portfolio manager | None (P01 reassigned to Test PM) |

Because the code is shown on screen, you can sign in as **any** of these accounts, including the `bnh.example`
demo managers, to see each portfolio manager's view.

**On a fresh database**, `db seed` creates the Chief of Staff from `SEED_COS_EMAIL` and the eight demo managers,
with P01 assigned to Adaeze Okafor. To reproduce the accounts above, run:

```bash
.venv/bin/python agentpm.py user-add "Test PM" usotex@gmail.com PM
.venv/bin/python agentpm.py assign P01 usotex@gmail.com
```

---

## Pages

| Address | Who | What it is |
|---|---|---|
| `/login` | Everyone | Sign in: enter your email; the 6-digit code is shown on the page (or emailed, if `AUTH_CODE_DELIVERY=email`). |
| `/auth/link` | Everyone | Only in email mode: opened from the one-click link in the sign-in email. Signs in only when you press **Continue**, so email scanners can't use the link up. |
| `/` | Chief of Staff | The main app (see tabs below). |
| `/` | Portfolio manager | **My compliance**: open and resolved issues for your own portfolios, your disputes, recent monthly updates, emails sent to you, and your recent sign-in activity. |
| `/dispute/<id>` | Portfolio manager | Link from a reminder email. After sign-in it opens the dispute form for that issue. |
| `/workflow` | Chief of Staff | Full-screen workflow diagram (n8n-style). Nodes light up live during a check; earlier checks can be replayed; click a node to see what it did. |
| `/audit` | Chief of Staff | Audit log: filters, search, expandable entries, one-click tamper check, live tail, CSV export. |

**Chief of Staff tabs** (at `/`):

| Tab | What it shows |
|---|---|
| Overview | Headline figures, items awaiting a decision, recent activity, status of every portfolio, **Run check now**. |
| Needs your decision | Actions held for approval and disputes from portfolio managers. Approve, decline or mark "incorrect: do not raise again". |
| Portfolios | Every portfolio with its status; click one for a side panel with issues, monthly updates, emails and history. |
| Activity | Plain-English feed of checks, emails, decisions and settings changes. |
| Workflow | The workflow diagram embedded in the app, with a link to the full view. |
| Settings | Assistant (mode, pause, daily check time), Reporting rules, AI review, Email (sender, test email, practice / test inbox / real delivery), Team & access, Exceptions, Security & history (audit log, sign-in activity), Advanced (demonstration date, technical view, system status, start over). |

---

## Setup (local)

Requirements: Python 3.9+ (no other system software). The only package is the Postgres driver.

```bash
git clone https://github.com/saammz/BHN-agenticPM.git
cd BHN-agenticPM
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then fill in the values (see below)
.venv/bin/python agentpm.py db init      # create the tables
.venv/bin/python agentpm.py db seed      # demo portfolios + the first Chief of Staff (SEED_COS_EMAIL)
.venv/bin/python agentpm.py serve        # open http://localhost:8765
```

### Environment variables (`.env`, never committed)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Neon Postgres connection string. Leave empty to use a local SQLite file (development only). |
| `OPENROUTER_API_KEY` | AI reviewer (OpenRouter). Can also be pasted in **Settings → AI review**. |
| `RESEND_API_KEY`, `EMAIL_FROM` | Email delivery (Resend) and sender, e.g. `BNH Compliance Agent <agent@mediatree.social>`. |
| `APP_URL` | Public address, used in email links (`http://localhost:8765` locally). |
| `SEED_COS_EMAIL`, `SEED_COS_NAME` | First Chief of Staff account, created by `db seed`. |
| `AUTH_SECRET`, `WEBHOOK_SECRET` | Generated automatically into `.env` on first run. **Set them explicitly on any hosted deployment.** |
| `CRON_SECRET` | Protects the daily-check endpoint on Vercel. |
| `AUTH_CODE_DELIVERY` | `screen`: show sign-in codes on the page, send no email. `email`: send them through Resend. |
| `TRUST_PROXY` | `1` behind a proxy/CDN (Vercel), so the real client IP is recorded. |

Everything else (operating mode, AI model, confidence thresholds, reporting rules, email delivery mode, daily
check time, team, exceptions) is set in the app under **Settings** and stored in the database.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v    # 63 tests; uses a throwaway SQLite file and never sends email
```

---

## Deployment (Vercel)

The repository deploys to Vercel as a Python serverless function (`api/index.py`). `vercel.json` routes every
path to it, allows checks to run for up to 5 minutes, and schedules the daily check (`/api/cron/daily`, 07:00 UTC =
08:00 WAT). On Vercel:

- run history is stored in the database (the filesystem is temporary);
- **Run check now** completes within the request rather than in the background;
- Settings changes apply to every instance immediately.

Set the same environment variables as `.env` in the Vercel project, plus `APP_URL` (the production address),
`TRUST_PROXY=1` and `CRON_SECRET`.

---

## How it works

Each check: **read monthly updates → apply exceptions → check the rules → AI review → double-check the AI's facts →
decide what to do → send emails / ask the Chief of Staff → record everything**. The *Workflow* tab shows each
step live.

- **Rules** (deadlines, missing or late updates, Red/Amber without explanation, copied explanations, Red for several
  months, "on track" despite falling cash, instructions hidden in text) always run. The AI cannot remove their findings.
- **The AI** (Claude via OpenRouter) reviews each portfolio with read-only access to that one portfolio, and must quote
  the records as evidence. Claims that don't match the records are discarded.
- **The policy gate** only acts alone on clear-cut, low-impact cases. Anything uncertain, anything serious from AI
  judgement, and anything disputed goes to the Chief of Staff.
- **Human override:** approve, decline, "incorrect: do not raise again", exceptions, pause. Decisions become precedents
  the AI reads next time. Portfolio managers can dispute findings.
- **Audit:** every sign-in, request, check, AI call, decision, email and settings change is hash-chained, append-only
  and exportable.

### Demo data (evaluated as of 4 Nov 2026)

| Portfolio | Situation | What the assistant does |
|---|---|---|
| Lagos Real Estate Fund I | Green, cash −46% (a planned investor payout) | Asks the CoS (deliberate "possible false positive") |
| Agri-Value Chain Fund | Red with "None" as the explanation; October not yet in | Reminds the manager |
| Fintech Ventures II | Green, but the update says the licence was suspended | Reminds the manager; asks the CoS before flagging for intervention |
| Infrastructure Debt Fund | September submitted late | Skipped: an extension exception is in place |
| Healthcare Growth Fund | Red three months running | Alerts the CoS |
| Renewable Energy Platform | September never submitted | Reminds the manager and alerts the CoS |
| Consumer Retail Holdings | Same explanation copied for 3 months | Reminds the manager |
| Logistics & Ports Fund | Update contains instructions aimed at the AI | Ignores them and asks the CoS to look |

### Command line

```bash
.venv/bin/python agentpm.py run [--llm mock|openrouter] [--as-of 2026-11-04]   # run a check
.venv/bin/python agentpm.py users | user-add "Name" email COS|PM | assign P01 email
.venv/bin/python agentpm.py review | approve REV-x | reject REV-x [--dismiss]
.venv/bin/python agentpm.py audit [--category llm] | audit-verify | audit-export file.csv
```

### Layout

```
api/index.py      Vercel entry point            server.py   web server: sign-in, roles, API
webapi.py         overview, activity, settings   agentpm.py  command line
agent/            rules, AI loop, policy gate, email, sign-in, audit trail, database layer
ui/               app.html (CoS), pm.html (portfolio manager), login.html, link.html, audit.html, workflow.html,
                  plain.js (plain-English labels), bnh.css (System 3 design system)
data/             demo dataset loaded by `db seed`     docs/  FRD and design document     tests/  test suite
```
