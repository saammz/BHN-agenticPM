"""Run tracer: every node start/end/error and every edge traversal is appended to
state/runs/<run_id>.jsonl. The workflow visualizer replays/streams these events."""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import WAT

_LOCK = threading.Lock()
# When set (serverless / TRACE_BACKEND=db), run traces live in the run_events table instead of JSONL files,
# because a serverless filesystem is temporary and not shared between instances.
_DB = None


def use_db(db) -> None:
    global _DB
    _DB = db


def new_run_id() -> str:
    return datetime.now(WAT).strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


class Tracer:
    def __init__(self, runs_dir: Path, run_id: str, step_delay: float = 0.0, echo: bool = False):
        self.path = Path(runs_dir) / f"{run_id}.jsonl"
        self.run_id = run_id
        self.step_delay = step_delay
        self.echo = echo
        # continue numbering when events are appended to an existing run (e.g. a human decision later on)
        if _DB is not None:
            row = _DB.one("SELECT MAX(seq) AS m FROM run_events WHERE run_id = %s", [run_id])
            self.seq = int(row["m"] or 0) if row else 0
        elif self.path.exists():
            self.seq = sum(1 for line in self.path.open() if line.strip())
        else:
            self.seq = 0

    def emit(self, node: str, status: str, **data: Any) -> None:
        with _LOCK:
            self.seq += 1
            evt = {"seq": self.seq, "ts": time.time(), "run_id": self.run_id, "node": node, "status": status, **data}
            if _DB is not None:
                payload = json.loads(json.dumps(data, default=str))
                _DB.insert("run_events", {"run_id": self.run_id, "seq": self.seq, "ts": evt["ts"], "node": node,
                                          "status": status, "data": payload})
            else:
                with self.path.open("a") as f:
                    f.write(json.dumps(evt, default=str) + "\n")
        if self.echo and status in ("error", "end") and node not in ("tools",):
            msg = data.get("summary") or data.get("error") or ""
            print(f"  [{node:<14}] {status:<5} {msg}")
    def flow(self, src: str, dst: str, count: int = 1, label: str = "") -> None:
        self.emit(dst, "flow", src=src, count=count, label=label)
        if self.step_delay:
            time.sleep(self.step_delay)

    @contextmanager
    def node(self, name: str, **data: Any) -> Iterator[Dict[str, Any]]:
        out: Dict[str, Any] = {}
        self.emit(name, "start", **data)
        t0 = time.time()
        if self.step_delay:
            time.sleep(self.step_delay)
        try:
            yield out
        except Exception as e:  # noqa: BLE001 - recorded then re-raised
            self.emit(name, "error", error=f"{type(e).__name__}: {e}", ms=int((time.time() - t0) * 1000), **data)
            raise
        self.emit(name, "end", ms=int((time.time() - t0) * 1000), **data, **out)


def _evt(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"seq": int(r["seq"]), "ts": float(r["ts"]), "run_id": r["run_id"], "node": r["node"], "status": r["status"],
            **(r.get("data") or {})}


def read_events(runs_dir: Path, run_id: str, since: int = 0) -> List[Dict[str, Any]]:
    if _DB is not None:
        return [_evt(r) for r in _DB.q("SELECT * FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq",
                                       [run_id, since])]
    path = Path(runs_dir) / f"{run_id}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            if e["seq"] > since:
                out.append(e)
    return out


def list_runs(runs_dir: Path) -> List[Dict[str, Any]]:
    if _DB is not None:
        rows = _DB.q("SELECT * FROM run_events WHERE node = 'run' ORDER BY run_id DESC, seq LIMIT 400")
        runs: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            item = runs.setdefault(r["run_id"], {"run_id": r["run_id"], "started": None, "finished": None})
            if r["status"] == "start":
                item["started"] = _evt(r)
            elif r["status"] in ("end", "error"):
                item["finished"] = _evt(r)
        return sorted(runs.values(), key=lambda x: x["run_id"], reverse=True)
    runs_l = []
    for p in sorted(Path(runs_dir).glob("run-*.jsonl"), reverse=True):
        first: Optional[Dict[str, Any]] = None
        last: Optional[Dict[str, Any]] = None
        with p.open() as f:
            for line in f:
                if line.strip():
                    e = json.loads(line)
                    if e["node"] == "run" and e["status"] == "start":
                        first = e
                    if e["node"] == "run" and e["status"] in ("end", "error"):
                        last = e
        runs_l.append({"run_id": p.stem, "started": first, "finished": last})
    return runs_l
