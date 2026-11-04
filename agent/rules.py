"""Deterministic compliance rules. These always run, are cheap and explainable, and the LLM
cannot delete their findings — it can only confirm, dispute (-> human), or add new ones."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from .datasource import Snapshot
from .models import Finding
from .timeutil import days_until, deadline_for, fmt_month, month_add, month_of, parse_ts

WEAK_BLOCKERS = {"none", "n/a", "na", "nil", "-", "no blocker", "nothing", "tbd", "."}
INJECTION_RE = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard (all |the )?(previous |prior )?instructions"
    r"|system prompt|you are now|mark (this|the) portfolio as compliant|<\s*/?\s*(system|assistant)\b",
    re.IGNORECASE,
)

RULE_CATALOGUE = {
    "MISSING_SUBMISSION": "No submission after the deadline",
    "DUE_SOON": "Submission due within the reminder window",
    "LATE_SUBMISSION": "Submitted after the deadline",
    "WEAK_BLOCKER": "Amber/Red status without a substantive blocker",
    "STALE_BLOCKER": "Amber/Red blocker copied unchanged from last month",
    "PERSISTENT_RED": "Persistent Red status",
    "CASH_RAG_INCONSISTENT": "Green status despite negative or sharply falling cash (needs judgement)",
    "SUSPICIOUS_CONTENT": "Free text contains instruction-like content aimed at an AI",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".")


def _version(sub: Dict[str, Any]) -> str:
    return f"{sub['id']}@{sub.get('updated_at') or sub['submitted_at']}"


def evaluate_portfolio(snap: Snapshot, p: Dict[str, Any], as_of: datetime) -> List[Finding]:
    cfg = snap.settings
    pid, name = p["id"], p["name"]
    out: List[Finding] = []

    def add(rule, month, severity, conf, rationale, evidence, actions, version, judgement=False):
        out.append(Finding(pid, name, rule, month, severity, conf, "rule", RULE_CATALOGUE[rule],
                           rationale, evidence, actions, version, needs_judgement=judgement))

    current = month_of(as_of)
    window = [month_add(current, -2), month_add(current, -1)]  # last closed-ish + currently open month

    # --- Timeliness (both months in the window)
    for m in window:
        dl = deadline_for(m, int(cfg["deadline_day"]))
        sub = snap.get(pid, m)
        if sub is None:
            if as_of > dl:
                add("MISSING_SUBMISSION", m, "HIGH", 1.0,
                    f"The {fmt_month(m)} update hasn't been submitted. It was due on {dl:%-d %B %Y}.",
                    [{"reporting_month": m, "deadline": dl.isoformat(), "submission": None}],
                    ["remind_pm", "escalate_cos"], "none")
            else:
                left = days_until(dl, as_of)
                if left <= float(cfg["reminder_window_days"]):
                    stage = "T-1" if left <= 1.5 else "T-7"
                    add("DUE_SOON", m, "LOW", 1.0,
                        f"The {fmt_month(m)} update hasn't arrived yet. It's due on {dl:%-d %B} ({left:.0f} days left).",
                        [{"reporting_month": m, "deadline": dl.isoformat(), "days_left": round(left, 1)}],
                        ["remind_pm"], f"none:{stage}")
            continue
        submitted = parse_ts(sub["submitted_at"])
        if submitted > dl:
            late_by = days_until(submitted, dl)
            add("LATE_SUBMISSION", m, "MEDIUM", 1.0,
                f"The {fmt_month(m)} update came in {late_by:.0f} day{'s' if round(late_by) != 1 else ''} after the deadline.",
                [{"submission_id": sub["id"], "field": "submitted_at", "value": sub["submitted_at"]}],
                ["remind_pm"], _version(sub))

    # --- Content quality (latest submission in the window only)
    latest = next((snap.get(pid, m) for m in reversed(window) if snap.get(pid, m)), None)
    if latest:
        m, rag, blocker = latest["reporting_month"], latest["rag_status"], latest["execution_blocker"]
        prev = snap.get(pid, month_add(m, -1))
        ev_blocker = {"submission_id": latest["id"], "field": "execution_blocker", "value": blocker}
        ev_rag = {"submission_id": latest["id"], "field": "rag_status", "value": rag}

        if rag in ("AMBER", "RED") and (_norm(blocker) in WEAK_BLOCKERS or len(_norm(blocker)) < 15):
            add("WEAK_BLOCKER", m, "MEDIUM", 1.0,
                f"Marked {'Red (off track)' if rag == 'RED' else 'Amber (at risk)'}, but the explanation only says “{blocker.strip()}”.",
                [ev_rag, ev_blocker], ["remind_pm"], _version(latest))

        if rag in ("AMBER", "RED") and prev and _norm(prev["execution_blocker"]) == _norm(blocker):
            add("STALE_BLOCKER", m, "LOW", 1.0,
                f"The explanation is word-for-word the same as {fmt_month(prev['reporting_month'])}, so there's no sign of progress.",
                [ev_blocker, {"submission_id": prev["id"], "field": "execution_blocker", "value": prev["execution_blocker"]}],
                ["remind_pm"], _version(latest))

        if rag == "GREEN":
            cash = float(latest["cash_position_ngn"])
            reason = None
            if cash < 0:
                reason = f"Marked Green (on track), but cash is negative (−₦{abs(cash):,.0f})."
            elif prev and float(prev["cash_position_ngn"]) > 0:
                drop = 1 - cash / float(prev["cash_position_ngn"])
                if drop >= float(cfg["cash_drop_threshold"]):
                    reason = f"Marked Green (on track), but cash fell {drop:.0%} from last month."
            if reason:
                add("CASH_RAG_INCONSISTENT", m, "MEDIUM", 0.5, reason,
                    [ev_rag, {"submission_id": latest["id"], "field": "cash_position_ngn", "value": latest["cash_position_ngn"]}],
                    ["remind_pm"], _version(latest), judgement=True)

        if INJECTION_RE.search(blocker):
            add("SUSPICIOUS_CONTENT", m, "MEDIUM", 1.0,
                "The update contains text that looks like instructions to the AI (for example, telling it to mark the portfolio as fine). The assistant ignored it, but someone should ask the manager about it.",
                [ev_blocker], ["flag_for_review"], _version(latest))

        # --- Persistent Red (counts back from the latest submission)
        n = int(cfg["persistent_red_months"])
        streak, cursor = 0, m
        while True:
            s = snap.get(pid, cursor)
            if not s or s["rag_status"] != "RED":
                break
            streak += 1
            cursor = month_add(cursor, -1)
        if streak >= n:
            add("PERSISTENT_RED", m, "HIGH", 1.0,
                f"Off track (Red) for {streak} months in a row, {fmt_month(month_add(m, 1 - streak))} to {fmt_month(m)}.",
                [ev_rag], ["escalate_cos"], _version(latest))
    return out


def evaluate_all(snap: Snapshot, as_of: datetime) -> Dict[str, List[Finding]]:
    return {p["id"]: evaluate_portfolio(snap, p, as_of) for p in snap.portfolios}
