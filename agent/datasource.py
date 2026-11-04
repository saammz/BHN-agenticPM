"""Data source: portfolios, submissions and settings, read from the agent's database (Neon Postgres or SQLite).
`seed_demo` loads the demo dataset in data/*.json into an empty database."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import WAT, Config
from .db import Database, DbError

VALID_RAG = {"GREEN", "AMBER", "RED"}


class DataSourceError(Exception):
    pass


@dataclass
class Snapshot:
    portfolios: List[Dict[str, Any]]
    submissions: List[Dict[str, Any]]
    settings: Dict[str, Any]
    loaded_at: datetime

    def portfolio(self, pid: str) -> Dict[str, Any]:
        for p in self.portfolios:
            if p["id"] == pid:
                return p
        raise KeyError(pid)

    def history(self, pid: str) -> List[Dict[str, Any]]:
        return sorted((s for s in self.submissions if s["portfolio_id"] == pid), key=lambda s: s["reporting_month"])

    def get(self, pid: str, month: str) -> Optional[Dict[str, Any]]:
        for s in self.submissions:
            if s["portfolio_id"] == pid and s["reporting_month"] == month:
                return s
        return None

    def submission(self, sid: str) -> Optional[Dict[str, Any]]:
        for s in self.submissions:
            if s["id"] == sid:
                return s
        return None


class DbDataSource:
    def __init__(self, db: Database):
        self.db = db

    def load(self) -> Snapshot:
        if os.environ.get("MOCK_DATASOURCE_FAIL") == "1":
            raise DataSourceError("Simulated outage: submissions database unreachable (connection timed out)")
        try:
            portfolios = self.db.q(
                "SELECT p.id, p.name, p.active, p.pm_user_id, u.name AS pm_name, u.email AS pm_email "
                "FROM portfolios p LEFT JOIN users u ON u.id = p.pm_user_id AND u.status = 'ACTIVE' "
                "WHERE p.active = %s ORDER BY p.id", [True])
            submissions = self.db.q(
                "SELECT s.id, s.portfolio_id, s.reporting_month, s.cash_position_ngn, s.rag_status, "
                "s.execution_blocker, u.email AS submitted_by, s.submitted_at, s.updated_at "
                "FROM submissions s LEFT JOIN users u ON u.id = s.submitted_by")
            st = self.db.one("SELECT * FROM settings WHERE id = 1") or {}
            cos = self.db.one("SELECT name, email FROM users WHERE role = 'COS' AND status = 'ACTIVE' "
                              "ORDER BY created_at LIMIT 1")
        except DbError as e:
            raise DataSourceError(str(e)) from e
        if not portfolios:
            raise DataSourceError("portfolio roster is empty — refusing to run (would look like 100% compliance). "
                                  "Run: agentpm.py db seed")
        ids = {p["id"] for p in portfolios}
        submissions = [s for s in submissions if s["portfolio_id"] in ids]
        for s in submissions:
            if s["rag_status"] not in VALID_RAG:
                raise DataSourceError(f"submission {s['id']} has invalid rag_status {s['rag_status']!r}")
        settings = {
            "deadline_day": int(st.get("deadline_day", 10)),
            "reminder_window_days": int(st.get("reminder_window_days", 7)),
            "persistent_red_months": int(st.get("persistent_red_months", 3)),
            "cash_drop_threshold": float(st.get("cash_drop_threshold", 0.4)),
            "cos": {"name": cos["name"], "email": cos["email"]} if cos else {"name": "Chief of Staff", "email": None},
        }
        return Snapshot(portfolios, submissions, settings, datetime.now(WAT))


def seed_demo(db: Database, cfg: Config, data_dir: Path) -> Dict[str, int]:
    """Idempotent: creates the first CoS (from SEED_COS_EMAIL), demo PMs, portfolios and submissions."""
    from .store import now_iso
    counts = {"users": 0, "portfolios": 0, "submissions": 0}

    def ensure_user(name: str, email: str, role: str) -> str:
        email = email.strip().lower()
        row = db.one("SELECT id FROM users WHERE email = %s", [email])
        if row:
            return row["id"]
        uid = str(uuid.uuid4())
        db.insert("users", {"id": uid, "name": name, "email": email, "role": role, "status": "ACTIVE",
                            "created_at": now_iso()})
        counts["users"] += 1
        return uid

    if cfg.seed_cos_email:
        ensure_user(cfg.seed_cos_name, cfg.seed_cos_email, "COS")
    settings = json.loads((data_dir / "settings.json").read_text())
    db.x("UPDATE settings SET deadline_day = %s, reminder_window_days = %s, persistent_red_months = %s, "
         "cash_drop_threshold = %s, updated_at = %s WHERE id = 1",
         [settings["deadline_day"], settings["reminder_window_days"], settings["persistent_red_months"],
          settings["cash_drop_threshold"], now_iso()])
    pm_ids: Dict[str, str] = {}
    for p in json.loads((data_dir / "portfolios.json").read_text()):
        uid = ensure_user(p["pm_name"], p["pm_email"], "PM")
        pm_ids[p["pm_email"].lower()] = uid
        if not db.one("SELECT id FROM portfolios WHERE id = %s", [p["id"]]):
            db.insert("portfolios", {"id": p["id"], "name": p["name"], "pm_user_id": uid,
                                     "active": bool(p.get("active", True)), "created_at": now_iso()})
            counts["portfolios"] += 1
    for s in json.loads((data_dir / "submissions.json").read_text()):
        if not db.one("SELECT id FROM submissions WHERE id = %s", [s["id"]]):
            db.insert("submissions", {
                "id": s["id"], "portfolio_id": s["portfolio_id"], "reporting_month": s["reporting_month"],
                "cash_position_ngn": s["cash_position_ngn"], "rag_status": s["rag_status"],
                "execution_blocker": s["execution_blocker"], "submitted_by": pm_ids.get(s["submitted_by"].lower()),
                "submitted_at": s["submitted_at"], "updated_at": s.get("updated_at")})
            counts["submissions"] += 1
    return counts
