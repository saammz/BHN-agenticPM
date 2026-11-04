# BNH Compliance Assistant (agenticPM)

An AI assistant that monitors Portfolio Managers' monthly reporting for BNH System 3. It reads every monthly
update, checks it against BNH's reporting rules, and uses an AI reviewer to catch problems that rules miss. It
sends routine reminders by itself and refers anything uncertain or serious to the Chief of Staff, who decides
with one click. Every action is recorded in a tamper-evident audit log.

- **Design document:** [`docs/Design-One-Pager.md`](docs/Design-One-Pager.md)
- **Functional requirements (FRD):** [`docs/FRD.md`](docs/FRD.md)

---

## Signing in

There are **no passwords**. Enter your email address on the sign-in page and a 6-digit code (plus a one-click
link) is emailed to you from `agent@mediatree.social`. The email shows the IP address, device and time of the
request. Codes expire after 10 minutes and work once.

| Account | Role | What they see |
|---|---|---|
| `usorohdev@gmail.com` | Chief of Staff | Full assistant: overview, decisions, portfolios, activity, how it works, settings, audit log |
| `usotex@gmail.com` | Portfolio manager (owns *Lagos Real Estate Fund I*) | *My compliance*: own portfolio's issues, emails, disputes |

Seven further demo portfolio managers (`adaeze.okafor@bnh.example` and others) have placeholder addresses
that cannot receive email. To sign in as one of them, the Chief of Staff can change a portfolio's manager under
**Settings → Team & access**, or add a new person with a real address.

> Locally, without a Resend key, sign-in codes are printed in the server console instead of being emailed.

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
| `TRUST_PROXY` | `1` behind a proxy/CDN (Vercel), so the real client IP is recorded. |

Everything else (operating mode, AI model, confidence thresholds, reporting rules, email delivery mode, daily
check time, team, exceptions) is set in the app under **Settings** and stored in the database.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v    # 61 tests; uses a throwaway SQLite file and never sends email
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
ui/               app (CoS), pm (portfolio manager), login, audit log, workflow diagram, bnh.css design system
data/             demo dataset loaded by `db seed`     docs/  FRD and design document     tests/  test suite
```
