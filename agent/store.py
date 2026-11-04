"""Agent state, stored in the database (Neon Postgres or SQLite). Run traces stay as JSONL files in state/runs
(high-volume telemetry for the canvas); everything with business meaning is in the database."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .audit import AuditTrail
from .config import WAT
from .db import AGENT_STATE_TABLES, Database


def now_iso() -> str:
    return datetime.now(WAT).isoformat(timespec="microseconds")


def new_id(prefix: str = "", n: int = 8) -> str:
    return f"{prefix}{uuid.uuid4().hex[:n]}"


OVERRIDE_COLS = ("portfolio_id", "rule_id", "reporting_month", "fingerprint", "severity", "until", "reason",
                 "decision", "review_id")


class OverrideView:
    """Overrides loaded once per run, so the policy gate does not hit the database for every finding."""

    def __init__(self, items: List[Dict[str, Any]]):
        self.items = items

    def paused(self) -> bool:
        state = False
        for o in self.items:
            if o["type"] == "PAUSE":
                state = True
            elif o["type"] == "RESUME":
                state = False
        return state

    def suppression_for(self, portfolio_id: str, rule_id: str, month: str, fingerprint: str,
                        today: date) -> Optional[Dict[str, Any]]:
        for o in self.items:
            if o["type"] == "DISMISS" and o.get("fingerprint") == fingerprint:
                return o
            if o["type"] == "SUPPRESS" and o.get("portfolio_id") in (portfolio_id, "*") \
                    and o.get("rule_id") in (rule_id, "*") and o.get("reporting_month") in (month, "*", None):
                until = o.get("until")
                if not until or date.fromisoformat(until) >= today:
                    return o
        return None

    def severity_for(self, fingerprint: str) -> Optional[str]:
        sev = None
        for o in self.items:
            if o["type"] == "SEVERITY" and o.get("fingerprint") == fingerprint:
                sev = o["severity"]
        return sev

    def precedents(self, portfolio_id: str) -> List[Dict[str, Any]]:
        keep = ("type", "rule_id", "reporting_month", "reason", "decision", "created_at")
        return [{k: o[k] for k in keep if o.get(k) is not None} for o in self.items
                if o.get("portfolio_id") == portfolio_id and o["type"] not in ("PAUSE", "RESUME")]


class OverrideStore:
    """Append-only human overrides. Types: SUPPRESS, DISMISS, SEVERITY, PAUSE, RESUME,
    ACTION_APPROVED, ACTION_REJECTED, DISPUTE_UPHELD, DISPUTE_DENIED."""

    def __init__(self, db: Database, audit=None):
        self.db = db
        self._audit = audit

    def all(self) -> List[Dict[str, Any]]:
        rows = self.db.q("SELECT * FROM overrides ORDER BY created_at, id")
        for r in rows:
            r["by"] = r.pop("by_email", None)
        return rows

    def view(self) -> OverrideView:
        return OverrideView(self.all())

    def add(self, type_: str, by: str, **fields: Any) -> Dict[str, Any]:
        row = {"id": fields.pop("id", None) or new_id("OVR-"), "type": type_, "by_email": by,
               "created_at": fields.pop("created_at", None) or now_iso()}
        for k in OVERRIDE_COLS:
            if fields.get(k) is not None:
                row[k] = str(fields[k])
        self.db.insert("overrides", row)
        row["by"] = row.pop("by_email")
        if self._audit:
            self._audit("override.created", target_type="override", target_id=row["id"],
                        summary=f"{type_} {row.get('portfolio_id') or ''} {row.get('rule_id') or ''} "
                                f"{row.get('fingerprint') or ''}".strip(),
                        override={k: v for k, v in row.items() if k != "id"})
        return row

    # convenience wrappers used by the CLI / tests
    def paused(self) -> bool:
        return self.view().paused()

    def precedents(self, portfolio_id: str) -> List[Dict[str, Any]]:
        return self.view().precedents(portfolio_id)


class ReviewStore:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> List[Dict[str, Any]]:
        return self.db.q("SELECT * FROM review_items ORDER BY created_at")

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM review_items WHERE id = %s", [rid])

    def pending_fingerprints(self) -> Set[str]:
        return {r["action_fingerprint"] for r in
                self.db.q("SELECT action_fingerprint FROM review_items WHERE status = 'pending'")}

    def add(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return self.db.insert("review_items", item)

    def update(self, rid: str, **fields: Any) -> None:
        cols = list(fields)
        self.db.x(f"UPDATE review_items SET {', '.join(c + ' = %s' for c in cols)} WHERE id = %s",
                  [fields[c] for c in cols] + [rid])


class FindingStore:
    """Latest known state of each finding (what PMs see in their portal)."""

    def __init__(self, db: Database):
        self.db = db

    def record_run(self, run_id: str, findings: Iterable[Any], decisions: Iterable[Any],
                   evaluated_portfolios: Iterable[str]) -> None:
        ts = now_iso()
        last: Dict[str, str] = {}
        rank = {"execute": 6, "queue": 5, "shadow": 4, "duplicate": 3, "suppressed": 2, "log": 1}
        for d in decisions:
            fp = d.finding.fingerprint
            if rank.get(d.decision, 0) > rank.get(last.get(fp, ""), 0):
                last[fp] = d.decision
        seen = set()
        for f in findings:
            fp = f.fingerprint
            seen.add(fp)
            data = f.to_dict()
            if self.db.one("SELECT fingerprint FROM findings WHERE fingerprint = %s", [fp]):
                self.db.x("UPDATE findings SET status = 'open', last_decision = %s, last_run_id = %s, last_seen = %s, "
                          "finding = %s WHERE fingerprint = %s", [last.get(fp), run_id, ts, data, fp])
            else:
                self.db.insert("findings", {"fingerprint": fp, "portfolio_id": f.portfolio_id, "rule_id": f.rule_id,
                                            "reporting_month": f.reporting_month, "status": "open",
                                            "last_decision": last.get(fp), "last_run_id": run_id,
                                            "first_seen": ts, "last_seen": ts, "finding": data})
        pids = list(evaluated_portfolios)
        if pids:
            rows = self.db.q(f"SELECT fingerprint FROM findings WHERE status = 'open' AND portfolio_id IN "
                             f"({', '.join(['%s'] * len(pids))})", pids)
            for r in rows:
                if r["fingerprint"] not in seen:
                    self.db.x("UPDATE findings SET status = 'resolved', last_seen = %s WHERE fingerprint = %s",
                              [ts, r["fingerprint"]])

    def for_portfolios(self, pids: List[str]) -> List[Dict[str, Any]]:
        if not pids:
            return []
        return self.db.q(f"SELECT * FROM findings WHERE portfolio_id IN ({', '.join(['%s'] * len(pids))}) "
                         f"ORDER BY status, last_seen DESC", pids)

    def get(self, fp: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM findings WHERE fingerprint = %s", [fp])


class DisputeStore:
    def __init__(self, db: Database):
        self.db = db

    def create(self, **row: Any) -> Dict[str, Any]:
        row = {"id": new_id("DSP-", 6), "status": "OPEN", "created_at": now_iso(), **row}
        return self.db.insert("disputes", row)

    def list(self, portfolio_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if portfolio_ids is None:
            return self.db.q("SELECT * FROM disputes ORDER BY created_at DESC")
        if not portfolio_ids:
            return []
        return self.db.q(f"SELECT * FROM disputes WHERE portfolio_id IN ({', '.join(['%s'] * len(portfolio_ids))}) "
                         f"ORDER BY created_at DESC", portfolio_ids)

    def get(self, did: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM disputes WHERE id = %s", [did])

    def update(self, did: str, **fields: Any) -> None:
        cols = list(fields)
        self.db.x(f"UPDATE disputes SET {', '.join(c + ' = %s' for c in cols)} WHERE id = %s",
                  [fields[c] for c in cols] + [did])


class EmailLog:
    def __init__(self, db: Database):
        self.db = db

    def add(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return self.db.insert("email_log", row)

    def list(self, limit: int = 200, to: Optional[str] = None) -> List[Dict[str, Any]]:
        if to:
            return self.db.q("SELECT * FROM email_log WHERE to_email = %s OR intended_to = %s "
                             "ORDER BY ts DESC LIMIT %s", [to, to, limit])
        return self.db.q("SELECT * FROM email_log ORDER BY ts DESC LIMIT %s", [limit])


class AppConfigStore:
    """Non-secret settings saved from the Settings page (overlay on top of .env defaults)."""

    def __init__(self, db: Database):
        self.db = db

    def all(self) -> Dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.db.q("SELECT key, value FROM app_config")}

    def set(self, key: str, value: Any, by: str) -> None:
        v = json.dumps(value)
        if self.db.one("SELECT key FROM app_config WHERE key = %s", [key]):
            self.db.x("UPDATE app_config SET value = %s, updated_at = %s, updated_by = %s WHERE key = %s",
                      [v, now_iso(), by, key])
        else:
            self.db.insert("app_config", {"key": key, "value": v, "updated_at": now_iso(), "updated_by": by})


class State:
    def __init__(self, state_dir: Path, data_dir: Path, db: Database):
        self.dir = Path(state_dir)
        self.data_dir = Path(data_dir)
        (self.dir / "runs").mkdir(parents=True, exist_ok=True)
        self.db = db
        self.trail = AuditTrail(db)
        self.overrides = OverrideStore(db, self.audit)
        self.review = ReviewStore(db)
        self.findings = FindingStore(db)
        self.disputes = DisputeStore(db)
        self.emails = EmailLog(db)
        self.config = AppConfigStore(db)

    def audit(self, event: str, **kw: Any) -> Dict[str, Any]:
        """Append to the tamper-evident audit trail (see agent/audit.py for the keyword fields)."""
        return self.trail.write(event, **kw)

    def done_set(self) -> Set[str]:
        return {r["action_fingerprint"] for r in self.db.q("SELECT action_fingerprint FROM action_ledger")}

    def already_done(self, action_fp: str) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM action_ledger WHERE action_fingerprint = %s", [action_fp]))

    def record_done(self, action_fp: str, run_id: Optional[str] = None, message_id: Optional[str] = None,
                    approved_by: Optional[str] = None) -> None:
        if not self.already_done(action_fp):
            self.db.insert("action_ledger", {"action_fingerprint": action_fp, "run_id": run_id,
                                             "message_id": message_id, "approved_by": approved_by, "ts": now_iso()})

    def seed_overrides(self) -> int:
        seed = self.data_dir / "seed_overrides.json"
        n = 0
        if seed.exists():
            for o in json.loads(seed.read_text()):
                if not self.db.one("SELECT id FROM overrides WHERE id = %s", [o["id"]]):
                    o = dict(o)
                    self.overrides.add(o.pop("type"), o.pop("by"), **o)
                    n += 1
        return n

    def reset(self) -> None:
        """Clear agent activity (runs, emails, reviews, overrides, findings, disputes). Users, portfolios and
        submissions are kept."""
        counts = {t: self.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in AGENT_STATE_TABLES}
        for t in AGENT_STATE_TABLES:
            self.db.x(f"DELETE FROM {t}")
        for p in (self.dir / "runs").glob("*.jsonl"):
            p.unlink()
        self.audit("system.reset", summary="agent activity cleared; audit trail kept", cleared_rows=counts)
        self.seed_overrides()
