# FRD — Agentic PM Compliance Monitoring

4 Nov 2026 · Companion to *Design - Agentic PM Compliance Agent (One-Pager).md* · Code: `agenticPM/`

## Purpose

BNH needs to know, without someone chasing it by hand, whether every Portfolio Manager (PM) is meeting the monthly reporting obligations set by System 3 (Portfolio Monthly Update & CoS Dashboard). This system adds an AI agent that watches PM submissions, judges compliance, and acts. Clear-cut, low-impact cases are handled automatically. Anything uncertain or consequential goes to the Chief of Staff (CoS) for a decision.

It builds on System 3. It reads System 3's submission data and does not change it.

| Item | Value |
| --- | --- |
| Document | Functional Requirements Document (FRD) |
| System | BNH Agentic PM Compliance Monitor |
| Version | 0.4 (draft): adds the page map, plain-English CoS app with Settings, seeded accounts and Vercel hosting. 0.3 added sign-in, PM portal, Neon Postgres, Resend and the tamper-evident audit trail |
| Live | https://bnh-agenticpm.vercel.app (GitHub `saammz/BHN-agenticPM`, deploys on push to `main`) |
| Depends on | System 3 FRD (BR-1 to BR-9, D4 Settings) |
| LLM gateway | OpenRouter (OpenAI-compatible tool calling) |
| Database | Dedicated Neon Postgres, separate from System 3 |
| Email | Resend (sign-in codes, alerts, compliance emails) |
| Audience | Product, engineering, CoS office, risk/compliance |

## Scope

**In scope**

- A1: Trigger mechanism: daily schedule, a webhook on each PM submission, and manual runs
- A2: Read-only data access to submissions, the portfolio roster and settings (mocked in this release)
- A3: A deterministic compliance rules engine
- A4: An LLM agent (OpenRouter) that uses tools to review rule findings and find issues rules can't see
- A5: A policy gate that decides, for each proposed action, whether to execute it, hold it for a human, or suppress it
- A6: Actions: PM reminder email, CoS escalation email, console run report, human review queue
- A7: Human override: approve or reject held actions, dismiss findings, suppress rules, re-grade severity, global pause
- A8: Audit log and run traces
- A9: A workflow visualiser (n8n-style) that shows each node and edge as it runs
- A10: Passwordless sign-in by emailed code or link (Resend), showing the requesting IP address; CoS and PM roles
- A11: A PM portal: own portfolios' findings, emails received, and disputes the CoS decides
- A12: Persistent storage in a dedicated Neon Postgres database
- A13: A plain-English web app for non-technical users: CoS app with tabs and a Settings page, PM portal, audit log and workflow pages, in the System 3 visual style

**Out of scope (this release)**

- Writing to System 3 data. The agent never edits, deletes or re-rates a submission
- Reading System 3's live database (decision 4 Nov 2026: the agent has its own Neon database, loaded with demo data; a System 3 sync is a future enhancement)
- Passwords, SSO and multi-factor beyond the emailed code

## Users and roles

| Role | Interaction | Can do |
| --- | --- | --- |
| Portfolio Manager (PM) | Receives reminders; signs in to *My compliance* | See findings and emails for **their own portfolios only**; dispute a finding |
| Chief of Staff (CoS) | Receives escalations; owns the review queue; signs in to the full console | Approve or reject held actions; uphold or deny PM disputes; dismiss findings; suppress rules; re-grade severity; pause or resume the agent; manage accounts and PM ↔ portfolio ownership |
| Ops / engineering | Runs and monitors the service | Set config and thresholds; read run traces and audit; respond to failure alerts |
| Agent (system actor) | Runs on a trigger | Read data; propose findings and actions; execute **only** what the policy gate allows |

## Compliance rules

The rules are deterministic. They always run and cost nothing, and the LLM cannot delete what they find. Thresholds come from Settings.

