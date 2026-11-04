"""Policy gate: the only place that decides whether an action executes, waits for a human, or is held."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from .config import Config
from .datasource import Snapshot
from .models import ActionDecision, Finding
from .store import OverrideView, State


def decide(findings: List[Finding], snap: Snapshot, state: State, mode: str, cfg: Config,
           as_of: datetime, overrides: OverrideView = None) -> List[ActionDecision]:
    overrides = overrides or state.overrides.view()
    paused = overrides.paused()
    cos_email = snap.settings["cos"]["email"]
    pending = state.review.pending_fingerprints()
    done = state.done_set()
    out: List[ActionDecision] = []
    for f in findings:
        sev = overrides.severity_for(f.fingerprint)
        if sev:
            f.severity = sev
        sup = overrides.suppression_for(f.portfolio_id, f.rule_id, f.reporting_month, f.fingerprint, as_of.date())
        pm = snap.portfolio(f.portfolio_id)
        for action in f.recommended_actions:
            recipient: Dict[str, str] = {
                "remind_pm": pm.get("pm_email") or "(no PM)", "escalate_cos": cos_email or "(no CoS)",
                "flag_for_review": "CoS review queue", "log_only": "console",
            }
            d = ActionDecision(action, recipient.get(action, "console"), f, "execute", "")
            if sup:
                d.decision, d.reason = "suppressed", f"human override {sup['id']} ({sup['type']}): {sup.get('reason', '')}"
            elif action == "log_only":
                d.decision, d.reason = "log", "informational"
            elif paused:
                d.decision, d.reason = "shadow", "agent PAUSED by human kill switch"
            elif mode == "shadow":
                d.decision, d.reason = "shadow", "shadow mode: would have acted"
            elif d.action_fingerprint in done:
                d.decision, d.reason = "duplicate", "already sent for this exact finding (idempotency)"
            elif d.action_fingerprint in pending:
                d.decision, d.reason = "duplicate", "already waiting in the review queue"
            elif action == "remind_pm" and not pm.get("pm_email"):
                d.decision, d.recipient, d.reason = "queue", "CoS review queue", "portfolio has no active PM to email"
            elif action == "escalate_cos" and not cos_email:
                d.decision, d.recipient, d.reason = "queue", "CoS review queue", "no active CoS account to escalate to"
            elif mode == "assist":
                d.decision, d.reason = "queue", "assist mode: every action needs approval"
            elif action == "flag_for_review":
                d.decision, d.reason = "queue", "finding explicitly needs a human"
            elif not f.grounded:
                d.decision, d.reason = "queue", "LLM evidence failed grounding"
            elif f.disputed:
                d.decision, d.reason = "queue", "LLM disputes a deterministic rule; human decides"
            elif f.confidence < cfg.conf_min_act:
                d.decision, d.reason = "queue", f"confidence {f.confidence:.2f} < {cfg.conf_min_act} minimum to act"
            elif action == "remind_pm":
                if f.source in ("rule", "rule+llm") and not f.needs_judgement:
                    d.reason = "deterministic rule; low-impact reminder"
                elif f.confidence >= cfg.conf_auto_remind:
                    d.reason = f"confidence {f.confidence:.2f} >= {cfg.conf_auto_remind}"
                else:
                    d.decision, d.reason = "queue", f"judgement call at {f.confidence:.2f} < {cfg.conf_auto_remind} auto threshold"
            elif action == "escalate_cos":
                if f.source in ("rule", "rule+llm") and not f.needs_judgement and f.severity in ("HIGH", "CRITICAL"):
                    d.reason = "deterministic HIGH finding"
                else:
                    d.decision, d.reason = "queue", "LLM-originated escalations always need human approval"
            out.append(d)
    return out
