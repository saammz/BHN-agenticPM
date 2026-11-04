from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
ACTION_TYPES = ("remind_pm", "escalate_cos", "flag_for_review", "log_only")


@dataclass
class Finding:
    portfolio_id: str
    portfolio_name: str
    rule_id: str
    reporting_month: str
    severity: str
    confidence: float
    source: str  # rule | rule+llm | llm | rule (llm unavailable)
    title: str
    rationale: str
    evidence: List[Any]
    recommended_actions: List[str]
    data_version: str
    needs_judgement: bool = False  # rule only raises a signal; an LLM (or human) must judge it
    disputed: bool = False  # LLM disagreed with a deterministic rule -> human decides
    llm_note: str = ""
    grounded: bool = True

    @property
    def fingerprint(self) -> str:
        raw = f"{self.portfolio_id}|{self.rule_id}|{self.reporting_month}|{self.data_version}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


@dataclass
class ActionDecision:
    action_type: str
    recipient: str
    finding: Finding
    decision: str  # execute | queue | suppressed | duplicate | shadow | log
    reason: str
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def action_fingerprint(self) -> str:
        return f"{self.finding.fingerprint}:{self.action_type}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "recipient": self.recipient,
            "decision": self.decision,
            "reason": self.reason,
            "action_fingerprint": self.action_fingerprint,
            "finding": self.finding.to_dict(),
            "meta": self.meta,
        }