| ID | Rule | Logic (defaults) | Severity | Default action |
| --- | --- | --- | --- | --- |
| R1 | MISSING_SUBMISSION | No submission and the deadline (day 10 of the next month, 23:59 WAT) has passed | HIGH | Remind PM + escalate to CoS |
| R2 | DUE_SOON | No submission and the deadline is ≤ 7 days away (reminders at T-7 and T-1) | LOW | Remind PM |
| R3 | LATE_SUBMISSION | `submitted_at` is after the deadline (System 3 BR-7) | MEDIUM | Remind PM |
| R4 | WEAK_BLOCKER | RAG is Amber or Red and the blocker is "None"/N/A or under 15 characters | MEDIUM | Remind PM |
| R5 | STALE_BLOCKER | Amber/Red blocker identical to last month's (ignoring whitespace and case) | LOW | Remind PM |
| R6 | PERSISTENT_RED | Red for ≥ 3 consecutive months | HIGH | Escalate to CoS |
| R7 | CASH_RAG_INCONSISTENT | Green with negative cash, or cash down ≥ 40% month on month. *A signal only: needs LLM or human judgement* | MEDIUM | Remind PM if confirmed |
| R8 | SUSPICIOUS_CONTENT | Blocker contains instruction-like text aimed at an AI (prompt injection) | MEDIUM | Hold for human review |

The LLM may add findings of these types, and only with grounded evidence (FR-D.5):

| ID | Type | Example |
| --- | --- | --- |
| L1 | NARRATIVE_RAG_MISMATCH | RAG is Green, but the blocker says "regulator suspended the licence; operations halted" |
| L2 | BLOCKER_QUALITY | The blocker is long but names no cause, owner or next step |
| L3 | DATA_ANOMALY | Values that are implausible given history |

## Functional requirements

**A1 — Triggers**

1. FR-A.1 A scheduled run every day at 08:00 WAT evaluates all active portfolios.
2. FR-A.2 A webhook (`POST /api/webhook/submission {portfolio_id}`) runs the agent for one portfolio when a PM creates or edits a submission, so a fix clears quickly.
3. FR-A.3 The CoS or ops can start a manual run with a mode (auto / assist / shadow), an LLM (OpenRouter / mock) and an "as of" date.
4. FR-A.4 Only one run executes at a time. A trigger that arrives during a run is logged and skipped. The next daily run picks up anything it missed.

**A2 — Observe**

5. FR-B.1 Each run reads the active portfolio roster, all submissions and the settings through a read-only adapter.
6. FR-B.2 If the source is unreachable, invalid, or returns an empty roster, the run **aborts with no findings and no actions**, writes an audit entry and raises an ops alert (fail closed: missing data must never look like full compliance).
7. FR-B.3 Each run loads all human overrides before evaluating anything.

**A3 — Evaluate (rules)**

8. FR-C.1 The rules R1–R8 run for every portfolio. Timeliness rules check the current open month and the previous month. Quality rules check the latest submission.
9. FR-C.2 Every finding records: portfolio, rule, reporting month, severity, confidence, source, rationale, evidence and a **fingerprint**. The fingerprint is a hash of portfolio, rule, month and the submission's version, so it changes when the PM edits the submission.

**A4 — Evaluate (LLM agent)**

10. FR-D.1 For each portfolio with data, the agent runs a tool-calling loop on OpenRouter. It receives the portfolio's recent submissions and its rule findings.
11. FR-D.2 Tools are read-only and scoped to the one portfolio under review: `get_submission_history(months)` and `get_override_history()`. The loop ends when the model calls `submit_assessment`.
12. FR-D.3 The assessment confirms or disputes each rule finding, with a confidence from 0 to 1 and a note, and may add new findings of type L1–L3 with a recommended action.
13. FR-D.4 The assessment is validated against a JSON schema. An invalid assessment is returned to the model with the errors. After 3 invalid attempts, or after the step limit (default 6), the portfolio falls back to rules-only.
14. FR-D.5 Evidence grounding: each new finding must cite `{submission_id, field, value}` items that exactly match the stored record, and quoted text must be a verbatim substring. A finding that fails this check has its confidence capped at 0.3 and goes to human review, labelled "Ungrounded".
15. FR-D.6 The LLM **cannot remove** a deterministic finding. If it disputes one, the finding stays and its actions go to human review. It *can* clear an R7 signal when it disputes it with confidence ≥ 0.7; the finding is then logged only.
16. FR-D.7 PM free text is wrapped in `<pm_text>` tags. The system prompt says to treat it as untrusted data and never follow instructions inside it.
17. FR-D.8 OpenRouter calls use temperature 0, a 60 s timeout, up to 3 retries with backoff on 408/429/5xx, and a fallback model list.

