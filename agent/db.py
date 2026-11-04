"""Database layer. Neon Postgres when DATABASE_URL is set; a local SQLite file otherwise (offline dev and tests).

SQL is written once with %s placeholders. Rows come back as plain dicts with datetimes/dates as ISO strings,
numerics as floats and JSON columns decoded, so the rest of the code never sees driver-specific types."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

JSON_COLS = {"finding", "meta", "detail", "data", "extra"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id VARCHAR(36) PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  email VARCHAR(254) NOT NULL UNIQUE,
  role VARCHAR(8) NOT NULL CHECK (role IN ('COS','PM')),
  status VARCHAR(12) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DEACTIVATED')),
  last_login_at {TS},
  last_login_ip VARCHAR(64),
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolios (
  id VARCHAR(20) PRIMARY KEY,
  name VARCHAR(100) NOT NULL UNIQUE,
  pm_user_id VARCHAR(36) REFERENCES users(id),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id VARCHAR(40) PRIMARY KEY,
  portfolio_id VARCHAR(20) NOT NULL REFERENCES portfolios(id),
  reporting_month VARCHAR(7) NOT NULL,
  cash_position_ngn NUMERIC(18,2) NOT NULL,
  rag_status VARCHAR(5) NOT NULL CHECK (rag_status IN ('GREEN','AMBER','RED')),
  execution_blocker VARCHAR(500) NOT NULL,
  submitted_by VARCHAR(36) REFERENCES users(id),
  submitted_at {TS} NOT NULL,
  updated_at {TS},
  UNIQUE (portfolio_id, reporting_month)
);
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  deadline_day INTEGER NOT NULL DEFAULT 10,
  reminder_window_days INTEGER NOT NULL DEFAULT 7,
  persistent_red_months INTEGER NOT NULL DEFAULT 3,
  cash_drop_threshold NUMERIC(4,2) NOT NULL DEFAULT 0.40,
  updated_at {TS}
);
CREATE TABLE IF NOT EXISTS login_codes (
  id VARCHAR(36) PRIMARY KEY,
  user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash VARCHAR(64) NOT NULL,
  link_hash VARCHAR(64) NOT NULL UNIQUE,
  ip VARCHAR(64),
  user_agent VARCHAR(512),
  attempts INTEGER NOT NULL DEFAULT 0,
  expires_at {TS} NOT NULL,
  used_at {TS},
  created_at {TS} NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_codes_user ON login_codes(user_id, created_at);
CREATE TABLE IF NOT EXISTS auth_sessions (
  id VARCHAR(36) PRIMARY KEY,
  token_hash VARCHAR(64) NOT NULL UNIQUE,
  user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ip VARCHAR(64),
  user_agent VARCHAR(512),
  expires_at {TS} NOT NULL,
  last_seen_at {TS} NOT NULL,
  revoked_at {TS},
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_events (
  id VARCHAR(36) PRIMARY KEY,
  ts {TS} NOT NULL,
  event VARCHAR(40) NOT NULL,
  email VARCHAR(254),
  user_id VARCHAR(36),
  ip VARCHAR(64),
  user_agent VARCHAR(512),
  detail {JSON}
);
CREATE INDEX IF NOT EXISTS ix_auth_events_rl ON auth_events(event, ts);
CREATE TABLE IF NOT EXISTS overrides (
  id VARCHAR(40) PRIMARY KEY,
  type VARCHAR(20) NOT NULL,
  portfolio_id VARCHAR(20),
  rule_id VARCHAR(40),
  reporting_month VARCHAR(7),
  fingerprint VARCHAR(20),
  severity VARCHAR(10),
  until VARCHAR(10),
  reason TEXT,
  decision VARCHAR(20),
  review_id VARCHAR(20),
  by_email VARCHAR(254),
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
  id VARCHAR(20) PRIMARY KEY,
  status VARCHAR(10) NOT NULL,
  run_id VARCHAR(60),
  action_fingerprint VARCHAR(60) NOT NULL,
  action_type VARCHAR(20) NOT NULL,
  recipient VARCHAR(254),
  reason TEXT,
  finding {JSON},
  created_at {TS} NOT NULL,
  decided_by VARCHAR(254),
  decided_at {TS},
  note TEXT,
  message_id VARCHAR(40)
);
CREATE TABLE IF NOT EXISTS findings (
  fingerprint VARCHAR(20) PRIMARY KEY,
  portfolio_id VARCHAR(20) NOT NULL,
  rule_id VARCHAR(40) NOT NULL,
  reporting_month VARCHAR(7) NOT NULL,
  status VARCHAR(10) NOT NULL,
  last_decision VARCHAR(20),
  last_run_id VARCHAR(60),
  first_seen {TS} NOT NULL,
  last_seen {TS} NOT NULL,
  finding {JSON}
);
CREATE TABLE IF NOT EXISTS disputes (
  id VARCHAR(20) PRIMARY KEY,
  fingerprint VARCHAR(20) NOT NULL,
  portfolio_id VARCHAR(20) NOT NULL,
  rule_id VARCHAR(40),
  reporting_month VARCHAR(7),
  raised_by VARCHAR(36) REFERENCES users(id),
  raised_by_email VARCHAR(254),
  message TEXT NOT NULL,
  status VARCHAR(10) NOT NULL,
  decided_by VARCHAR(254),
  decided_at {TS},
  decision_note TEXT,
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS action_ledger (
  action_fingerprint VARCHAR(60) PRIMARY KEY,
  run_id VARCHAR(60),
  message_id VARCHAR(40),
  approved_by VARCHAR(254),
  ts {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS email_log (
  id VARCHAR(20) PRIMARY KEY,
  ts {TS} NOT NULL,
  to_email VARCHAR(254) NOT NULL,
  intended_to VARCHAR(254),
  subject VARCHAR(300) NOT NULL,
  body TEXT NOT NULL,
  kind VARCHAR(40),
  delivery VARCHAR(12) NOT NULL,
  provider_id VARCHAR(80),
  status VARCHAR(12) NOT NULL,
  error TEXT,
  run_id VARCHAR(60),
  portfolio_id VARCHAR(20),
  meta {JSON}
);
CREATE INDEX IF NOT EXISTS ix_email_log_ts ON email_log(ts);
CREATE TABLE IF NOT EXISTS run_events (
  run_id VARCHAR(60) NOT NULL,
  seq INTEGER NOT NULL,
  ts DOUBLE PRECISION NOT NULL,
  node VARCHAR(30) NOT NULL,
  status VARCHAR(12) NOT NULL,
  data {JSON},
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS app_config (
  key VARCHAR(60) PRIMARY KEY,
  value TEXT,
  updated_at {TS} NOT NULL,
  updated_by VARCHAR(254)
);
CREATE TABLE IF NOT EXISTS audit_trail (
  id VARCHAR(36) PRIMARY KEY,
  seq BIGINT NOT NULL UNIQUE,
  ts {TS} NOT NULL,
  category VARCHAR(20) NOT NULL,
  event VARCHAR(60) NOT NULL,
  outcome VARCHAR(10) NOT NULL,
  actor VARCHAR(254),
  actor_role VARCHAR(8),
  actor_id VARCHAR(36),
  ip VARCHAR(64),
  user_agent VARCHAR(512),
  target_type VARCHAR(30),
  target_id VARCHAR(80),
  run_id VARCHAR(60),
  summary VARCHAR(500),
  data {JSON},
  payload TEXT NOT NULL,
  prev_hash VARCHAR(64) NOT NULL,
  hash VARCHAR(64) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_trail_ts ON audit_trail(ts);
CREATE INDEX IF NOT EXISTS ix_audit_trail_cat ON audit_trail(category, seq);
CREATE INDEX IF NOT EXISTS ix_audit_trail_actor ON audit_trail(actor);
CREATE INDEX IF NOT EXISTS ix_audit_trail_target ON audit_trail(target_id);
CREATE INDEX IF NOT EXISTS ix_audit_trail_run ON audit_trail(run_id);
"""

