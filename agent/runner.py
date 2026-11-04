"""One agent run: Trigger -> Load data -> Load overrides -> Rules -> LLM agent (per portfolio) ->
Validate / Fallback -> Policy gate -> Email / Console / Review queue -> Audit."""
from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from .actions import execute
from .audit import audit_context, install_guards, migrate_legacy
from .agent_loop import merge_assessment, run_agent_loop
from .config import Config, load_config
from .datasource import DataSourceError, DbDataSource
from .db import DbError, get_db
from .mailer import Mailer
from .llm import LLMError, get_llm
from .models import Finding
from .policy import decide
from .rules import evaluate_all
from .store import State
from .timeutil import parse_as_of
from .trace import Tracer, new_run_id, use_db

SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AGENTICPM_SERVERLESS"))


_SCHEMA_READY = set()


def get_state(cfg: Optional[Config] = None) -> State:
    cfg = cfg or load_config()
    db = get_db(cfg.database_url, cfg.sqlite_path)
    key = (db.kind, cfg.database_url, str(cfg.sqlite_path))
    if key not in _SCHEMA_READY:
        db.init_schema()
        install_guards(db)
        state = State(cfg.state_dir, cfg.data_dir, db)
        migrate_legacy(db, state.trail)
        from .config import set_overlay
        set_overlay(state.config.all())  # settings saved from the Settings page
        if SERVERLESS or os.environ.get("TRACE_BACKEND") == "db":
            use_db(db)
        _SCHEMA_READY.add(key)
        return state
    state = State(cfg.state_dir, cfg.data_dir, db)
    if SERVERLESS:
        # several instances may serve requests; pick up Settings changes made on another one
        from .config import set_overlay
        set_overlay(state.config.all())
    return state


def run_once(mode: Optional[str] = None, llm_kind: Optional[str] = None, as_of: Optional[str] = None,
             trigger: str = "manual", portfolio_ids: Optional[List[str]] = None, step_delay: float = 0.0,
             run_id: Optional[str] = None, echo: bool = True, triggered_by: Optional[str] = None) -> Dict[str, Any]:
    run_id = run_id or new_run_id()
    # Everything the agent does in this run is audited as actor "agent", tagged with the run id.
    with audit_context(actor="agent", actor_role="SYSTEM", run_id=run_id):
        return _run(mode, llm_kind, as_of, trigger, portfolio_ids, step_delay, run_id, echo, triggered_by)