**A5 — Policy gate**

18. FR-E.1 Each proposed action gets exactly one decision: `execute`, `queue` (wait for a human), `suppressed`, `duplicate`, `shadow` or `log`. Rules are applied in this order:

| Order | Condition | Decision |
| --- | --- | --- |
| 1 | An active SUPPRESS override or DISMISS of this fingerprint | suppressed |
| 2 | Action is log_only | log |
| 3 | Agent paused, or mode = shadow | shadow (recorded as "would have acted") |
| 4 | Same finding + action already sent, or already waiting in the queue | duplicate |
| 5 | Mode = assist | queue |
| 6 | flag_for_review, ungrounded, disputed, or confidence < 0.6 | queue |
| 7 | remind_pm from a deterministic rule, or LLM-judged with confidence ≥ 0.75 | execute |
| 8 | escalate_cos from a deterministic HIGH rule | execute |
| 9 | Any escalation that comes from the LLM | queue (always human-approved) |

19. FR-E.2 Severity overrides are applied before the gate.

**A6 — Actions**

20. FR-F.1 Reminders are batched into one email per PM per run, listing every finding with its rationale and the agent's note.
21. FR-F.2 Escalations are batched into one CoS digest per run.
22. FR-F.3 Every PM email has a dispute link that opens the override screen with the finding's fingerprint filled in.
23. FR-F.4 Recipients come only from the roster and Settings. The LLM cannot choose a recipient, and email bodies use fixed templates (rationale and notes are length-capped).
24. FR-F.5 Each run prints a console report: every decision, its reason and its confidence.
25. FR-F.6 Sent actions go into an action ledger keyed by fingerprint + action, so re-runs never send the same message twice.

**A7 — Human review and override**

26. FR-G.1 The review queue shows each held action: the finding, evidence, the agent's note, **why it was held**, the proposed action and recipient.
27. FR-G.2 The CoS can *Approve & send*, *Reject*, or *Reject as false positive* (which also dismisses the finding), with an optional note.
28. FR-G.3 The CoS can add a SUPPRESS override (portfolio or `*`, rule or `*`, month or `*`, end date and a required reason), a DISMISS by fingerprint, or a SEVERITY change.
29. FR-G.4 A global **Pause** (kill switch) turns every run into shadow mode until it is resumed. Detection and logging continue.
30. FR-G.5 Every override records who made it, when, and why. Overrides are append-only, and a RESUME cancels an earlier PAUSE.
31. FR-G.6 Past overrides are given to the LLM as precedents (FR-D.2), so its later judgements follow the CoS's earlier decisions.
32. FR-G.7 A dismissal applies to one fingerprint. If the PM edits the submission, the fingerprint changes and the agent judges it again.

**A8 — Audit and observability**

33. FR-H.1 Every action is written to a single **audit trail**, recording who (actor, role, account id), from where (IP address, device), what (event, target, outcome, details) and when. It covers:

| Category | Events |
| --- | --- |
| auth | code requested (incl. unknown emails), verify failed, rate limited, signed in (code or link), signed out |
| access | every 401/403: who tried which endpoint |
| http | every state-changing request, with status and origin |
| run / data / rules | run requested, started (trigger, who), data loaded, overrides loaded, rules evaluated, run completed or aborted (with duration) |
| llm | each assessment: provider, model, steps, tool calls, tokens, latency, verdicts; fallbacks; ungrounded claims |
| decision / review | every policy decision with reason, evidence and confidence; items held; approvals and rejections |
| email | every email: recipient, kind, delivery mode, Resend id, failures (sign-in codes redacted) |
| override / dispute | every override; disputes opened and decided |
| user / portfolio | accounts created, deactivated or reactivated; PM ↔ portfolio changes (before and after) |
| webhook / system / audit | webhooks received or rejected; server start; reset; audit log viewed, exported and verified |