# Cleared by "reset". The audit trail is never cleared: a reset is itself audited.
AGENT_STATE_TABLES = ("overrides", "review_items", "findings", "disputes", "action_ledger", "email_log", "run_events")


class DbError(Exception):
    pass


def _norm(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


class Database:
    def __init__(self, url: Optional[str], sqlite_path: Path):
        self.url = url or ""
        self.kind = "postgres" if self.url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self.sqlite_path = Path(sqlite_path)
        self._local = threading.local()
        self._sqlite_lock = threading.RLock()

    # -- connections -------------------------------------------------------------------
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is not None and not getattr(c, "closed", False):
            return c
        if self.kind == "postgres":
            try:
                import psycopg
            except ImportError as e:  # pragma: no cover
                raise DbError("psycopg is not installed: run  .venv/bin/pip install -r requirements.txt") from e
            try:
                c = psycopg.connect(self.url, autocommit=True, connect_timeout=15, prepare_threshold=None)
            except Exception as e:  # noqa: BLE001
                raise DbError(f"cannot connect to Postgres: {type(e).__name__}: {str(e)[:200]}") from e
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(self.sqlite_path), check_same_thread=False, isolation_level=None, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
        self._local.conn = c
        return c

    def _sql(self, sql: str) -> str:
        return sql if self.kind == "postgres" else sql.replace("%s", "?")

    def _params(self, params: Sequence[Any]) -> List[Any]:
        out = []
        for p in params:
            if isinstance(p, (dict, list)):
                p = json.dumps(p, default=str)
            elif self.kind == "sqlite" and isinstance(p, bool):
                p = int(p)
            out.append(p)
        return out

    def _rows(self, cur) -> List[Dict[str, Any]]:
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = {k: _norm(v) for k, v in zip(cols, tuple(r))}
            for k in JSON_COLS & d.keys():
                if isinstance(d[k], str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            if "active" in d and isinstance(d["active"], int):
                d["active"] = bool(d["active"])
            rows.append(d)
        return rows

    def _run(self, sql: str, params: Sequence[Any] = ()):
        for attempt in (1, 2):
            conn = self._conn()
            try:
                if self.kind == "sqlite":
                    with self._sqlite_lock:
                        cur = conn.execute(self._sql(sql), self._params(params))
                        return self._rows(cur), cur.rowcount
                with conn.cursor() as cur:
                    cur.execute(self._sql(sql), self._params(params))
                    return self._rows(cur), cur.rowcount
            except DbError:
                raise
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if self.kind == "postgres" and attempt == 1 and name in ("OperationalError", "InterfaceError"):
                    self._local.conn = None  # Neon closed an idle connection: reconnect once
                    continue
                raise DbError(f"{name}: {str(e)[:300]}") from e
        raise DbError("unreachable")

    # -- public API ----------------------------------------------------------------------
    def q(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return self._run(sql, params)[0]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self._run(sql, params)[1]

    def insert(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        cols = list(row)
        self.x(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
               [row[c] for c in cols])
        return row

    def init_schema(self) -> None:
        ddl = SCHEMA.replace("{TS}", "TIMESTAMPTZ" if self.kind == "postgres" else "TEXT")
        ddl = ddl.replace("{JSON}", "JSONB" if self.kind == "postgres" else "TEXT")
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            self.x(stmt)
        if not self.one("SELECT id FROM settings WHERE id = 1"):
            from .store import now_iso
            self.x("INSERT INTO settings (id, updated_at) VALUES (1, %s)", [now_iso()])

    def ping(self) -> str:
        if self.kind == "postgres":
            v = self.one("SELECT version() AS v")["v"]
            return v.split(",")[0]
        return f"SQLite {sqlite3.sqlite_version} ({self.sqlite_path})"

    def describe(self) -> str:
        if self.kind == "postgres":
            host = self.url.split("@")[-1].split("/")[0]
            return f"Postgres @ {host}"
        return f"SQLite {self.sqlite_path.name}"


_DB: Dict[str, Database] = {}
_DB_LOCK = threading.Lock()


def get_db(url: Optional[str], sqlite_path: Path) -> Database:
    key = f"{url}|{sqlite_path}"
    with _DB_LOCK:
        if key not in _DB:
            _DB[key] = Database(url, sqlite_path)
        return _DB[key]
