"""Agent console: sign-in, workflow canvas (CoS), PM portal, API and daily scheduler.

Public:   GET /login · GET /auth/link · POST /api/auth/{request,verify,link,logout} · GET /dispute/<fp>
          POST /api/webhook/submission   (X-Webhook-Secret header)
Signed in: GET / (CoS → canvas, PM → portal) · GET /api/me · GET /api/status
CoS only:  runs, review, overrides, outbox, users, portfolios, disputes, reset
PM only:   GET /api/pm/overview · POST /api/pm/disputes"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

from agent.actions import create_dispute, decide_dispute, decide_review
from agent.audit import CATEGORIES, clear_context, set_context
from agent.auth import Auth, AuthError
from agent.config import ROOT, WAT, load_config
from agent.datasource import DbDataSource
from agent.mailer import Mailer
from agent.runner import SERVERLESS, get_state, run_once
from agent.store import now_iso
from agent.trace import Tracer, list_runs, new_run_id, read_events
import webapi

UI = ROOT / "ui"
COOKIE = "bnh_agent_session"
_RUN_LOCK = threading.Lock()  # one run at a time (avoids double-sends from overlapping triggers)
OVERRIDE_TYPES = ("SUPPRESS", "DISMISS", "SEVERITY", "PAUSE", "RESUME")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def start_run(**kw) -> str:
    run_id = new_run_id()
    kw.setdefault("triggered_by", None)
    if SERVERLESS:
        # A serverless function is frozen once it responds, so the check runs inside the request.
        kw["step_delay"] = 0.0
        try:
            run_once(run_id=run_id, echo=False, **kw)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            Tracer(get_state().dir / "runs", run_id).emit("run", "error", error=f"{type(e).__name__}: {e}")
        return run_id

    def target():
        if not _RUN_LOCK.acquire(blocking=False):
            Tracer(get_state().dir / "runs", run_id).emit("run", "error", error="another run is in progress; skipped",
                                                          summary="skipped: concurrent run")
            return
        try:
            run_once(run_id=run_id, echo=True, **kw)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            Tracer(get_state().dir / "runs", run_id).emit("run", "error", error=f"{type(e).__name__}: {e}")
        finally:
            _RUN_LOCK.release()

    threading.Thread(target=target, daemon=True).start()
    return run_id


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "BNH-AgentPM"
    sys_version = ""

    # ── plumbing ──────────────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        if "/api/runs/" not in self.path and "/api/status" not in self.path:
            super().log_message(fmt, *args)

    @property
    def cfg(self):
        # Fresh each request so changes made in Settings apply immediately; keep server-level flags (tests).
        if getattr(self, "_cfg", None) is None:
            self._cfg = load_config()
            base = getattr(self.server, "cfg", None)
            if base is not None:
                self._cfg.trust_proxy = base.trust_proxy
        return self._cfg

    def ctx(self):
        state = get_state(self.cfg)
        mailer = Mailer(self.cfg, state, echo=True)
        return state, mailer, Auth(self.cfg, state, mailer)

    def ip(self) -> str:
        if self.cfg.trust_proxy:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd[:64]
        ip = self.client_address[0]
        return ip.replace("::ffff:", "")[:64]

    def ua(self) -> str:
        return (self.headers.get("User-Agent") or "")[:512]

    def token(self) -> Optional[str]:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[COOKIE].value if COOKIE in c else None

    def _headers(self, code: int, ctype: str, length: int, extra: Optional[Dict[str, str]] = None):
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")  # the app embeds its own workflow page
        self.send_header("Referrer-Policy", "no-referrer")  # sign-in link tokens never leak via Referer
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_html(self, name: str, code: int = 200):
        data = (UI / name).read_bytes()
        self._headers(code, "text/html; charset=utf-8", len(data))
        self.wfile.write(data)

    def redirect(self, to: str):
        self._headers(302, "text/plain", 0, {"Location": to})

    def body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000:
            raise HttpError(413, "request too large")
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "expected a JSON object")
        return data

    def session_cookie(self, token: str) -> Dict[str, str]:
        secure = "; Secure" if self.cfg.app_url.startswith("https://") else ""
        return {"Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; "
                              f"Max-Age={self.cfg.session_max_hours * 3600}{secure}"}

    def clear_cookie(self) -> Dict[str, str]:
        return {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"}

    def check_csrf(self):
        """JSON-only POSTs (a cross-site form cannot send application/json without a CORS preflight, which we never
        grant) plus an Origin check when the browser sends one."""
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(415, "Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            host = urlparse(origin).netloc
            allowed = {self.headers.get("Host"), urlparse(self.cfg.app_url).netloc}
            if host not in allowed:
                raise HttpError(403, "cross-site request blocked")

    def require(self, auth: Auth, role: Optional[str] = None) -> Dict[str, Any]:
        user = auth.session_user(self.token())
        if not user:
            self._audit_denied(auth, 401, "no valid session")
            raise HttpError(401, "sign in required")
        set_context(actor=user["email"], actor_role=user["role"], actor_id=user["id"])
        self._user = user
        if role and user["role"] != role:
            self._audit_denied(auth, 403, f"{user['role']} tried a {role}-only endpoint")
            raise HttpError(403, "not allowed for your role")
        return user

    def _audit_denied(self, auth: Auth, status: int, reason: str) -> None:
        # Unauthenticated status polls from a tab left open after sign-out are not interesting; everything else is.
        if status == 401 and self.command == "GET" and self.path.startswith(("/api/status", "/api/runs/", "/api/me")):
            return
        auth.state.audit("access.denied", outcome="denied", target_type="endpoint",
                         target_id=f"{self.command} {urlparse(self.path).path}"[:80], summary=reason, status=status)

    def _restore_path(self) -> None:
        """On Vercel every request is rewritten to /api/index?__path=/<original>; put the original path back."""
        parts = urlparse(self.path)
        if parts.path.rstrip("/") in ("/api/index", "/api/index.py") and "__path=" in parts.query:
            params = parse_qsl(parts.query, keep_blank_values=True)
            original = next((v for k, v in params if k == "__path"), "/") or "/"
            rest = [(k, v) for k, v in params if k != "__path"]
            self.path = "/" + original.lstrip("/") + (("?" + urlencode(rest)) if rest else "")

    def handle_errors(self, fn):
        self._restore_path()
        clear_context()
        set_context(ip=self.ip(), user_agent=self.ua())
        self._status, self._user, self._err = None, None, None
        try:
            self._wrapped(fn)
        finally:
            if self.command == "POST" and not urlparse(self.path).path.startswith("/api/auth/"):
                try:
                    get_state(self.cfg).audit(
                        "http.request", outcome="success" if (self._status or 500) < 400 else "failure",
                        target_type="endpoint", target_id=f"POST {urlparse(self.path).path}"[:80],
                        summary=f"POST {urlparse(self.path).path} → {self._status}" + (f" ({self._err})" if self._err else ""),
                        status=self._status, origin=self.headers.get("Origin"))
                except Exception:  # noqa: BLE001 - auditing must never take the request down
                    traceback.print_exc()
            clear_context()

    def send_json(self, code: int, body: Any, extra: Optional[Dict[str, str]] = None):
        if code >= 400 and isinstance(body, dict):
            self._err = str(body.get("error", ""))[:200]
        data = json.dumps(body, default=str).encode()
        self._headers(code, "application/json", len(data), extra)
        self.wfile.write(data)

    def _wrapped(self, fn):
        try:
            fn()
        except HttpError as e:
            self.send_json(e.status, {"error": str(e)})
        except AuthError as e:
            self.send_json(e.status, {"error": str(e)})
        except KeyError as e:
            self.send_json(404, {"error": str(e).strip("'")})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})

    # ── GET ───────────────────────────────────────────────────────────────────────────
    def do_GET(self):
        self.handle_errors(self._get)

    def _get(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        state, mailer, auth = self.ctx()

        if path == "/login":
            return self.send_html("login.html")
        if path in ("/plain.js", "/bnh.css"):
            data = (UI / path.lstrip("/")).read_bytes()
            ctype = "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            self._headers(200, ctype, len(data))
            return self.wfile.write(data)
        if path == "/auth/link":
            return self.send_html("link.html")
        m = re.match(r"^/dispute/([0-9a-f]{6,20})$", path)
        if m:
            if not auth.session_user(self.token()):
                return self.redirect(f"/login?next={quote('/#dispute=' + m.group(1))}")
            return self.redirect(f"/#dispute={m.group(1)}")
        if path in ("/", "/index.html"):
            user = auth.session_user(self.token())
            if not user:
                return self.redirect("/login")
            return self.send_html("app.html" if user["role"] == "COS" else "pm.html")
        if path == "/workflow":
            user = auth.session_user(self.token())
            if not user:
                return self.redirect("/login?next=/workflow")
            if user["role"] != "COS":
                return self.redirect("/")
            return self.send_html("workflow.html")
        if path == "/audit":
            user = auth.session_user(self.token())
            if not user:
                return self.redirect("/login?next=/audit")
            set_context(actor=user["email"], actor_role=user["role"], actor_id=user["id"])
            if user["role"] != "COS":
                self._audit_denied(auth, 403, "PM tried to open the audit log")
                return self.redirect("/")
            return self.send_html("audit.html")
        if path == "/api/cron/daily":
            # Vercel Cron (vercel.json) calls this once a day with "Authorization: Bearer $CRON_SECRET".
            secret = os.environ.get("CRON_SECRET", "")
            if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {secret}"):
                set_context(actor="cron", actor_role="SYSTEM")
                state.audit("webhook.rejected", outcome="denied", summary="daily check called without the cron secret")
                raise HttpError(401, "unauthorised")
            if not self.cfg.daily_check_time:
                return self.send_json(200, {"skipped": "daily check is switched off in Settings"})
            set_context(actor="scheduler", actor_role="SYSTEM")
            rid = start_run(trigger="schedule", triggered_by="vercel-cron")
            return self.send_json(200, {"run_id": rid})
        if path == "/favicon.ico":
            return self._headers(204, "image/x-icon", 0)

        user = self.require(auth)
        if path == "/api/me":
            return self.send_json(200, {k: user[k] for k in ("id", "name", "email", "role", "last_login_at", "last_login_ip")})
        if path == "/api/status":
            ov = state.overrides.view() if user["role"] == "COS" else None
            return self.send_json(200, {
                "has_openrouter_key": self.cfg.has_openrouter_key, "model": self.cfg.openrouter_model,
                "mode": self.cfg.mode, "paused": ov.paused() if ov else None, "now": datetime.now(WAT).isoformat(),
                "running": _RUN_LOCK.locked(), "db": state.db.describe(), "resend": self.cfg.has_resend,
                "agent_email": self.cfg.agent_email_delivery if self.cfg.has_resend else "simulated",
                "redirect_to": self.cfg.agent_email_redirect_to if self.cfg.agent_email_delivery == "redirect" else None})

        if path == "/api/pm/overview":
            pids = [r["id"] for r in state.db.q("SELECT id FROM portfolios WHERE pm_user_id = %s", [user["id"]])]
            portfolios = state.db.q("SELECT id, name FROM portfolios WHERE pm_user_id = %s ORDER BY id", [user["id"]])
            subs = state.db.q(f"SELECT id, portfolio_id, reporting_month, rag_status, cash_position_ngn, execution_blocker, "
                              f"submitted_at FROM submissions WHERE portfolio_id IN ({', '.join(['%s'] * len(pids)) or 'NULL'}) "
                              f"ORDER BY reporting_month DESC", pids) if pids else []
            return self.send_json(200, {"portfolios": portfolios, "submissions": subs,
                                        "findings": state.findings.for_portfolios(pids),
                                        "disputes": state.disputes.list(pids),
                                        "emails": [{k: e[k] for k in ("ts", "subject", "body", "kind", "status")}
                                                   for e in state.emails.list(50, to=user["email"])
                                                   if e["kind"] not in ("auth_code",)],
                                        "signins": state.db.q("SELECT ts, event, ip, user_agent FROM auth_events "
                                                              "WHERE user_id = %s ORDER BY ts DESC LIMIT 10", [user["id"]])})

        self.require(auth, "COS")
        if path == "/api/runs":
            return self.send_json(200, list_runs(state.dir / "runs")[:50])
        m = re.match(r"^/api/runs/([\w-]+)$", path)
        if m:
            return self.send_json(200, read_events(state.dir / "runs", m.group(1), int(qs.get("since", ["0"])[0])))
        if path == "/api/review":
            return self.send_json(200, list(reversed(state.review.all())))
        if path == "/api/overrides":
            return self.send_json(200, list(reversed(state.overrides.all())))
        if path == "/api/outbox":
            return self.send_json(200, [e for e in state.emails.list(200) if e["kind"] != "auth_code"])
        if path == "/api/users":
            users = state.db.q("SELECT id, name, email, role, status, last_login_at, last_login_ip, created_at "
                               "FROM users ORDER BY role, name")
            ports = state.db.q("SELECT p.id, p.name, p.pm_user_id, u.name AS pm_name, u.email AS pm_email "
                               "FROM portfolios p LEFT JOIN users u ON u.id = p.pm_user_id ORDER BY p.id")
            events = state.db.q("SELECT ts, event, email, ip, user_agent FROM auth_events ORDER BY ts DESC LIMIT 40")
            return self.send_json(200, {"users": users, "portfolios": ports, "auth_events": events})
        if path == "/api/disputes":
            return self.send_json(200, state.disputes.list())
        if path == "/api/overview":
            return self.send_json(200, webapi.overview(state, self.cfg))
        if path == "/api/activity":
            return self.send_json(200, webapi.activity(state, int(qs.get("limit", ["60"])[0]),
                                                       qs.get("portfolio", [None])[0]))
        m = re.match(r"^/api/portfolios/([\w-]+)/detail$", path)
        if m:
            return self.send_json(200, webapi.portfolio_detail(state, m.group(1)))
        if path == "/api/settings":
            return self.send_json(200, webapi.settings_get(state, self.cfg))
        if path in ("/api/audit", "/api/audit/export"):
            f = {k: (qs.get(k, [""])[0] or None) for k in ("category", "q", "outcome", "actor", "since", "until",
                                                             "run_id", "target_id", "before_seq")}
            if f["category"] and f["category"] not in CATEGORIES:
                raise HttpError(400, "unknown category")
            if path == "/api/audit/export":
                csv_text = state.trail.export_csv(**{k: v for k, v in f.items() if k != "before_seq"})
                state.audit("audit.exported", summary=f"CSV export ({csv_text.count(chr(10)) - 1} rows)",
                            filters={k: v for k, v in f.items() if v})
                data = csv_text.encode("utf-8-sig")
                self._headers(200, "text/csv; charset=utf-8", len(data),
                              {"Content-Disposition": f'attachment; filename="bnh-audit-{datetime.now(WAT):%Y%m%d-%H%M}.csv"'})
                return self.wfile.write(data)
            rows = state.trail.list(limit=int(qs.get("limit", ["100"])[0]), **f)
            if not f["before_seq"]:  # first page of a search = one "viewed" event, not one per scroll
                state.audit("audit.viewed", summary="audit log opened" + (" (filtered)" if any(f.values()) else ""),
                            filters={k: v for k, v in f.items() if v})
            return self.send_json(200, {"rows": rows, "stats": state.trail.stats() if not f["before_seq"] else None})
        if path == "/api/audit/verify":
            res = state.trail.verify()
            state.audit("audit.verified", outcome="success" if res["ok"] else "failure",
                        summary=f"chain {'intact' if res['ok'] else 'BROKEN at seq ' + str(res['broken_at'])} "
                                f"({res['checked']} entries checked)", **{k: v for k, v in res.items() if k != "head"})
            return self.send_json(200, res)
        raise HttpError(404, "not found")

    # ── POST ──────────────────────────────────────────────────────────────────────────
    def do_POST(self):
        self.handle_errors(self._post)

    def _post(self):
        path = urlparse(self.path).path
        state, mailer, auth = self.ctx()

        if path == "/api/webhook/submission":
            secret = self.headers.get("X-Webhook-Secret") or ""
            set_context(actor="webhook", actor_role="SYSTEM")
            if not self.cfg.webhook_secret or not hmac.compare_digest(secret, self.cfg.webhook_secret):
                state.audit("webhook.rejected", outcome="denied", summary="bad or missing X-Webhook-Secret")
                raise HttpError(401, "bad webhook secret")
            b = self.body()
            rid = start_run(trigger="webhook", portfolio_ids=[str(b["portfolio_id"])], llm_kind=b.get("llm") or None,
                            as_of=b.get("as_of"), step_delay=0.35, triggered_by=f"webhook from {self.ip()}")
            state.audit("webhook.received", target_type="run", target_id=rid,
                        summary=f"submission event for {b['portfolio_id']} → run started", portfolio_id=b["portfolio_id"])
            return self.send_json(202, {"run_id": rid})

        self.check_csrf()
        b = self.body()
        if path == "/api/auth/request":
            auth.request_code(str(b.get("email", "")), self.ip(), self.ua())
            return self.send_json(200, {"ok": True, "ip": self.ip(),
                                        "message": "If that email has an account, a sign-in code is on its way."})
        if path == "/api/auth/verify":
            token, user = auth.verify_code(str(b.get("email", "")), str(b.get("code", "")), self.ip(), self.ua())
            return self.send_json(200, {"ok": True, "role": user["role"]}, self.session_cookie(token))
        if path == "/api/auth/link":
            token, user = auth.verify_link(str(b.get("token", "")), self.ip(), self.ua())
            return self.send_json(200, {"ok": True, "role": user["role"]}, self.session_cookie(token))
        if path == "/api/auth/logout":
            auth.logout(self.token(), self.ip())
            return self.send_json(200, {"ok": True}, self.clear_cookie())

        user = self.require(auth)
        if path == "/api/pm/disputes":
            if user["role"] != "PM":
                raise HttpError(403, "only PMs raise disputes; the CoS can dismiss directly")
            pids = [r["id"] for r in state.db.q("SELECT id FROM portfolios WHERE pm_user_id = %s", [user["id"]])]
            return self.send_json(201, create_dispute(state, mailer, user, str(b.get("fingerprint", "")),
                                                      str(b.get("message", "")), pids))

        self.require(auth, "COS")
        by = user["email"]
        if path == "/api/runs":
            rid = start_run(mode=b.get("mode") or None, llm_kind=b.get("llm") or None, as_of=b.get("as_of") or None,
                            trigger="manual", portfolio_ids=b.get("portfolio_ids") or None,
                            step_delay=float(b.get("delay", 0.35)), triggered_by=by)
            state.audit("run.requested", target_type="run", target_id=rid, summary=f"manual run requested by {by}",
                        mode=b.get("mode"), llm=b.get("llm", "mock"), as_of=b.get("as_of"))
            return self.send_json(202, {"run_id": rid})
        if path == "/api/webhook/test":  # lets the CoS fire the webhook trigger from the canvas
            rid = start_run(trigger="webhook", portfolio_ids=[str(b["portfolio_id"])], llm_kind=b.get("llm") or None,
                            as_of=b.get("as_of") or None, step_delay=0.35, triggered_by=f"{by} (test webhook)")
            state.audit("run.requested", target_type="run", target_id=rid,
                        summary=f"test webhook for {b['portfolio_id']} fired by {by}")
            return self.send_json(202, {"run_id": rid})
        m = re.match(r"^/api/review/([\w-]+)$", path)
        if m:
            snap = DbDataSource(state.db).load()
            res = decide_review(m.group(1), str(b["decision"]), by, str(b.get("note", "")), state, snap, mailer,
                                dismiss_finding=bool(b.get("dismiss")))
            item = res["item"]
            tr = Tracer(state.dir / "runs", item["run_id"])
            tr.flow("review_queue", "human", label=item["id"])
            tr.emit("human", "end", summary=f"{item['id']} {item['status']} by {by}", item=item)
            if res["email"]:
                tr.flow("human", "send_email", label="approved")
                tr.emit("send_email", "end", summary=f"approved email {res['email']['status']}", emails=[res["email"]])
            else:
                tr.flow("human", "load_overrides", label="precedent")
            return self.send_json(200, res)
        m = re.match(r"^/api/disputes/([\w-]+)$", path)
        if m:
            return self.send_json(200, decide_dispute(state, mailer, m.group(1), str(b.get("decision")), by,
                                                      str(b.get("note", ""))))
        if path == "/api/overrides":
            t = b.pop("type", None)
            if t not in OVERRIDE_TYPES:
                raise HttpError(400, "bad override type")
            if t in ("SUPPRESS", "DISMISS", "SEVERITY") and not str(b.get("reason", "")).strip():
                raise HttpError(400, "a reason is required")
            b.pop("by", None)
            return self.send_json(201, state.overrides.add(t, by, **b))
        if path == "/api/users":
            name, email, role = str(b.get("name", "")).strip(), str(b.get("email", "")).strip().lower(), b.get("role")
            if not (2 <= len(name) <= 120) or not EMAIL_RE.match(email) or role not in ("COS", "PM"):
                raise HttpError(400, "name, a valid email and role (COS or PM) are required")
            if state.db.one("SELECT id FROM users WHERE email = %s", [email]):
                raise HttpError(409, "a user with that email already exists")
            row = state.db.insert("users", {"id": str(uuid.uuid4()), "name": name, "email": email, "role": role,
                                            "status": "ACTIVE", "created_at": now_iso()})
            state.audit("user.created", target_type="user", target_id=row["id"], summary=f"{role} account created for {email}",
                        email=email, name=name, role=role)
            return self.send_json(201, row)
        m = re.match(r"^/api/users/([\w-]+)$", path)
        if m:
            target = state.db.one("SELECT * FROM users WHERE id = %s", [m.group(1)])
            if not target:
                raise HttpError(404, "user not found")
            status = b.get("status")
            if status not in ("ACTIVE", "DEACTIVATED"):
                raise HttpError(400, "status must be ACTIVE or DEACTIVATED")
            if target["id"] == user["id"] and status == "DEACTIVATED":
                raise HttpError(400, "you can't deactivate your own account")
            state.db.x("UPDATE users SET status = %s WHERE id = %s", [status, target["id"]])
            if status == "DEACTIVATED":
                auth.revoke_user_sessions(target["id"])
            state.audit("user.status_changed", target_type="user", target_id=target["id"],
                        summary=f"{target['email']}: {target['status']} → {status}" + (" (sessions revoked)" if status == "DEACTIVATED" else ""),
                        email=target["email"], before=target["status"], after=status)
            return self.send_json(200, {"ok": True})
        m = re.match(r"^/api/portfolios/([\w-]+)$", path)
        if m:
            pm_id = b.get("pm_user_id") or None
            before = state.db.one("SELECT p.pm_user_id, u.email FROM portfolios p LEFT JOIN users u ON u.id = p.pm_user_id "
                                  "WHERE p.id = %s", [m.group(1)])
            after_email = (state.db.one("SELECT email FROM users WHERE id = %s", [pm_id]) or {}).get("email") if pm_id else None
            if pm_id and not state.db.one("SELECT id FROM users WHERE id = %s AND role = 'PM'", [pm_id]):
                raise HttpError(400, "pm_user_id must be a PM account")
            if not state.db.x("UPDATE portfolios SET pm_user_id = %s WHERE id = %s", [pm_id, m.group(1)]):
                raise HttpError(404, "portfolio not found")
            state.audit("portfolio.pm_assigned", target_type="portfolio", target_id=m.group(1),
                        summary=f"{m.group(1)}: PM {(before or {}).get('email') or 'none'} → {after_email or 'none'}",
                        before=(before or {}).get("email"), after=after_email)
            return self.send_json(200, {"ok": True})
        if path == "/api/settings":
            try:
                return self.send_json(200, webapi.settings_save(state, self.cfg, by, b.get("values") or {}))
            except webapi.BadRequest as e:
                raise HttpError(400, str(e))
        if path == "/api/settings/secret":
            try:
                return self.send_json(200, webapi.secret_save(state, by, str(b.get("name", "")), str(b.get("value", ""))))
            except webapi.BadRequest as e:
                raise HttpError(400, str(e))
        if path == "/api/settings/test":
            try:
                return self.send_json(200, webapi.test_connection(state, self.cfg, user, str(b.get("what", ""))))
            except webapi.BadRequest as e:
                raise HttpError(400, str(e))
        if path == "/api/reset":
            state.reset()
            return self.send_json(200, {"ok": True})
        raise HttpError(404, "not found")


def scheduler() -> None:
    """Daily check at the time chosen in Settings (re-read every 20 s, so changes apply without a restart)."""
    last = None
    while True:
        try:
            cfg = load_config()
            now = datetime.now(WAT)
            if cfg.daily_check_time and now.strftime("%H:%M") == cfg.daily_check_time and last != now.date():
                last = now.date()
                print(f"⏰ daily check {now:%Y-%m-%d %H:%M} WAT", flush=True)
                start_run(trigger="schedule", step_delay=0.2, triggered_by="scheduler")
        except Exception:  # noqa: BLE001 - the scheduler must keep going
            traceback.print_exc()
        time.sleep(20)


def serve(port: int = 8765, host: str = "127.0.0.1") -> None:
    cfg = load_config()
    state = get_state(cfg)
    cfg = load_config()  # now includes values saved from Settings
    threading.Thread(target=scheduler, daemon=True).start()
    daily_at = cfg.daily_check_time or "off"
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    n_cos = state.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'COS' AND status = 'ACTIVE'")["n"]
    state.audit("system.server_started", actor="system", summary=f"console started on {host}:{port}",
                db=state.db.describe(), email="resend" if cfg.has_resend else "console",
                agent_email=cfg.agent_email_delivery, scheduler=daily_at, model=cfg.openrouter_model)
    print(f"BNH PM Compliance Agent on http://{host}:{port}  ·  {state.db.describe()}  ·  "
          f"email: {'Resend' if cfg.has_resend else 'console only (no RESEND_API_KEY)'}  ·  CoS accounts: {n_cos}",
          flush=True)
    if not n_cos:
        print("  ⚠ No CoS account yet. Set SEED_COS_EMAIL in .env and run:  agentpm.py db seed", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