FR-H.1a The trail is **tamper-evident**: each entry stores a gap-free sequence number, its canonical payload, the previous entry's hash, and SHA-256(previous hash + payload). Verification reports the first altered or missing entry.
FR-H.1b The trail is **append-only**: database triggers reject UPDATE, DELETE and TRUNCATE, and *Reset* never clears it (a reset is itself audited).
FR-H.1c The CoS has an **Audit log** page with category chips, search, actor, outcome, date and run filters, expandable entries (full details, device, hashes), one-click chain verification, live tail and CSV export (spreadsheet-formula-safe). The CLI has `audit`, `audit-verify` and `audit-export`.
FR-H.1d PMs see their own recent sign-in activity (IP and device) in *My compliance*.
34. FR-H.2 Every node start, end and error, and every edge traversal, is written to the run trace, along with LLM steps and token usage. Locally the trace is `state/runs/<run_id>.jsonl`; on Vercel (no lasting disk) it is the `run_events` table.

**A9 — Workflow visualiser**

35. FR-I.1 A canvas shows the workflow as nodes: Trigger → Read submissions → Load overrides → Rules engine → LLM agent ⇄ Agent tools → Validate & ground / Rules-only fallback → Policy gate → Send email / Console report / Review queue → Audit log, plus Human override with its paths back into the flow.
36. FR-I.2 During a run, nodes show their state (running / done / error / fallback) and how many times they ran. Edges animate as data passes and show item counts.
37. FR-I.3 Clicking a node shows what it did in that run: findings, decisions with reasons, tool calls and results, the LLM assessment, and emails.
38. FR-I.4 Past runs can be reopened and replayed. The canvas is available full-screen at `/workflow` and embedded in the CoS app's **Workflow** tab. Labels and the node inspector use plain English; technical detail is folded away.

**A10 — Authentication**

39. FR-J.1 Every page and API needs a signed-in session, except the sign-in pages and the webhook (which needs the `X-Webhook-Secret` header).
40. FR-J.2 Sign-in is passwordless. The user enters their email, and Resend delivers a 6-digit code and a one-click link, valid for 10 minutes and usable once. Requesting a new code invalidates the old one.
41. FR-J.3 The sign-in email states the **IP address, device and time** of the request, and what to do if it wasn't them. The sign-in page shows the same IP so the user can compare.
42. FR-J.4 If a sign-in completes from a different IP than the one that requested the code (e.g. the link opened on a phone), the user is emailed an alert showing both IPs.
43. FR-J.5 The one-click link opens a confirmation page and only signs in on a button press, so email security scanners can't use it up.
44. FR-J.6 Protections: codes and link tokens stored only as HMAC-SHA256 hashes; 5 wrong guesses burn a code; at most 5 code requests per email and 20 per IP per 15 minutes; 25 failed verifications per IP per 15 minutes; the same response whether or not the email has an account.
45. FR-J.7 Sessions use a random 256-bit token in an HttpOnly, SameSite=Lax cookie (Secure on https), stored only as a hash. They expire after 30 minutes idle or 12 hours. Sign-out and deactivation revoke them on the server.
46. FR-J.8 State-changing requests must be JSON, and a cross-site Origin is rejected (CSRF). Responses carry CSP, X-Frame-Options SAMEORIGIN (so the app can embed its own workflow page, and no other site can) and Referrer-Policy no-referrer.
47. FR-J.9 Every sign-in event (requested, failed, rate-limited, signed in, signed out) is logged with IP and user agent and shown to the CoS under **Settings → Security & history**.
48. FR-J.10 Only accounts created by the CoS can sign in. There is no self-registration.

**A11 — PM portal and disputes**

