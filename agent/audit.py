"""Tamper-evident audit trail.

Every entry records who (actor, role, id), from where (IP, user agent), what (event, target, outcome, data) and
when, plus:
  seq        a gap-free sequence number
  payload    the canonical JSON of all the fields above, exactly as hashed
  prev_hash  the previous entry's hash
  hash       SHA-256(prev_hash + payload)
Changing or deleting any entry breaks the chain, and `verify()` reports the first broken sequence number. The
database also refuses UPDATE, DELETE and TRUNCATE on the table (triggers), so the trail is append-only for the
application. Database owners can still drop triggers; export regularly (CSV) to keep an off-site copy."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from .config import WAT
from .db import Database, DbError

GENESIS = "0" * 64
HASHED = ("id", "seq", "ts", "category", "event", "outcome", "actor", "actor_role", "actor_id", "ip", "user_agent",
          "target_type", "target_id", "run_id", "summary", "data")
CATEGORIES = ("auth", "access", "http", "run", "data", "rules", "llm", "decision", "review", "email", "override",
              "dispute", "user", "portfolio", "webhook", "audit", "system", "pm", "settings", "legacy")
MAX_DATA_CHARS = 20_000

_ctx = threading.local()
_write_lock = threading.Lock()


def _context() -> Dict[str, Any]:
    return getattr(_ctx, "values", {})


@contextmanager
def audit_context(**values: Any) -> Iterator[None]:
    """Who/where for everything audited in this thread (set per HTTP request, per run, per CLI command)."""
    old = dict(_context())
    _ctx.values = {**old, **{k: v for k, v in values.items() if v is not None}}
    try:
        yield
    finally:
        _ctx.values = old


def set_context(**values: Any) -> None:
    _ctx.values = {**_context(), **{k: v for k, v in values.items() if v is not None}}


def clear_context() -> None:
    _ctx.values = {}


def _safe(data: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(data, default=str))
    text = json.dumps(out)
    if len(text) > MAX_DATA_CHARS:
        out = {"truncated": True, "original_chars": len(text), "preview": text[:MAX_DATA_CHARS]}
    return out


def canonical(row: Dict[str, Any]) -> str:
    return json.dumps({k: row.get(k) for k in HASHED}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuditTrail:
    def __init__(self, db: Database):
        self.db = db

    # ── write ─────────────────────────────────────────────────────────────────────────
    def write(self, event: str, *, outcome: str = "success", actor: Optional[str] = None,
              actor_role: Optional[str] = None, actor_id: Optional[str] = None, ip: Optional[str] = None,
              user_agent: Optional[str] = None, target_type: Optional[str] = None, target_id: Optional[str] = None,
              run_id: Optional[str] = None, summary: Optional[str] = None, **data: Any) -> Dict[str, Any]:
        ctx = _context()
        row: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(WAT).isoformat(timespec="microseconds"),
            "category": event.split(".")[0][:20],
            "event": event[:60],
            "outcome": outcome,
            "actor": (actor or ctx.get("actor") or "system")[:254],
            "actor_role": (actor_role or ctx.get("actor_role") or "SYSTEM")[:8],
            "actor_id": actor_id or ctx.get("actor_id"),
            "ip": (ip or ctx.get("ip") or None),
            "user_agent": ((user_agent or ctx.get("user_agent") or "")[:512]) or None,
            "target_type": target_type,
            "target_id": str(target_id)[:80] if target_id is not None else None,
            "run_id": run_id or ctx.get("run_id"),
            "summary": (summary or "")[:500] or None,
            "data": _safe(data),
        }
        db = self.db
        with _write_lock:
            for _ in range(6):
                # (seq, hash) of the chain's tail, cached on the Database object so each write is one round trip
                if getattr(db, "_audit_tail", None) is None:
                    last = db.one("SELECT seq, hash FROM audit_trail ORDER BY seq DESC LIMIT 1")
                    db._audit_tail = (int(last["seq"]), last["hash"]) if last else (0, GENESIS)
                seq, prev = db._audit_tail
                row["seq"] = seq + 1
                payload = canonical(row)
                h = hashlib.sha256((prev + payload).encode()).hexdigest()
                try:
                    self.db.insert("audit_trail", {**row, "payload": payload, "prev_hash": prev, "hash": h})
                except DbError as e:
                    if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                        db._audit_tail = None  # another process appended; re-read the tail and retry
                        continue
                    raise
                db._audit_tail = (row["seq"], h)
                return {**row, "hash": h, "prev_hash": prev}
        raise DbError("could not append to audit trail after retries")

    # ── read ──────────────────────────────────────────────────────────────────────────
    def _where(self, category=None, q=None, outcome=None, actor=None, since=None, until=None, run_id=None,
               target_id=None, before_seq=None):
        where, params = [], []
        if category:
            where.append("category = %s"); params.append(category)
        if outcome:
            where.append("outcome = %s"); params.append(outcome)
        if actor:
            where.append("LOWER(actor) LIKE %s"); params.append(f"%{actor.lower()}%")
        if run_id:
            where.append("run_id = %s"); params.append(run_id)
        if target_id:
            where.append("target_id = %s"); params.append(target_id)
        if since:
            where.append("ts >= %s"); params.append(since)
        if until:
            where.append("ts <= %s"); params.append(until)
        if before_seq:
            where.append("seq < %s"); params.append(int(before_seq))
        if q:
            like = f"%{q.lower()}%"
            where.append("(LOWER(event) LIKE %s OR LOWER(COALESCE(actor,'')) LIKE %s OR LOWER(COALESCE(target_id,'')) "
                         "LIKE %s OR LOWER(COALESCE(summary,'')) LIKE %s OR LOWER(COALESCE(ip,'')) LIKE %s "
                         "OR LOWER(COALESCE(run_id,'')) LIKE %s)")
            params += [like] * 6
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list(self, limit: int = 100, **filters: Any) -> List[Dict[str, Any]]:
        w, p = self._where(**filters)
        rows = self.db.q(f"SELECT id, seq, ts, category, event, outcome, actor, actor_role, actor_id, ip, user_agent, "
                         f"target_type, target_id, run_id, summary, data, hash, prev_hash FROM audit_trail{w} "
                         f"ORDER BY seq DESC LIMIT %s", p + [max(1, min(int(limit), 1000))])
        return rows

    def stats(self) -> Dict[str, Any]:
        rows = self.db.q("SELECT category, COUNT(*) AS n FROM audit_trail GROUP BY category")
        last = self.db.one("SELECT seq, ts, hash FROM audit_trail ORDER BY seq DESC LIMIT 1")
        return {"by_category": {r["category"]: int(r["n"]) for r in rows}, "last": last}

    def export_csv(self, **filters: Any) -> str:
        w, p = self._where(**filters)
        rows = self.db.q(f"SELECT seq, ts, category, event, outcome, actor, actor_role, ip, user_agent, target_type, "
                         f"target_id, run_id, summary, data, prev_hash, hash FROM audit_trail{w} ORDER BY seq", p)
        buf = io.StringIO()
        cols = ["seq", "ts", "category", "event", "outcome", "actor", "actor_role", "ip", "user_agent", "target_type",
                "target_id", "run_id", "summary", "data", "prev_hash", "hash"]
        wr = csv.DictWriter(buf, fieldnames=cols)
        wr.writeheader()
        for r in rows:
            r["data"] = json.dumps(r.get("data"), ensure_ascii=False, sort_keys=True)
            # neutralise spreadsheet formula injection (values starting with = + - @)
            wr.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items() if k in cols})
        return buf.getvalue()

    # ── verify ────────────────────────────────────────────────────────────────────────
    def verify(self) -> Dict[str, Any]:
        prev, expected_seq, checked, after = GENESIS, 1, 0, 0
        while True:
            batch = self.db.q("SELECT seq, event, outcome, actor, target_id, run_id, ip, payload, prev_hash, hash "
                              "FROM audit_trail WHERE seq > %s ORDER BY seq LIMIT 1000", [after])
            if not batch:
                break
            for r in batch:
                seq = int(r["seq"])
                problem = None
                if seq != expected_seq:
                    problem = f"sequence gap: expected {expected_seq}, found {seq} (entries deleted?)"
                elif r["prev_hash"] != prev:
                    problem = "prev_hash does not match the previous entry (chain broken)"
                elif hashlib.sha256((prev + r["payload"]).encode()).hexdigest() != r["hash"]:
                    problem = "hash does not match the stored payload (entry altered)"
                else:
                    pl = json.loads(r["payload"])
                    for k in ("seq", "event", "outcome", "actor", "target_id", "run_id", "ip"):
                        if pl.get(k) != (seq if k == "seq" else r[k]):
                            problem = f"column {k!r} differs from the hashed payload (entry altered)"
                            break
                if problem:
                    return {"ok": False, "checked": checked, "broken_at": seq, "reason": problem}
                prev, expected_seq, checked, after = r["hash"], seq + 1, checked + 1, seq
        return {"ok": True, "checked": checked, "broken_at": None, "head": prev}


def install_guards(db: Database) -> None:
    """Append-only enforcement at the database level."""
    if db.kind == "postgres":
        db.x("CREATE OR REPLACE FUNCTION audit_trail_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ "
             "BEGIN RAISE EXCEPTION 'audit_trail is append-only'; END $$")
        db.x("CREATE OR REPLACE TRIGGER audit_trail_no_change BEFORE UPDATE OR DELETE ON audit_trail "
             "FOR EACH ROW EXECUTE FUNCTION audit_trail_append_only()")
        db.x("CREATE OR REPLACE TRIGGER audit_trail_no_truncate BEFORE TRUNCATE ON audit_trail "
             "FOR EACH STATEMENT EXECUTE FUNCTION audit_trail_append_only()")
    else:
        db.x("CREATE TRIGGER IF NOT EXISTS audit_trail_no_update BEFORE UPDATE ON audit_trail "
             "BEGIN SELECT RAISE(ABORT, 'audit_trail is append-only'); END")
        db.x("CREATE TRIGGER IF NOT EXISTS audit_trail_no_delete BEFORE DELETE ON audit_trail "
             "BEGIN SELECT RAISE(ABORT, 'audit_trail is append-only'); END")


def migrate_legacy(db: Database, trail: AuditTrail) -> int:
    """Move rows from the old unchained audit_log table (v0.2) into the chained trail, then drop it."""
    if db.kind == "postgres":
        exists = db.one("SELECT to_regclass('public.audit_log') AS t")["t"]
    else:
        exists = db.one("SELECT name AS t FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'")
    if not exists:
        return 0
    rows = db.q("SELECT * FROM audit_log ORDER BY ts")
    for r in rows:
        trail.write(f"legacy.{r['event']}", actor=r.get("actor") or "system", run_id=r.get("run_id"),
                    summary="migrated from unchained audit_log", original_ts=r["ts"], **(r.get("data") or {}))
    db.x("DROP TABLE audit_log")
    return len(rows)
