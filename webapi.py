"""Plain-language endpoints for the CoS app: overview, activity, portfolio detail, and the Settings page."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from agent.config import EDITABLE, WAT, Config, load_config, set_overlay, write_env_secret
from agent.mailer import Mailer, resend_send, MailError
from agent.store import State
from agent.trace import list_runs

MODELS = [
    {"id": "anthropic/claude-sonnet-5.5", "label": "Claude Sonnet 5.5", "note": "Recommended: best judgement"},
    {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "note": "Previous generation, slightly cheaper"},
    {"id": "openai/gpt-4o-mini", "label": "GPT-4o mini", "note": "Fastest and cheapest, weaker judgement"},
]
ACTIVITY_CATEGORIES = ("email", "review", "dispute", "override", "user", "portfolio", "run", "settings")
ACTIVITY_SKIP_EVENTS = ("run.started", "run.requested", "email.console")
RULE_KEYS = {"deadline_day": (int, 1, 28), "reminder_window_days": (int, 1, 20),
             "persistent_red_months": (int, 2, 12), "cash_drop_threshold": (float, 0.1, 0.9)}
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class BadRequest(ValueError):
    pass


def _hint(secret: str) -> Optional[str]:
    return f"…{secret[-4:]}" if secret else None


def _next_check(cfg: Config) -> Optional[str]:
    if not cfg.daily_check_time:
        return None
    hh, mm = (int(x) for x in cfg.daily_check_time.split(":"))
    now = datetime.now(WAT)
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt.isoformat()


# ── Overview ────────────────────────────────────────────────────────────────────────────
def overview(state: State, cfg: Config) -> Dict[str, Any]:
    db = state.db
    ports = db.q("SELECT p.id, p.name, u.name AS pm_name, u.email AS pm_email FROM portfolios p "
                 "LEFT JOIN users u ON u.id = p.pm_user_id WHERE p.active = %s ORDER BY p.id", [True])
    open_f = db.q("SELECT fingerprint, portfolio_id, rule_id, reporting_month, last_decision, finding FROM findings "
                  "WHERE status = 'open'")
    pending = [r for r in state.review.all() if r["status"] == "pending"]
    disputes = [d for d in state.disputes.list() if d["status"] == "OPEN"]
    subs = db.q("SELECT portfolio_id, reporting_month, rag_status, submitted_at FROM submissions")
    latest: Dict[str, Dict[str, Any]] = {}
    for s in subs:
        if s["portfolio_id"] not in latest or s["reporting_month"] > latest[s["portfolio_id"]]["reporting_month"]:
            latest[s["portfolio_id"]] = s
    rows = []
    for p in ports:
        pid = p["id"]
        f = [x for x in open_f if x["portfolio_id"] == pid]
        waiting = [r for r in pending if r["finding"]["portfolio_id"] == pid] + [d for d in disputes if d["portfolio_id"] == pid]
        acted = [x for x in f if x["last_decision"] in ("execute", "duplicate", "queue")]
        if waiting:
            status = "needs_you"
        elif any(x["rule_id"] == "MISSING_SUBMISSION" for x in f):
            status = "overdue"
        elif acted:
            status = "reminded"
        elif any(x["last_decision"] == "suppressed" for x in f):
            status = "exception"
        else:
            status = "on_track"
        issues = [{"rule_id": x["rule_id"], "month": x["reporting_month"],
                   "severity": (x["finding"] or {}).get("severity"), "decision": x["last_decision"]}
                  for x in f if x["last_decision"] not in ("log",)]
        rows.append({**p, "status": status, "issues": issues, "waiting": len(waiting), "latest": latest.get(pid)})
    month_start = datetime.now(WAT).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    reminders = db.one("SELECT COUNT(*) AS n FROM email_log WHERE kind LIKE %s AND ts >= %s AND status <> 'failed'",
                       ["remind_pm%", month_start])["n"]
    runs = [r for r in list_runs(state.dir / "runs") if r["finished"]]
    last = runs[0] if runs else None
    return {
        "portfolios": rows,
        "kpis": {"total": len(rows), "on_track": sum(1 for r in rows if r["status"] in ("on_track", "exception")),
                 "needs_you": len(pending) + len(disputes), "reminders_this_month": int(reminders),
                 "overdue": sum(1 for r in rows if r["status"] == "overdue")},
        "last_check": {"run_id": last["run_id"], "at": last["finished"].get("ts"),
                       "ok": last["finished"]["status"] == "end",
                       "summary": last["finished"].get("summary") if isinstance(last["finished"].get("summary"), dict) else None,
                       "trigger": (last["started"] or {}).get("trigger")} if last else None,
        "next_check": _next_check(cfg),
        "assistant": {"mode": cfg.mode, "paused": state.overrides.paused(), "ai": cfg.llm_kind,
                      "model": cfg.openrouter_model, "emails": cfg.agent_email_delivery if cfg.has_resend else "simulated",
                      "redirect_to": cfg.agent_email_redirect_to},
    }


def activity(state: State, limit: int = 60, portfolio_id: Optional[str] = None) -> List[Dict[str, Any]]:
    ph = ", ".join(["%s"] * len(ACTIVITY_CATEGORIES))
    ph2 = ", ".join(["%s"] * len(ACTIVITY_SKIP_EVENTS))
    rows = state.db.q(f"SELECT seq, ts, category, event, outcome, actor, actor_role, target_id, summary, data FROM audit_trail "
                      f"WHERE category IN ({ph}) AND event NOT IN ({ph2}) ORDER BY seq DESC LIMIT %s",
                      [*ACTIVITY_CATEGORIES, *ACTIVITY_SKIP_EVENTS, max(1, min(limit, 300))])
    out = []
    for r in rows:
        d = r.get("data") or {}
        if r["event"].startswith("email.") and d.get("kind") in ("auth_code", "auth_alert"):
            continue  # sign-in emails belong to Security, not the compliance timeline
        if portfolio_id and d.get("portfolio_id") != portfolio_id and r.get("target_id") != portfolio_id:
            continue
        out.append(r)
    return out


def portfolio_detail(state: State, pid: str) -> Dict[str, Any]:
    db = state.db
    p = db.one("SELECT p.id, p.name, p.pm_user_id, u.name AS pm_name, u.email AS pm_email FROM portfolios p "
               "LEFT JOIN users u ON u.id = p.pm_user_id WHERE p.id = %s", [pid])
    if not p:
        raise KeyError("portfolio not found")
    return {
        "portfolio": p,
        "submissions": db.q("SELECT id, reporting_month, rag_status, cash_position_ngn, execution_blocker, submitted_at "
                            "FROM submissions WHERE portfolio_id = %s ORDER BY reporting_month DESC", [pid]),
        "findings": state.findings.for_portfolios([pid]),
        "emails": [e for e in db.q("SELECT ts, to_email, subject, kind, status, delivery, body FROM email_log "
                                   "WHERE portfolio_id = %s ORDER BY ts DESC LIMIT 30", [pid])],
        "disputes": state.disputes.list([pid]),
        "pms": db.q("SELECT id, name, email FROM users WHERE role = 'PM' AND status = 'ACTIVE' ORDER BY name"),
    }


# ── Settings ────────────────────────────────────────────────────────────────────────────
def settings_get(state: State, cfg: Config) -> Dict[str, Any]:
    rules = state.db.one("SELECT deadline_day, reminder_window_days, persistent_red_months, cash_drop_threshold "
                         "FROM settings WHERE id = 1") or {}
    return {
        "assistant": {"mode": cfg.mode, "paused": state.overrides.paused(), "daily_check_time": cfg.daily_check_time,
                      "next_check": _next_check(cfg)},
        "ai": {"llm_kind": cfg.llm_kind, "openrouter_model": cfg.openrouter_model, "conf_min_act": cfg.conf_min_act,
               "conf_auto_remind": cfg.conf_auto_remind, "models": MODELS},
        "rules": rules,
        "email": {"email_from": cfg.email_from, "agent_email_delivery": cfg.agent_email_delivery,
                  "agent_email_redirect_to": cfg.agent_email_redirect_to},
        "connections": {
            "openrouter": {"connected": cfg.has_openrouter_key, "hint": _hint(cfg.openrouter_api_key)},
            "resend": {"connected": bool(cfg.resend_api_key), "hint": _hint(cfg.resend_api_key),
                       "sender_ok": bool(cfg.email_from)},
            "database": {"label": state.db.describe(), "kind": state.db.kind},
        },
        "advanced": {"demo_as_of": cfg.demo_as_of, "app_url": cfg.app_url},
    }


def settings_save(state: State, cfg: Config, by: str, values: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(values, dict) or not values:
        raise BadRequest("nothing to save")
    changes: Dict[str, Dict[str, Any]] = {}
    cfg_updates: Dict[str, Any] = {}
    rule_updates: Dict[str, Any] = {}
    current_rules = state.db.one("SELECT * FROM settings WHERE id = 1") or {}
    for key, val in values.items():
        if key == "paused":
            if bool(val) != state.overrides.paused():
                state.overrides.add("PAUSE" if val else "RESUME", by, reason="changed in Settings")
                changes["paused"] = {"before": not bool(val), "after": bool(val)}
            continue
        if key in RULE_KEYS:
            typ, lo, hi = RULE_KEYS[key]
            try:
                v = typ(val)
            except (TypeError, ValueError):
                raise BadRequest(f"{key} must be a number")
            if not lo <= v <= hi:
                raise BadRequest(f"{key} must be between {lo} and {hi}")
            rule_updates[key] = v
            continue
        if key not in EDITABLE:
            raise BadRequest(f"unknown setting {key}")
        v = EDITABLE[key](val) if val is not None else ""
        if key == "mode" and v not in ("auto", "assist", "shadow"):
            raise BadRequest("mode must be auto, assist or shadow")
        if key == "llm_kind":
            if v not in ("mock", "openrouter"):
                raise BadRequest("AI must be 'openrouter' or 'mock'")
            if v == "openrouter" and not cfg.has_openrouter_key:
                raise BadRequest("Connect OpenRouter first (add the API key above).")
        if key == "openrouter_model" and not re.match(r"^[\w.\-/:]{3,100}$", v):
            raise BadRequest("model id looks wrong")
        if key in ("conf_min_act", "conf_auto_remind") and not 0.3 <= v <= 0.99:
            raise BadRequest("confidence must be between 30% and 99%")
        if key == "daily_check_time" and v and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise BadRequest("time must look like 08:00")
        if key == "demo_as_of" and v:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise BadRequest("date must look like 2026-11-04")
        if key == "agent_email_delivery" and v not in ("simulated", "redirect", "resend"):
            raise BadRequest("email delivery must be simulated, redirect or resend")
        if key == "agent_email_redirect_to" and v and not EMAIL_RE.match(v):
            raise BadRequest("test inbox must be an email address")
        if key == "email_from" and v and "@" not in v:
            raise BadRequest("sender must include an email address, e.g. BNH Compliance <agent@yourdomain.com>")
        cfg_updates[key] = v
    merged_min = cfg_updates.get("conf_min_act", cfg.conf_min_act)
    merged_auto = cfg_updates.get("conf_auto_remind", cfg.conf_auto_remind)
    if merged_min > merged_auto:
        raise BadRequest("'act at all' must be lower than or equal to 'act on its own'")
    if cfg_updates.get("agent_email_delivery", cfg.agent_email_delivery) == "redirect" and \
            not cfg_updates.get("agent_email_redirect_to", cfg.agent_email_redirect_to):
        raise BadRequest("Enter the test inbox that should receive all emails.")
    for k, v in cfg_updates.items():
        before = getattr(cfg, k)
        if before != v:
            state.config.set(k, v, by)
            changes[k] = {"before": before, "after": v}
    if rule_updates:
        from agent.store import now_iso
        for k, v in rule_updates.items():
            if current_rules.get(k) != v and float(current_rules.get(k) or 0) != float(v):
                changes[k] = {"before": current_rules.get(k), "after": v}
        sets = ", ".join(f"{k} = %s" for k in rule_updates)
        state.db.x(f"UPDATE settings SET {sets}, updated_at = %s WHERE id = 1", [*rule_updates.values(), now_iso()])
    set_overlay(state.config.all())
    if changes:
        state.audit("settings.changed", target_type="settings", target_id=",".join(changes)[:80],
                    summary="Changed " + ", ".join(changes), changes=changes)
    return {"ok": True, "changed": list(changes)}


SECRETS = {"openrouter": ("OPENROUTER_API_KEY", "sk-or-"), "resend": ("RESEND_API_KEY", "re_")}


def _check_openrouter(key: str) -> str:
    req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "bnh-agentpm/1.0"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("data", {})
    except urllib.error.HTTPError as e:
        raise BadRequest("OpenRouter rejected this key." if e.code in (401, 403) else f"OpenRouter error {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise BadRequest(f"Couldn't reach OpenRouter: {e}")
    used, limit = data.get("usage"), data.get("limit")
    return "Connected" + (f" · ${used:.2f} used" if isinstance(used, (int, float)) else "") + \
           (f" of ${limit:.2f}" if isinstance(limit, (int, float)) else "")


def _check_resend(key: str) -> str:
    req = urllib.request.Request("https://api.resend.com/domains",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "bnh-agentpm/1.0"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("data", [])
        ok = [d["name"] for d in data if d.get("status") == "verified"]
        return "Connected · verified domains: " + (", ".join(ok) if ok else "none yet")
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode(errors="replace")
        if e.code == 401 and "restricted" in body:
            return "Connected (sending-only key)"
        raise BadRequest("Resend rejected this key." if e.code in (401, 403) else f"Resend error {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise BadRequest(f"Couldn't reach Resend: {e}")


def secret_save(state: State, by: str, name: str, value: str) -> Dict[str, Any]:
    if name not in SECRETS:
        raise BadRequest("unknown connection")
    env, prefix = SECRETS[name]
    value = (value or "").strip()
    if not value.startswith(prefix) or len(value) < 20 or any(c.isspace() for c in value):
        raise BadRequest(f"That doesn't look like a {name} key (it should start with {prefix}).")
    detail = _check_openrouter(value) if name == "openrouter" else _check_resend(value)
    write_env_secret(env, value)
    state.audit("settings.secret_replaced", target_type="connection", target_id=name,
                summary=f"{name} key replaced ({_hint(value)})", key_hint=_hint(value), check=detail)
    return {"ok": True, "detail": detail, "hint": _hint(value)}


def test_connection(state: State, cfg: Config, user: Dict[str, Any], what: str) -> Dict[str, Any]:
    if what == "openrouter":
        if not cfg.openrouter_api_key:
            raise BadRequest("No OpenRouter key yet.")
        detail = _check_openrouter(cfg.openrouter_api_key)
    elif what == "resend":
        if not cfg.has_resend:
            raise BadRequest("Add the Resend key and sender address first.")
        msg = Mailer(cfg, state, echo=False).send(
            user["email"], "Test email from the BNH Compliance Assistant",
            f"Hi {user['name'].split()[0]},\n\nThis is a test from the BNH Compliance Assistant. If you can read this, "
            f"email is set up correctly.\n\nSender: {cfg.email_from}\n\n— BNH Compliance Assistant",
            "settings_test", transactional=True)
        if msg["status"] != "sent":
            raise BadRequest(f"Sending failed: {msg['error']}")
        detail = f"Test email sent to {user['email']}"
    elif what == "database":
        detail = state.db.ping()
    else:
        raise BadRequest("unknown test")
    state.audit("settings.connection_tested", target_type="connection", target_id=what, summary=f"{what}: {detail}")
    return {"ok": True, "detail": detail}