49. FR-K.1 A PM sees only the portfolios assigned to them: open and resolved findings with the agent's decision, recent submissions, and the agent's emails to them.
50. FR-K.2 A PM can dispute an open finding with an explanation (10–2000 characters). The CoS is emailed. Links in reminder emails go straight to the dispute form after sign-in.
51. FR-K.3 The CoS upholds or denies each dispute (a reason is required to deny). Upholding dismisses the finding and closes any held actions for it. Either way, the PM is emailed the outcome and it becomes a precedent for the LLM.
52. FR-K.4 The CoS adds and deactivates accounts and assigns each portfolio to a PM. A portfolio with no active PM never emails anyone; its reminders are held for review.

**A12 — Storage and email delivery**

53. FR-L.1 All agent state is in the dedicated Neon Postgres database: users, portfolios, submissions, settings, sessions, sign-in codes and events, findings, overrides, review items, disputes, action ledger, email log and audit. Run traces are JSONL files locally and the `run_events` table on Vercel. Settings changed in the app are stored in `app_config` and override `.env` defaults.
54. FR-L.2 If the database is unreachable, the run aborts and sends nothing (FR-B.2).
55. FR-L.3 Sign-in emails always go through Resend. Compliance emails follow `AGENT_EMAIL_DELIVERY`: `simulated` (logged only), `redirect` (all sent to one test inbox) or `resend` (real recipients).
56. FR-L.4 Every email is logged with its delivery mode, Resend id and status. Sign-in codes are never stored in the log.

**A13 — Pages and navigation**

57. FR-M.1 The app uses everyday language throughout. Rule ids, decision codes and model names are translated (for example `MISSING_SUBMISSION` → "Monthly update is overdue", `queue` → "Waiting for you"); technical detail is only in the workflow inspector, the audit log and **Settings → Advanced**.
58. FR-M.2 Pages:

| Address | Who | Purpose |
| --- | --- | --- |
| `/login` | Everyone | Enter email, then the 6-digit code. Shows the visitor's IP to compare with the email |
| `/auth/link` | Everyone | Confirmation page for the one-click link (FR-J.5) |
| `/` | CoS | The CoS app (FR-M.3) |
| `/` | PM | *My compliance* (FR-K.1): open and resolved issues, disputes, recent monthly updates, emails received, recent sign-in activity |
| `/dispute/<fingerprint>` | PM | Link from a reminder email; opens the dispute form for that issue after sign-in |
| `/workflow` | CoS | Full-screen workflow canvas (A9) |
| `/audit` | CoS | Audit log (FR-H.1c) |
| `/api/cron/daily` | Vercel Cron | Daily check; needs `Authorization: Bearer <CRON_SECRET>` |

A PM who opens a CoS page or API gets 403 (pages redirect to `/`). Anyone signed out is sent to `/login`.

59. FR-M.3 The CoS app has six tabs:

| Tab | Content |
| --- | --- |
| Overview | Headline figures, items awaiting a decision, recent activity, every portfolio's status, last check time and **Run check now** |
| Needs your decision | Held actions and PM disputes, each with the reason in plain English; approve, decline, or "incorrect: do not raise again"; recently decided items |
| Portfolios | Every portfolio and its status; a side panel per portfolio with its issues, monthly updates, emails and history |
| Activity | Plain-English feed of checks, emails, decisions and settings changes |
| Workflow | The embedded workflow canvas (FR-I), with a link to `/workflow` |
| Settings | See FR-M.4 |

60. FR-M.4 **Settings** holds everything that was previously configuration, so the system is plug-and-play:
    - *Assistant*: operating mode (Automatic / Ask me first / Practice), daily check on/off and time (WAT), pause.
    - *Reporting rules*: deadline day, reminder window, months of Red before alerting, cash-drop threshold.
    - *AI review*: on/off, model, confidence thresholds, OpenRouter key (replace and test).
    - *Email*: sender, Resend key (replace and send a test), delivery mode (practice / one test inbox / real recipients).
    - *Team & access*: add and deactivate people, assign each portfolio to a manager.
    - *Exceptions*: active exceptions and "do not raise again" decisions, with removal.
    - *Security & history*: link to the audit log, recent sign-in activity.
    - *Advanced*: demonstration date, technical view, system status (database test), start over (reset demo state; the audit trail is kept).
