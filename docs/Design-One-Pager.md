# Design: Agentic PM Compliance Agent (one-pager)

BNH · 4 Nov 2026 · v0.1 · Requirements: *FRD - Agentic PM Compliance Monitoring.md* · Code: `agenticPM/`

## 1. Purpose

The agent watches Portfolio Managers' monthly System 3 submissions and keeps them compliant without anyone chasing by hand. It finds missing, late, thin, stale or contradictory reports. It reminds PMs of fixable issues and brings to the Chief of Staff the ones that need intervention. **It acts alone only when the case is clear-cut and the action is low-impact. Everything else waits for a human.**

## 2. How it works

```
 Trigger ──► Read submissions ──► Load overrides ──► Rules engine ──► LLM agent ⇄ Tools (read-only, 1 portfolio)
 (cron 08:00 WAT,   (fail closed)     (human decisions)   (R1–R8,          │ OpenRouter tool-calling loop
  webhook, manual)                                         deterministic)  ▼
                                                   Validate & ground ──┐   or   Rules-only fallback ──┐
                                                                       ▼                              ▼
                     Audit log ◄── Send email (batched) ◄─── Policy gate ───► Review queue ──► Human (CoS)
                                   Console report ◄────────┘  execute/hold/      approve → send
                                                              suppress/dedupe    reject/dismiss → precedent ↺
```

The loop is **observe → evaluate → decide → act → record**. The LLM only *judges*. The **policy gate** is the only component that can cause an action, and every LLM claim is checked against the data before it counts.

## 3. Stack

| Layer | Prototype (`agenticPM/`) | Production |
| --- | --- | --- |
| Runtime | Python 3.9+; `psycopg` only for Postgres | Same code, in a container worker |
| LLM | **OpenRouter** chat/completions with tool calling, temperature 0, fallback model list; `MockLLM` offline | OpenRouter with a restricted provider data policy |
| Database | **Dedicated Neon Postgres** (SQLite for tests/offline) | Same, plus a sync from System 3 submissions |
| Sign-in | Passwordless emailed code/link; email shows requesting IP + device; CoS and PM roles | Same, behind HTTPS |
| Email | **Resend** for sign-in; compliance emails simulated / redirected / live by config | Resend live |
| Triggers | Built-in scheduler, HTTP webhook, CLI/UI | Cron (e.g. Vercel Cron) + System 3 submit hook |
| Observability | JSONL run traces → n8n-style canvas (CoS only) | Same, plus alerting |

## 4. Tools the agent uses

| Tool | Who calls it | Access |
| --- | --- | --- |
| `get_submission_history(months)` | LLM | Read-only, **scoped to the one portfolio** under review; PM text wrapped as `<pm_text>`, submitter emails removed |
| `get_override_history()` | LLM | Read-only; past CoS decisions used as precedents |
| `submit_assessment(rule_reviews, new_findings, summary)` | LLM (ends the loop) | Validated against a JSON schema, then every evidence item is matched to the stored record |
| `send_email`, `escalate`, `enqueue_review` | **Policy gate only** | Fixed templates; recipients from the roster and Settings, never chosen by the LLM |

## 5. Triggers

1. **Schedule:** daily at 08:00 WAT, all portfolios. Deadline-based reminders (T-7, T-1, overdue) come from this run.
2. **Event:** a webhook fires when a PM submits or edits, re-checking that portfolio straight away. An edit changes the finding's fingerprint, so issues that have been fixed clear.
3. **Manual:** the CoS or ops runs it from the canvas or the CLI, in `auto`, `assist` (approve everything) or `shadow` (dry run) mode.

Only one run executes at a time, and an action ledger makes re-runs idempotent: the same finding never produces the same email twice.

## 6. Failure modes

| Failure | Behaviour |
| --- | --- |
| Data source down, invalid or empty | **Fail closed**: abort the run, send nothing, alert ops. Missing data must never look like full compliance |
| OpenRouter error, timeout or rate limit | Retry ×3 with backoff, then fallback models, then **rules-only** for that portfolio. Judgement calls are held for a human |
| Malformed output or runaway loop | Errors fed back to the model; after 3 strikes or 6 steps, rules-only |
| Hallucinated evidence | Grounding check fails, so confidence is capped at 0.3 and the claim goes to the review queue labelled *Ungrounded* |
| Prompt injection in PM text | Rule R8 flags it; the text is isolated as data; tools are read-only and recipients fixed, so the injection can't do anything |
| Duplicate triggers | Run lock + ledger, so nothing is sent twice |
| Bad model or deploy | Kill switch (Pause), shadow mode, roll back `OPENROUTER_MODEL` |

## 7. When the agent is wrong

The design assumes the agent will make mistakes, and limits what each mistake can cost:

- **Impact-tiered autonomy.** Auto-execution is allowed only for (a) reminders from deterministic rules, (b) reminders from LLM judgements with confidence ≥ 0.75, and (c) escalations from deterministic HIGH rules. **Every LLM-originated escalation waits for the CoS**, as does any finding below 0.6 confidence, any disputed finding and any ungrounded finding.
- **The LLM can't remove facts.** If it disputes a deterministic finding, the finding stays and goes to a human.
- **Low-cost wording.** Reminders ask the PM to check, never accuse, and include a one-click dispute link.
- **Visible reasoning.** Every held item shows its evidence, the agent's note and *why it was held*.
- **Learning from overrides.** Rejections and dismissals become precedents the LLM reads on the next run. A dismissal holds until the PM changes the submission.
- **Measured.** Override, dispute, ungrounded and fallback rates come from the audit log. The agent moves from shadow to assist to auto only when the override rate stays below 10%.

## 8. How human override works

| Override | Effect | Where |
| --- | --- | --- |
| **Approve / Reject** a held action | Approve sends it now; reject records a precedent | Review tab · `agentpm.py approve/reject` |
| **Reject as false positive** / **Dismiss** | Finding suppressed for this submission version | Review tab, Overrides tab, PM dispute link · `agentpm.py dismiss` |
| **Suppress** (portfolio × rule × month, until a date, reason required) | Agent stops acting on that rule, e.g. an agreed extension | Overrides tab · `agentpm.py suppress` |
| **Severity** | Re-grades a finding before the gate | `agentpm.py severity` |
| **Pause / Resume** | Global kill switch: everything becomes shadow; detection continues | Header button · `agentpm.py pause` |

Overrides are append-only and record who, when and why. They are loaded at the start of every run, before any evaluation, and shown on the canvas as a feedback edge from *Human override* back to *Load overrides*.

## 9. Who can see what

The **CoS** signs in to the full console: canvas, review queue, PM disputes, outbox, overrides and accounts. A **PM** signs in to *My compliance*, which shows only their own portfolios' findings and emails and lets them dispute a finding. Sign-in is a one-time code emailed through Resend. The email shows the IP address and device the request came from, and an alert goes out if the sign-in completes from a different IP.

## 10. Partial implementation (delivered)

`agenticPM/` runs the full loop on mocked data: 8 portfolios, each showing one compliance case, including a planted false positive (P01) and a prompt injection (P08). `python3 agentpm.py serve` opens the workflow canvas, where nodes light up as they run, edges animate with item counts, and clicking a node shows its findings, decisions, tool calls and emails. 34 automated tests cover the rules, the loop, grounding, the policy gate, overrides, failure drills, sign-in, roles and disputes. It runs live on OpenRouter (Claude Sonnet 5.5) and Neon Postgres.