def _run(mode, llm_kind, as_of, trigger, portfolio_ids, step_delay, run_id, echo, triggered_by) -> Dict[str, Any]:
    t_start = time.time()
    cfg = load_config()
    llm_kind = llm_kind or cfg.llm_kind
    as_of = as_of or cfg.demo_as_of or None
    mode = mode or cfg.mode
    if mode not in ("auto", "assist", "shadow"):
        raise ValueError("mode must be auto, assist or shadow")
    try:
        state = get_state(cfg)
    except DbError as e:
        if echo:
            print(f"✖ database unavailable: {e}\n  Fail-closed: no findings, no emails.")
        return {"run_id": run_id, "status": "aborted", "error": str(e)}
    now = parse_as_of(as_of)
    tr = Tracer(state.dir / "runs", run_id, step_delay=step_delay, echo=echo)
    meta = {"mode": mode, "llm": llm_kind, "as_of": now.isoformat(), "trigger": trigger,
            "portfolios": portfolio_ids or "all", "db": state.db.describe(),
            "email": cfg.agent_email_delivery if cfg.has_resend else "simulated"}
    tr.emit("run", "start", **meta)
    state.audit("run.started", target_type="run", target_id=run_id,
                summary=f"{trigger} run · mode {mode} · llm {llm_kind} · as of {now:%Y-%m-%d}",
                triggered_by=triggered_by or ("scheduler" if trigger == "schedule" else "cli"), **meta)
    if echo:
        print(f"\n▶ {run_id}  trigger={trigger} mode={mode} llm={llm_kind} as_of={now:%Y-%m-%d}")

    with tr.node("trigger", **meta) as o:
        o["summary"] = f"{trigger} trigger"

    # 1. OBSERVE --------------------------------------------------------------------
    tr.flow("trigger", "load_data")
    try:
        with tr.node("load_data") as o:
            snap = DbDataSource(state.db).load()
            if portfolio_ids:
                snap.portfolios = [p for p in snap.portfolios if p["id"] in portfolio_ids]
            o["summary"] = f"{len(snap.portfolios)} portfolios, {len(snap.submissions)} monthly updates read"
            state.audit("data.loaded", target_type="run", target_id=run_id, summary=o["summary"],
                        portfolios=[p["id"] for p in snap.portfolios], submissions=len(snap.submissions),
                        unassigned_portfolios=[p["id"] for p in snap.portfolios if not p.get("pm_email")])
    except DataSourceError as e:
        try:
            state.audit("run.aborted", outcome="failure", target_type="run", target_id=run_id,
                        summary="data source unavailable: nothing evaluated, nothing sent", reason=str(e))
        except DbError:
            pass  # database itself is down; the trace file still records the abort
        tr.flow("load_data", "audit", label="abort")
        with tr.node("audit") as o:
            o["summary"] = "check stopped safely; nothing sent"
        tr.emit("run", "error", error=str(e), summary="Data source unavailable: run aborted safely, nothing sent")
        if echo:
            print(f"✖ data source failure: {e}\n  Fail-closed: no findings, no emails. Ops alert raised.")
        return {"run_id": run_id, "status": "aborted", "error": str(e)}

    tr.flow("load_data", "load_overrides")
    with tr.node("load_overrides") as o:
        oview = state.overrides.view()
        n_exc = sum(1 for x in oview.items if x["type"] in ("SUPPRESS", "DISMISS"))
        o["summary"] = f"{n_exc} exception{'s' if n_exc != 1 else ''} applied" + (" · assistant paused" if oview.paused() else "")
        o["overrides"] = oview.items
        state.audit("data.overrides_loaded", target_type="run", target_id=run_id, summary=o["summary"],
                    count=len(oview.items), paused=oview.paused())

    # 2. EVALUATE (deterministic) ----------------------------------------------------
    tr.flow("load_overrides", "rules")
    with tr.node("rules") as o:
        rule_findings = evaluate_all(snap, now)
        flat = [f for fs in rule_findings.values() for f in fs]
        o["summary"] = f"{len(flat)} possible issues in {sum(1 for v in rule_findings.values() if v)} portfolios"
        o["findings"] = [f.to_dict() for f in flat]
        state.audit("rules.evaluated", target_type="run", target_id=run_id, summary=o["summary"],
                    by_rule=dict(Counter(f.rule_id for f in flat)),
                    findings=[{"portfolio": f.portfolio_id, "rule": f.rule_id, "month": f.reporting_month,
                               "severity": f.severity, "fingerprint": f.fingerprint} for f in flat])

    # 3. EVALUATE (agentic, per portfolio) ---------------------------------------------
    llm = None
    llm_init_error = ""
    try:
        llm = get_llm(llm_kind, cfg)
    except LLMError as e:
        llm_init_error = str(e)
    all_findings: List[Finding] = []
    llm_stats = Counter()
    for p in snap.portfolios:
        fs = rule_findings[p["id"]]
        if not snap.history(p["id"]) and not fs:
            continue
        tr.flow("rules", "llm_agent", label=p["id"])
        res = None
        with tr.node("llm_agent", portfolio=p["id"], portfolio_name=p["name"], model=getattr(llm, "model", llm_kind)) as o:
            if llm is None:
                o["summary"] = f"{p['name']}: AI not available"
                o["ok"] = False
            else:
                t0 = time.time()
                res = run_agent_loop(llm, snap, p, fs, oview, now, cfg, tr)
                a = res.assessment or {}
                state.audit("llm.assessment", outcome="success" if res.ok else "failure", target_type="portfolio",
                            target_id=p["id"], summary=f"{p['id']}: {'assessed' if res.ok else 'FAILED'} by "
                                                       f"{getattr(llm, 'model', llm_kind)} in {res.steps} step(s)",
                            provider=llm_kind, model=getattr(llm, "model", llm_kind), steps=res.steps,
                            tool_calls=res.tool_calls, error=res.error or None, latency_ms=int((time.time() - t0) * 1000),
                            usage=res.usage, rule_reviews=[{k: r.get(k) for k in ("rule_id", "reporting_month", "verdict",
                                                                                  "confidence")} for r in a.get("rule_reviews", [])],
                            new_findings=[{k: n.get(k) for k in ("rule_id", "reporting_month", "severity", "confidence",
                                                                  "recommended_action")} for n in a.get("new_findings", [])],
                            llm_summary=a.get("summary"))
                o.update(ok=res.ok, steps=res.steps, tool_calls=res.tool_calls, error=res.error,
                         assessment=res.assessment,
                         summary=f"{p['name']}: " + ("reviewed" if res.ok else "AI review failed, using rules only"))
        if res and res.ok:
            llm_stats["ok"] += 1
            tr.flow("llm_agent", "validate", label=p["id"])
            with tr.node("validate", portfolio=p["id"]) as o:
                merged = merge_assessment(fs, res.assessment, snap, p)
                ungrounded = [f.rule_id for f in merged if not f.grounded]
                o["summary"] = f"{p['name']}: " + (f"{len(ungrounded)} AI claim(s) didn't match the records" if ungrounded
                                                    else "facts checked")
                o["findings"] = [f.to_dict() for f in merged]
                for f in merged:
                    if not f.grounded:
                        state.audit("llm.ungrounded_claim", outcome="failure", target_type="finding",
                                    target_id=f.fingerprint, summary=f"{p['id']} {f.rule_id}: {f.llm_note}",
                                    portfolio_id=p["id"], evidence=f.evidence)
        else:
            llm_stats["fallback"] += 1
            tr.flow("llm_agent", "fallback", label=p["id"])
            with tr.node("fallback", portfolio=p["id"]) as o:
                merged = fs
                for f in merged:
                    f.source = "rule (llm unavailable)"
                o["summary"] = f"{p['name']}: standard rules only; judgement calls passed to you"
                state.audit("llm.fallback", outcome="failure", target_type="portfolio", target_id=p["id"],
                            summary=o["summary"], reason=(res.error if res else llm_init_error))
                o["findings"] = [f.to_dict() for f in merged]
        all_findings.extend(merged)

    # 4. DECIDE ------------------------------------------------------------------------
    if llm_stats["ok"]:
        tr.flow("validate", "policy", count=llm_stats["ok"])
    if llm_stats["fallback"]:
        tr.flow("fallback", "policy", count=llm_stats["fallback"])
    with tr.node("policy", mode=mode) as o:
        decisions = decide(all_findings, snap, state, mode, cfg, now, oview)
        c = Counter(d.decision for d in decisions)
        words = {"execute": "done automatically", "queue": "for you to decide", "suppressed": "skipped (exception)",
                 "duplicate": "already handled", "shadow": "practice only", "log": "noted"}
        o["summary"] = ", ".join(f"{v} {words.get(k, k)}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])) or "nothing to do"
        o["decisions"] = [d.to_dict() for d in decisions]

    # 5. ACT ---------------------------------------------------------------------------
    mailer = Mailer(cfg, state, echo=echo)
    n_exec = sum(1 for d in decisions if d.decision == "execute")
    n_queue = sum(1 for d in decisions if d.decision == "queue")
    if n_exec:
        tr.flow("policy", "send_email", count=n_exec)
    if n_queue:
        tr.flow("policy", "review_queue", count=n_queue)
    out = execute(decisions, snap, state, run_id, mailer)
    if out["emails"] or out["failed"]:
        with tr.node("send_email") as o:
            kinds = Counter(m["delivery"] for m in out["emails"])
            o["summary"] = (f"{len(out['emails'])} email{'s' if len(out['emails']) != 1 else ''} "
                            + ("(practice, not sent)" if set(kinds) <= {"simulated"} else "sent")
                            + (f" · {len(out['failed'])} failed, will retry" if out["failed"] else ""))
            o["emails"] = out["emails"] + out["failed"]
        if out["failed"]:
            tr.emit("send_email", "error", error=f"{len(out['failed'])} email(s) failed: {out['failed'][0]['error']}")
    if n_queue:
        with tr.node("review_queue") as o:
            o["summary"] = f"{len(out['queued'])} waiting for your decision"
            o["queued"] = out["queued"]

    tr.flow("policy", "console")
    with tr.node("console") as o:
        report = console_report(decisions, out, llm_stats)
        if echo:
            print(report)
        o["summary"] = "summary written"
        o["report"] = report

    # 6. AUDIT -------------------------------------------------------------------------
    for src, n in (("send_email", len(out["emails"])), ("review_queue", n_queue), ("console", 1)):
        if n:
            tr.flow(src, "audit")
    with tr.node("audit") as o:
        for d in decisions:
            f = d.finding
            state.audit("decision.made", target_type="finding", target_id=f.fingerprint,
                        summary=f"{f.portfolio_id} {f.rule_id} {f.reporting_month}: {d.action_type} → {d.decision}",
                        decision=d.decision, action_type=d.action_type, recipient=d.recipient, reason=d.reason,
                        action_fingerprint=d.action_fingerprint, portfolio_id=f.portfolio_id, rule_id=f.rule_id,
                        reporting_month=f.reporting_month, severity=f.severity, confidence=f.confidence,
                        source=f.source, grounded=f.grounded, disputed=f.disputed, rationale=f.rationale,
                        llm_note=f.llm_note, evidence=f.evidence)
        state.findings.record_run(run_id, all_findings, decisions, [p["id"] for p in snap.portfolios])
        state.audit("findings.recorded", target_type="run", target_id=run_id,
                    summary=f"{len(all_findings)} finding(s) stored", count=len(all_findings))
        o["summary"] = f"{len(decisions)} decisions recorded"

    summary = {"run_id": run_id, "status": "ok", "findings": len(all_findings),
               "decisions": dict(Counter(d.decision for d in decisions)), "emails": len(out["emails"]),
               "email_failures": len(out["failed"]),
               "queued": len(out["queued"]), "llm": dict(llm_stats)}
    tr.emit("run", "end", summary=summary)
    state.audit("run.completed", target_type="run", target_id=run_id, duration_ms=int((time.time() - t_start) * 1000),
                summary=f"{summary['findings']} findings · {summary['emails']} emails · {summary['queued']} held · "
                        f"decisions {summary['decisions']}", result=summary)
    return summary


def console_report(decisions, out, llm_stats) -> str:
    icon = {"execute": "✅ SENT ", "queue": "⏸  HELD ", "suppressed": "🔕 SUPPR", "duplicate": "↺  DUP  ",
            "shadow": "👁  SHADW", "log": "ℹ  LOG  "}
    rows = [f"  {icon.get(d.decision, d.decision):8} {d.finding.portfolio_id} {d.finding.rule_id:<24} "
            f"{d.action_type:<15} conf={d.finding.confidence:.2f}  {d.reason}" for d in decisions]
    return ("\n══ PM compliance run report " + "═" * 44 + "\n" + "\n".join(rows) +
            f"\n  LLM: {dict(llm_stats)}  emails: {len(out['emails'])}  queued for review: {len(out['queued'])}\n")