61. FR-M.5 Every settings change is validated on the server, applies to every running instance at once, and is written to the audit trail with old and new values. Keys are write-only: the page shows only whether a key is set.
62. FR-M.6 Pages follow the System 3 design: navy header with gold rule, IBM Plex Sans, RAG status pills, NGN amounts and WAT times; usable on a phone.

**A14 — Seeded accounts**

63. FR-N.1 `db seed` creates the first CoS from `SEED_COS_EMAIL`, eight demo PMs (from `data/portfolios.json`), the eight portfolios and their submissions. It is idempotent.
64. FR-N.2 Accounts in the live database (4 Nov 2026):

| Email | Name | Role | Portfolio |
| --- | --- | --- | --- |
| `usorohdev@gmail.com` | Chief of Staff | CoS | All (full app) |
| `usotex@gmail.com` | Test PM | PM | P01 Lagos Real Estate Fund I |
| `tunde.bakare@bnh.example` | Tunde Bakare | PM | P02 Agri-Value Chain Fund |
| `ngozi.eze@bnh.example` | Ngozi Eze | PM | P03 Fintech Ventures II |
| `ibrahim.musa@bnh.example` | Ibrahim Musa | PM | P04 Infrastructure Debt Fund |
| `funmi.adeyemi@bnh.example` | Funmi Adeyemi | PM | P05 Healthcare Growth Fund |
| `chinedu.obi@bnh.example` | Chinedu Obi | PM | P06 Renewable Energy Platform |
| `amina.yusuf@bnh.example` | Amina Yusuf | PM | P07 Consumer Retail Holdings |
| `segun.afolabi@bnh.example` | Segun Afolabi | PM | P08 Logistics & Ports Fund |
| `adaeze.okafor@bnh.example` | Adaeze Okafor | PM | None (P01 reassigned to Test PM) |

There are no passwords (FR-J.2). Only the two Gmail accounts can receive a sign-in code; `bnh.example` is a reserved domain that cannot receive email. Compliance emails are in practice mode (`simulated`), so no demo PM is ever emailed.

## Incorrect assessments

The agent will sometimes be wrong. The design makes errors cheap, visible and correctable, and it does not depend on the model being accurate.

| Error | Example | Containment | Correction |
| --- | --- | --- | --- |
| False positive, low impact | Reminder about a cash drop that was a planned distribution | Reminders are neutral requests to check, with a dispute link. Uncertain judgement calls (< 0.75) are held, not sent | PM disputes, or the CoS rejects it as a false positive. A DISMISS stops it coming back for that submission version. The decision becomes a precedent |
| False positive, high impact | LLM escalates a healthy portfolio to the CoS | LLM-originated escalations **always** wait for a human (FR-E.1, row 9) | Reject. The escalation is never sent |
| Hallucinated evidence | LLM quotes text the PM never wrote | Grounding check (FR-D.5) caps confidence at 0.3 and holds it | Reviewer sees "Ungrounded"; ops tracks the ungrounded rate per model |
| False negative (rules) | A rule misses a case | The LLM layer adds L1–L3 findings | CoS raises it; a rule is added or tuned |
| False negative (LLM) | LLM disputes a real missing submission | The LLM cannot remove a deterministic finding (FR-D.6); the dispute goes to a human | — |
| Systematic drift | New model is over-eager | Shadow mode before each change; override-rate metric | Pause, roll back `OPENROUTER_MODEL`, raise thresholds |

**Quality metrics** (computed from the audit log and overrides): override rate (rejected or dismissed ÷ acted), dispute rate, ungrounded rate, fallback rate, held-queue age. Target before moving to auto mode: override rate < 10% over 2 reporting cycles.

## Failure modes

