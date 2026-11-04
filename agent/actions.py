"""Actions: compliance emails (Resend / simulated), human review queue, PM disputes."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .config import Config
from .datasource import Snapshot
from .mailer import Mailer
from .models import ActionDecision
from .store import State, new_id, now_iso
from .timeutil import fmt_month

DELIVERED = ("sent", "simulated", "console")


def dispute_note(cfg: Config, fps: List[str]) -> str:
    links = "\n".join(f"  {cfg.app_url}/dispute/{fp}" for fp in fps)
    return ("If you believe something here is wrong, dispute it (you'll be asked to sign in). The Chief of Staff "
            "reviews every dispute, and an upheld dispute stops the agent raising it again:\n" + links)


def _line(f: Dict[str, Any]) -> str:
    note = f"\n     Agent note: {f['llm_note']}" if f.get("llm_note") else ""
    return f"  • [{f['severity']}] {f['title']} — {fmt_month(f['reporting_month'])}\n     {f['rationale']}{note}"


def render_pm_email(cfg: Config, pm: Dict[str, Any], findings: List[Dict[str, Any]], footer: str = "") -> Dict[str, str]:
    first = (pm.get("pm_name") or "there").split()[0]
    body = (f"Dear {first},\n\nThe BNH reporting assistant found the following on {pm['name']} that needs your "
            f"attention:\n\n" + "\n".join(_line(f) for f in findings) +
            f"\n\nPlease update your monthly submission in the Portfolio Reporting app.\n\n"
            f"{dispute_note(cfg, [f['fingerprint'] for f in findings])}\n\n— BNH PM Compliance Agent {footer or '(automated)'}")
    return {"subject": f"[Action needed] {pm['name']}: monthly reporting", "body": body}


def render_cos_email(cfg: Config, findings: List[Dict[str, Any]], footer: str = "") -> Dict[str, str]:
    by_p: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in findings:
        by_p[f["portfolio_name"]].append(f)
    sections = "\n\n".join(f"{name}\n" + "\n".join(_line(f) for f in fs) for name, fs in by_p.items())
    body = (f"Chief of Staff,\n\n{len(findings)} compliance issue(s) need your intervention:\n\n{sections}\n\n"
            f"Review, approve or override at {cfg.app_url}\n\n— BNH PM Compliance Agent {footer or '(automated)'}")
    return {"subject": f"[Escalation] {len(by_p)} portfolio(s) need CoS intervention", "body": body}


def enqueue(d: ActionDecision, state: State, run_id: str) -> Dict[str, Any]:
    item = {"id": new_id("REV-", 6), "status": "pending", "created_at": now_iso(), "run_id": run_id,
            "action_fingerprint": d.action_fingerprint, "action_type": d.action_type, "recipient": d.recipient,
            "reason": d.reason, "finding": d.finding.to_dict()}
    state.review.add(item)
    state.audit("review.queued", target_type="review", target_id=item["id"], run_id=run_id,
                summary=f"{d.finding.portfolio_id} {d.finding.rule_id}: {d.action_type} held — {d.reason}",
                fingerprint=d.finding.fingerprint, action_type=d.action_type, reason=d.reason,
                portfolio_id=d.finding.portfolio_id, portfolio_name=d.finding.portfolio_name, rule_id=d.finding.rule_id)
    return item


def execute(decisions: List[ActionDecision], snap: Snapshot, state: State, run_id: str, mailer: Mailer
            ) -> Dict[str, List[Dict[str, Any]]]:
    """Execute decided actions. Emails are batched: one per PM, one digest to the CoS. A failed send is not
    recorded in the ledger, so the next run retries it."""
    cfg = mailer.cfg
    result: Dict[str, List[Dict[str, Any]]] = {"emails": [], "queued": [], "failed": []}
    pm_batches: Dict[str, List[ActionDecision]] = defaultdict(list)
    cos_batch: List[ActionDecision] = []
    for d in decisions:
        if d.decision == "execute" and d.action_type == "remind_pm":
            pm_batches[d.finding.portfolio_id].append(d)
        elif d.decision == "execute" and d.action_type == "escalate_cos":
            cos_batch.append(d)
        elif d.decision == "queue":
            result["queued"].append(enqueue(d, state, run_id))

    def deliver(to, mail, kind, items, pid=None):
        msg = mailer.send(to, mail["subject"], mail["body"], kind,
                          meta={"run_id": run_id, "portfolio_id": pid,
                                "fingerprints": [d.finding.fingerprint for d in items]})
        if msg["status"] in DELIVERED:
            for d in items:
                state.record_done(d.action_fingerprint, run_id=run_id, message_id=msg["id"])
            result["emails"].append(msg)
        else:
            result["failed"].append(msg)

    for pid, items in pm_batches.items():
        pm = snap.portfolio(pid)
        deliver(pm["pm_email"], render_pm_email(cfg, pm, [d.finding.to_dict() for d in items]), "remind_pm", items, pid)
    if cos_batch:
        deliver(snap.settings["cos"]["email"], render_cos_email(cfg, [d.finding.to_dict() for d in cos_batch]),
                "escalate_cos", cos_batch)
    return result


def decide_review(review_id: str, decision: str, by: str, note: str, state: State, snap: Snapshot,
                  mailer: Mailer, dismiss_finding: bool = False) -> Dict[str, Any]:
    """Human-in-the-loop: approve executes the held action; reject records a precedent and can dismiss the finding."""
    item = state.review.get(review_id)
    if not item:
        raise KeyError(f"review item {review_id} not found")
    if item["status"] != "pending":
        raise ValueError(f"{review_id} is already {item['status']}")
    f = item["finding"]
    sent = None
    if decision == "approve":
        action = "remind_pm" if item["action_type"] == "flag_for_review" else item["action_type"]
        g = dict(f)
        if note:
            g["llm_note"] = (g.get("llm_note") or "") + f"\n     Reviewer note: {note}"
        footer = f"(approved by {by})"
        if action == "remind_pm":
            pm = snap.portfolio(f["portfolio_id"])
            if not pm.get("pm_email"):
                raise ValueError("this portfolio has no active PM to email; assign one in Users")
            mail, to = render_pm_email(mailer.cfg, pm, [g], footer), pm["pm_email"]
        else:
            to = snap.settings["cos"]["email"]
            if not to:
                raise ValueError("no active CoS account to send the escalation to")
            mail = render_cos_email(mailer.cfg, [g], footer)
        sent = mailer.send(to, mail["subject"], mail["body"], f"{action}(approved)",
                           meta={"run_id": item["run_id"], "portfolio_id": f["portfolio_id"],
                                 "fingerprints": [f["fingerprint"]], "review_id": review_id})
        if sent["status"] not in DELIVERED:
            raise ValueError(f"email failed: {sent['error']} — the item stays pending, try again")
        state.record_done(item["action_fingerprint"], run_id=item["run_id"], message_id=sent["id"], approved_by=by)
        state.overrides.add("ACTION_APPROVED", by, review_id=review_id, portfolio_id=f["portfolio_id"],
                            rule_id=f["rule_id"], reporting_month=f["reporting_month"], decision="approved",
                            reason=note or "approved")
    elif decision == "reject":
        state.overrides.add("ACTION_REJECTED", by, review_id=review_id, portfolio_id=f["portfolio_id"],
                            rule_id=f["rule_id"], reporting_month=f["reporting_month"], decision="rejected",
                            reason=note or "rejected")
        if dismiss_finding:
            state.overrides.add("DISMISS", by, fingerprint=f["fingerprint"], portfolio_id=f["portfolio_id"],
                                rule_id=f["rule_id"], reporting_month=f["reporting_month"],
                                reason=note or "false positive")
    else:
        raise ValueError("decision must be approve or reject")
    state.review.update(review_id, status="approved" if decision == "approve" else "rejected", decided_by=by,
                        decided_at=now_iso(), note=note, message_id=sent["id"] if sent else None)
    state.audit(f"review.{'approved' if decision == 'approve' else 'rejected'}", target_type="review",
                target_id=review_id, run_id=item["run_id"],
                summary=f"{f['portfolio_id']} {f['rule_id']}: {item['action_type']} {decision}d"
                        + (" + finding dismissed as false positive" if dismiss_finding else ""),
                note=note, dismissed=dismiss_finding, fingerprint=f["fingerprint"],
                email_id=sent["id"] if sent else None, portfolio_id=f["portfolio_id"],
                portfolio_name=f.get("portfolio_name"), rule_id=f["rule_id"], action_type=item["action_type"])
    return {"item": state.review.get(review_id), "email": sent}


# ── PM disputes ──────────────────────────────────────────────────────────────────────────
def create_dispute(state: State, mailer: Mailer, user: Dict[str, Any], fingerprint: str, message: str,
                   allowed_portfolios: List[str]) -> Dict[str, Any]:
    finding = state.findings.get(fingerprint)
    if not finding or finding["portfolio_id"] not in allowed_portfolios:
        raise KeyError("finding not found")  # same error whether it exists or not: no cross-portfolio probing
    message = (message or "").strip()
    if not 10 <= len(message) <= 2000:
        raise ValueError("Explain the dispute in 10–2000 characters.")
    if any(d["fingerprint"] == fingerprint and d["status"] == "OPEN" for d in state.disputes.list([finding["portfolio_id"]])):
        raise ValueError("There is already an open dispute for this finding.")
    d = state.disputes.create(fingerprint=fingerprint, portfolio_id=finding["portfolio_id"],
                              rule_id=finding["rule_id"], reporting_month=finding["reporting_month"],
                              raised_by=user["id"], raised_by_email=user["email"], message=message)
    state.audit("dispute.opened", actor=user["email"], actor_role=user["role"], actor_id=user["id"],
                target_type="dispute", target_id=d["id"],
                summary=f"{finding['portfolio_id']} {finding['rule_id']} {finding['reporting_month']} disputed",
                fingerprint=fingerprint, message=message)
    cos = state.db.one("SELECT email FROM users WHERE role = 'COS' AND status = 'ACTIVE' ORDER BY created_at LIMIT 1")
    if cos:
        f = finding["finding"]
        mailer.send(cos["email"], f"[Dispute] {f['portfolio_name']}: {f['title']}",
                    f"{user['name']} ({user['email']}) disputes a finding on {f['portfolio_name']} "
                    f"({fmt_month(f['reporting_month'])}):\n\n  {f['title']}\n  {f['rationale']}\n\n"
                    f"Their explanation:\n  {message}\n\nUphold or deny it at {mailer.cfg.app_url}\n\n"
                    "— BNH PM Compliance Agent", "dispute_opened", meta={"portfolio_id": finding["portfolio_id"]})
    return d


def decide_dispute(state: State, mailer: Mailer, dispute_id: str, decision: str, by: str, note: str) -> Dict[str, Any]:
    d = state.disputes.get(dispute_id)
    if not d:
        raise KeyError("dispute not found")
    if d["status"] != "OPEN":
        raise ValueError(f"dispute is already {d['status']}")
    if decision not in ("uphold", "deny"):
        raise ValueError("decision must be uphold or deny")
    if decision == "deny" and not note.strip():
        raise ValueError("Give the PM a reason when denying a dispute.")
    status = "UPHELD" if decision == "uphold" else "DENIED"
    state.disputes.update(dispute_id, status=status, decided_by=by, decided_at=now_iso(), decision_note=note)
    if decision == "uphold":
        state.overrides.add("DISMISS", by, fingerprint=d["fingerprint"], portfolio_id=d["portfolio_id"],
                            rule_id=d["rule_id"], reporting_month=d["reporting_month"],
                            reason=f"PM dispute upheld: {note or d['message'][:200]}")
        for item in state.review.all():
            if item["status"] == "pending" and item["finding"]["fingerprint"] == d["fingerprint"]:
                state.review.update(item["id"], status="rejected", decided_by=by, decided_at=now_iso(),
                                    note="closed: PM dispute upheld")
    state.overrides.add("DISPUTE_" + status, by, portfolio_id=d["portfolio_id"], rule_id=d["rule_id"],
                        reporting_month=d["reporting_month"], fingerprint=d["fingerprint"],
                        decision=status.lower(), reason=note or d["message"][:300])
    state.audit("dispute.decided", target_type="dispute", target_id=dispute_id,
                summary=f"{d['portfolio_id']} {d['rule_id']}: dispute {status.lower()}",
                decision=status, note=note, fingerprint=d["fingerprint"], raised_by=d["raised_by_email"])
    mailer.send(d["raised_by_email"], f"Your dispute was {status.lower()}",
                f"Your dispute ({d['id']}) about {d['rule_id']} for {fmt_month(d['reporting_month'])} was "
                f"{status.lower()} by the Chief of Staff's office."
                + (f"\n\nNote: {note}" if note else "")
                + ("\n\nThe agent will not raise this finding again for this submission." if status == "UPHELD" else
                   "\n\nPlease update your submission in the Portfolio Reporting app.")
                + "\n\n— BNH PM Compliance Agent", "dispute_decided", meta={"portfolio_id": d["portfolio_id"]})
    return state.disputes.get(dispute_id)