| Failure | Detection | Behaviour |
| --- | --- | --- |
| Data source down, invalid or empty roster | Adapter error or validation | Abort the run, send nothing, audit it, alert ops (fail closed) |
| OpenRouter 429/5xx/timeout | HTTP status, timeout | Retry ×3 with backoff, then fallback models, then rules-only for that portfolio. Judgement calls are held |
| No API key | Client initialisation | Rules-only fallback; shown in the visualiser header |
| Malformed or invalid assessment | Schema validator | Errors sent back to the model; after 3 strikes, rules-only |
| Loop runs away | Step counter | Hard stop at 6 steps; rules-only |
| Hallucinated evidence | Grounding check | Confidence capped at 0.3; held |
| Prompt injection in PM text | R8 regex + `<pm_text>` isolation + fixed recipients | Flagged for review; the instruction has no effect |
| Duplicate or overlapping triggers | Run lock + action ledger | Skip; never double-send |
| Email provider failure (production) | Provider error | Retry from the outbox. Not recorded in the ledger until sent |
| Bad deploy or bad model | Override-rate spike | Pause (kill switch), switch to shadow, roll back |
| Clock or timezone errors | All deadlines in WAT; `--as-of` for replay | Deterministic, testable deadlines |

## Data model

| Entity | Key attributes |
| --- | --- |
| Finding | fingerprint, portfolio_id, rule_id, reporting_month, severity, confidence, source (rule / rule+llm / llm / fallback), rationale, evidence[], llm_note, grounded, disputed, data_version |
| ActionDecision | action_type (remind_pm / escalate_cos / flag_for_review / log_only), recipient, decision, reason, action_fingerprint |
| ReviewItem | id, status (pending / approved / rejected), run_id, finding, action, held reason, decided_by, decided_at, note |
| Override | id, type (SUPPRESS / DISMISS / SEVERITY / PAUSE / RESUME / ACTION_APPROVED / ACTION_REJECTED), scope fields, until, reason, by, created_at |
| ActionLedger | action_fingerprint, run_id, message_id, ts, approved_by |
| AuditEntry | seq, ts, category, event, outcome, actor, actor_role, actor_id, ip, user_agent, target_type, target_id, run_id, summary, data, payload, prev_hash, hash |
| RunTrace | run_id, seq, node, status, data |
| User | id, name, email (unique, lower-case), role (COS / PM), status, last_login_at, last_login_ip |
| Portfolio | id, name, pm_user_id, active |
| LoginCode | user_id, code_hash, link_hash, ip, user_agent, attempts, expires_at, used_at |
| AuthSession | token_hash, user_id, ip, user_agent, expires_at, last_seen_at, revoked_at |
| AuthEvent | ts, event, email, user_id, ip, user_agent |
| Dispute | id, fingerprint, portfolio_id, raised_by, message, status (OPEN / UPHELD / DENIED), decided_by, decision_note |
| EmailLog | id, to, intended_to, subject, kind, delivery, provider_id, status, error |

All are tables in the dedicated Neon Postgres database, except RunTrace (JSONL files). With no `DATABASE_URL` the same schema runs on SQLite for development and tests.

## Non-functional requirements

| ID | Category | Requirement |
| --- | --- | --- |
| NFR-1 | Safety | The agent has no write access to System 3 data. Its only side effects are emails to roster/Settings addresses and review-queue entries |
| NFR-2 | Security | API key in environment/secret store only, never logged. Tools follow least privilege (one portfolio per loop) |
| NFR-3 | Privacy | Only the fields needed are sent to OpenRouter (no email addresses). Restrict OpenRouter to providers that do not retain or train on prompts |
| NFR-4 | Reliability | A failure in one portfolio's LLM loop does not stop the others. Runs are idempotent |
| NFR-5 | Cost | ≤ 6 LLM calls per portfolio per run; temperature 0; max 1,500 output tokens. Usage logged per step |
| NFR-6 | Performance | A daily run of 50 portfolios finishes in < 5 minutes |
| NFR-7 | Auditability | Every decision can be traced to its rule, evidence, model, confidence and the human who overrode it |
| NFR-8 | Localisation | WAT time zone and NGN amounts, as in System 3 |
| NFR-9 | Access control | Role checks on the server for every route. PMs can never read another portfolio's data, including by guessing identifiers |
| NFR-10 | Secrets | Database URL, API keys, AUTH_SECRET and WEBHOOK_SECRET only in `.env` (mode 600, git-ignored); none are logged |

## Acceptance criteria

Items AC-1 to AC-10 are covered by `agenticPM/tests/test_agent.py` and the visualiser demo. The full suite (`tests/`) has 61 tests.

- [x] AC-1 A run reads the mocked data, evaluates all 8 portfolios, and sends 5 batched simulated emails, holding 3 actions for review (as of 2026-11-04).
- [x] AC-2 Re-running immediately sends nothing (idempotency).
- [x] AC-3 The planted false positive (P01) is held. Rejecting it as a false positive means later runs suppress it.
- [x] AC-4 The LLM-originated escalation (P03) is held until the CoS approves it; approval sends it.
- [x] AC-5 The P04 late submission is suppressed by the seeded extension override.
- [x] AC-6 Prompt-injection text (P08) is flagged, and no email goes to the address it names.
- [x] AC-7 With the data source down, the run aborts and nothing is sent.
- [x] AC-8 With OpenRouter unavailable, the run completes rules-only and judgement calls are held.
- [x] AC-9 An LLM finding with fabricated evidence is downgraded and held. An LLM dispute of a deterministic rule is held, not dropped.
- [x] AC-10 Pause and shadow mode stop all sends; assist mode holds every action.
- [x] AC-11 With a real OpenRouter key, a live run completes with the same safety properties (4 Nov 2026, `anthropic/claude-sonnet-5.5`: 8/8 portfolios assessed, all evidence grounded, prompt injection ignored, ~47k tokens per full run).
- [x] AC-13 Sign-in works by emailed code and by link. The email shows the requesting IP and device; codes are single-use; 5 wrong guesses burn a code; rate limits apply; unknown emails get the same response (`tests/test_auth_server.py`).
- [x] AC-14 A sign-in from a different IP than the request triggers an alert email showing both IPs.
- [x] AC-15 A PM sees only their own portfolio and gets 403/404 on CoS APIs and on other portfolios' findings.
- [x] AC-16 A PM dispute upheld by the CoS dismisses the finding, and the next run suppresses it.
- [x] AC-17 The agent runs end to end on the Neon database.
- [x] AC-18 Sign-in email delivered through Resend from `agent@mediatree.social` (domain verified).
- [x] AC-19 A full run writes run, data, rules, LLM (one per portfolio), decision (one per action), email, review and completion events; all carry the run id (`tests/test_audit.py`).
- [x] AC-20 Altering or deleting any audit entry is detected by verification at the right sequence number; the database refuses UPDATE and DELETE on the trail.
- [x] AC-21 Sign-in codes appear in neither the email log nor the audit trail.
- [x] AC-22 Deployed on Vercel: unauthenticated `/` redirects to `/login`, CoS-only pages return 403 to a PM, the daily-check and webhook endpoints reject requests without their secrets, and a webhook-triggered check stores its run trace in Neon.
- [x] AC-23 Settings changes made in the app take effect without a restart and are audited (`tests/test_auth_server.py`, `SettingsTests`).
- [ ] AC-12 The CoS office signs off the rule thresholds and email wording.

## Rollout

1. **Shadow** (2 reporting cycles): the agent runs daily and sends nothing; the CoS reviews "would have acted" decisions.
2. **Assist** (1–2 cycles): every action waits for approval. Track the override rate.
3. **Auto**: deterministic reminders and escalations send automatically. Judgement calls and LLM escalations still wait for a human.

## Assumptions and open questions

**Assumptions**

- System 3 data (D1/D4) is the source of truth, and its deadline setting is the one that applies.
- One PM is responsible for each portfolio, and the roster holds their email address.
- The CoS is the single reviewer. Delegation is a future enhancement.

**Open questions**

- [ ] Confirm the thresholds: reminder window (7 days), persistent Red (3 months), cash drop (40%), confidence floors (0.6 / 0.75).
- [ ] Should PMs be copied on CoS escalations about their portfolio?
- [ ] Which OpenRouter model and data policy does BNH approve?
- [ ] Should LATE_SUBMISSION notify the PM, or be logged only?
