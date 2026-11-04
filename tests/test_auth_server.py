"""Sign-in, roles and disputes, exercised over real HTTP against a server on a random port (SQLite, no real email)."""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("AGENTICPM_STATE_DIR", tempfile.mkdtemp(prefix="agenticpm-auth-"))
os.environ["AGENTICPM_NO_SECRET_WRITE"] = "1"
os.environ["DATABASE_URL"] = ""
os.environ["RESEND_API_KEY"] = ""
os.environ["AUTH_CODE_DELIVERY"] = "email"  # tests read codes from captured emails
os.environ["AGENT_EMAIL_DELIVERY"] = "simulated"
os.environ["AGENT_LLM"] = "mock"           # never call a real AI from tests
os.environ["DAILY_CHECK_TIME"] = ""
os.environ["SEED_COS_EMAIL"] = "cos@bnh.example"
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "hook-secret")

from http.server import ThreadingHTTPServer  # noqa: E402

import agent.mailer as mailer_mod  # noqa: E402
import server as srv  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.datasource import seed_demo  # noqa: E402
from agent.runner import get_state, run_once  # noqa: E402

SENT = []
_orig_send = mailer_mod.Mailer.send


def _capture(self, to, subject, body, kind, *a, **k):
    SENT.append({"to": to, "subject": subject, "body": body, "kind": kind})
    k["log_body"] = k.get("log_body")
    self.echo = False
    return _orig_send(self, to, subject, body, kind, *a, **k)


mailer_mod.Mailer.send = _capture


class Client:
    def __init__(self, base, ip=None):
        self.base, self.cookie, self.ip = base, None, ip

    def call(self, method, path, body=None, ctype="application/json", headers=None):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if method == "POST" and ctype:
            req.add_header("Content-Type", ctype)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        if self.ip:
            req.add_header("X-Forwarded-For", self.ip)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req, timeout=10)
            status, hdrs, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, raw = e.code, e.headers, e.read()
        sc = hdrs.get("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if "Max-Age=0" not in sc else None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return status, payload, hdrs

    def login(self, email):
        SENT.clear()
        self.call("POST", "/api/auth/request", {"email": email})
        code = re.search(r"\n\s+(\d{6})\n", SENT[-1]["body"]).group(1)
        st, body, _ = self.call("POST", "/api/auth/verify", {"email": email, "code": code})
        assert st == 200, body
        return self


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = load_config()
        cfg.trust_proxy = True  # lets tests simulate different client IPs via X-Forwarded-For
        st = get_state(cfg)
        if not st.db.one("SELECT id FROM portfolios LIMIT 1"):
            seed_demo(st.db, cfg, cfg.data_dir)
        st.reset()
        run_once(as_of="2026-11-04", echo=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.httpd.cfg = cfg
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.state = st

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        for t in ("auth_events", "login_codes", "auth_sessions"):
            self.state.db.x(f"DELETE FROM {t}")
        SENT.clear()

    # ── access control ────────────────────────────────────────────────────────────────
    def test_anonymous_is_redirected_and_api_is_401(self):
        c = Client(self.base)
        st, _, h = c.call("GET", "/")
        self.assertEqual((st, h.get("Location")), (302, "/login"))
        self.assertEqual(c.call("GET", "/api/runs")[0], 401)
        self.assertEqual(c.call("GET", "/api/pm/overview")[0], 401)

    def test_security_headers(self):
        _, _, h = Client(self.base).call("GET", "/login")
        self.assertEqual(h.get("X-Frame-Options"), "SAMEORIGIN")  # only our own pages may embed us
        self.assertEqual(h.get("Referrer-Policy"), "no-referrer")
        self.assertIn("frame-ancestors 'self'", h.get("Content-Security-Policy"))

    # ── sign-in ───────────────────────────────────────────────────────────────────────
    def test_code_email_shows_requesting_ip_and_device(self):
        c = Client(self.base, ip="41.58.12.7")
        st, body, _ = c.call("POST", "/api/auth/request", {"email": "COS@bnh.example "},
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit Chrome/131 Safari/537"})
        self.assertEqual((st, body["ip"]), (200, "41.58.12.7"))
        mail = SENT[-1]["body"]
        self.assertIn("41.58.12.7", mail)
        self.assertIn("Chrome on macOS", mail)
        self.assertRegex(mail, r"\n\s+\d{6}\n")
        logged = self.state.db.one("SELECT body FROM email_log WHERE kind = 'auth_code' ORDER BY ts DESC LIMIT 1")
        self.assertNotRegex(logged["body"], r"\d{6}")  # code never stored in the email log

    def test_unknown_email_gets_same_response_and_no_email(self):
        st, body, _ = Client(self.base).call("POST", "/api/auth/request", {"email": "nobody@evil.example"})
        self.assertEqual(st, 200)
        self.assertIn("If that email has an account", body["message"])
        self.assertEqual(SENT, [])

    def test_cos_login_logout(self):
        c = Client(self.base).login("cos@bnh.example")
        st, me, _ = c.call("GET", "/api/me")
        self.assertEqual((st, me["role"]), (200, "COS"))
        self.assertEqual(c.call("GET", "/api/runs")[0], 200)
        c2 = Client(self.base); c2.cookie = c.cookie
        c.call("POST", "/api/auth/logout")
        self.assertEqual(c2.call("GET", "/api/me")[0], 401)  # server-side revocation, not just cookie clearing

    def test_code_is_single_use_and_wrong_codes_burn_it(self):
        c = Client(self.base)
        c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})
        code = re.search(r"\n\s+(\d{6})\n", SENT[-1]["body"]).group(1)
        wrong = f"{(int(code) + 1) % 1_000_000:06d}"
        for _ in range(5):
            self.assertEqual(c.call("POST", "/api/auth/verify", {"email": "cos@bnh.example", "code": wrong})[0], 401)
        self.assertEqual(c.call("POST", "/api/auth/verify", {"email": "cos@bnh.example", "code": code})[0], 401)

    def test_new_code_invalidates_old_one(self):
        c = Client(self.base)
        c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})
        first = re.search(r"\n\s+(\d{6})\n", SENT[-1]["body"]).group(1)
        c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})
        second = re.search(r"\n\s+(\d{6})\n", SENT[-1]["body"]).group(1)
        if first != second:
            self.assertEqual(c.call("POST", "/api/auth/verify", {"email": "cos@bnh.example", "code": first})[0], 401)
        self.assertEqual(c.call("POST", "/api/auth/verify", {"email": "cos@bnh.example", "code": second})[0], 200)

    def test_rate_limit_per_email(self):
        c = Client(self.base)
        codes = [c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})[0] for _ in range(6)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5], 429)

    def test_magic_link_single_use_and_ip_alert(self):
        c = Client(self.base, ip="41.58.12.7")
        c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})
        token = re.search(r"/auth/link\?token=([\w-]+)", SENT[-1]["body"]).group(1)
        phone = Client(self.base, ip="102.89.3.44")
        self.assertEqual(phone.call("POST", "/api/auth/link", {"token": token})[0], 200)
        self.assertEqual(phone.call("GET", "/api/me")[0], 200)
        alert = [m for m in SENT if m["kind"] == "auth_alert"]
        self.assertTrue(alert and "102.89.3.44" in alert[-1]["body"] and "41.58.12.7" in alert[-1]["body"])
        self.assertEqual(Client(self.base).call("POST", "/api/auth/link", {"token": token})[0], 401)

    def test_csrf_protections(self):
        c = Client(self.base).login("cos@bnh.example")
        self.assertEqual(c.call("POST", "/api/runs", {"llm": "mock"}, ctype="text/plain")[0], 415)
        self.assertEqual(c.call("POST", "/api/overrides", {"type": "PAUSE"},
                                headers={"Origin": "https://evil.example"})[0], 403)

    def test_deactivated_user_is_signed_out(self):
        pm = Client(self.base).login("amina.yusuf@bnh.example")
        uid = self.state.db.one("SELECT id FROM users WHERE email = 'amina.yusuf@bnh.example'")["id"]
        cos = Client(self.base).login("cos@bnh.example")
        self.assertEqual(cos.call("POST", f"/api/users/{uid}", {"status": "DEACTIVATED"})[0], 200)
        self.assertEqual(pm.call("GET", "/api/me")[0], 401)
        cos.call("POST", f"/api/users/{uid}", {"status": "ACTIVE"})

    def test_webhook_requires_secret(self):
        c = Client(self.base)
        self.assertEqual(c.call("POST", "/api/webhook/submission", {"portfolio_id": "P01"})[0], 401)
        st, _, _ = c.call("POST", "/api/webhook/submission", {"portfolio_id": "P01", "as_of": "2026-11-04"},
                          headers={"X-Webhook-Secret": os.environ["WEBHOOK_SECRET"]})
        self.assertEqual(st, 202)

    # ── PM role ───────────────────────────────────────────────────────────────────────
    def test_pm_sees_only_own_portfolio_and_cannot_use_cos_api(self):
        pm = Client(self.base).login("adaeze.okafor@bnh.example")
        self.assertEqual(pm.call("GET", "/api/runs")[0], 403)
        self.assertEqual(pm.call("POST", "/api/overrides", {"type": "PAUSE"})[0], 403)
        st, ov, _ = pm.call("GET", "/api/pm/overview")
        self.assertEqual(st, 200)
        self.assertEqual({p["id"] for p in ov["portfolios"]}, {"P01"})
        self.assertTrue(all(f["portfolio_id"] == "P01" for f in ov["findings"]))
        other = self.state.db.one("SELECT fingerprint FROM findings WHERE portfolio_id = 'P03'")["fingerprint"]
        st, _, _ = pm.call("POST", "/api/pm/disputes", {"fingerprint": other, "message": "not my portfolio but trying"})
        self.assertEqual(st, 404)

    def test_dispute_round_trip(self):
        pm = Client(self.base).login("adaeze.okafor@bnh.example")
        fp = self.state.db.one("SELECT fingerprint FROM findings WHERE portfolio_id = 'P01'")["fingerprint"]
        st, d, _ = pm.call("POST", "/api/pm/disputes",
                           {"fingerprint": fp, "message": "The drop is the scheduled Q3 LP distribution paid on 30 Oct."})
        self.assertEqual(st, 201, d)
        self.assertTrue(any(m["kind"] == "dispute_opened" for m in SENT))
        cos = Client(self.base).login("cos@bnh.example")
        self.assertEqual(cos.call("POST", f"/api/disputes/{d['id']}", {"decision": "deny", "note": ""})[0], 400)
        st, res, _ = cos.call("POST", f"/api/disputes/{d['id']}", {"decision": "uphold", "note": "Confirmed with finance"})
        self.assertEqual((st, res["status"]), (200, "UPHELD"))
        self.assertTrue(any(o["type"] == "DISMISS" and o.get("fingerprint") == fp for o in self.state.overrides.all()))
        out = run_once(as_of="2026-11-04", echo=False, portfolio_ids=["P01"])
        self.assertEqual(out["decisions"], {"suppressed": 1})


if __name__ == "__main__":
    unittest.main()


class ScreenCodeTests(unittest.TestCase):
    """AUTH_CODE_DELIVERY=screen: the code comes back to the page and no email is sent."""
    setUpClass = ServerTests.__dict__["setUpClass"]
    tearDownClass = ServerTests.__dict__["tearDownClass"]

    def setUp(self):
        ServerTests.setUp(self)
        os.environ["AUTH_CODE_DELIVERY"] = "screen"
        self.addCleanup(os.environ.__setitem__, "AUTH_CODE_DELIVERY", "email")

    def test_code_shown_not_emailed(self):
        c = Client(self.base)
        st, body, _ = c.call("GET", "/api/auth/config")
        self.assertEqual(body["code_delivery"], "screen")
        SENT.clear()
        st, body, _ = c.call("POST", "/api/auth/request", {"email": "cos@bnh.example"})
        self.assertEqual(st, 200)
        self.assertEqual(body["delivery"], "screen")
        self.assertRegex(body["code"], r"^\d{6}$")
        self.assertEqual(SENT, [])
        st, body, _ = c.call("POST", "/api/auth/verify", {"email": "cos@bnh.example", "code": body["code"]})
        self.assertEqual((st, body["role"]), (200, "COS"))
        self.assertEqual(c.call("GET", "/api/me")[0], 200)
        self.assertEqual(SENT, [])  # no sign-in alert either

    def test_unknown_email_rejected(self):
        st, body, _ = Client(self.base).call("POST", "/api/auth/request", {"email": "nobody@bnh.example"})
        self.assertEqual(st, 404)
        self.assertNotIn("code", body)


class SettingsTests(ServerTests):
    """Plug-and-play settings, overview and activity endpoints."""
    def test_overview_and_activity_are_plain_and_cos_only(self):
        cos = Client(self.base).login("cos@bnh.example")
        st, ov, _ = cos.call("GET", "/api/overview")
        self.assertEqual(st, 200)
        self.assertEqual(ov["kpis"]["total"], 8)
        self.assertTrue({p["status"] for p in ov["portfolios"]} <= {"on_track", "reminded", "needs_you", "overdue", "exception"})
        self.assertEqual(cos.call("GET", "/api/activity")[0], 200)
        pm = Client(self.base).login("adaeze.okafor@bnh.example")
        self.assertEqual(pm.call("GET", "/api/overview")[0], 403)
        self.assertEqual(pm.call("GET", "/api/settings")[0], 403)
        self.assertEqual(pm.call("POST", "/api/settings", {"values": {"mode": "shadow"}})[0], 403)

    def test_settings_save_validates_applies_and_audits(self):
        cos = Client(self.base).login("cos@bnh.example")
        self.assertEqual(cos.call("POST", "/api/settings", {"values": {"mode": "yolo"}})[0], 400)
        self.assertEqual(cos.call("POST", "/api/settings", {"values": {"deadline_day": 40}})[0], 400)
        self.assertEqual(cos.call("POST", "/api/settings", {"values": {"conf_min_act": 0.9, "conf_auto_remind": 0.7}})[0], 400)
        self.assertEqual(cos.call("POST", "/api/settings", {"values": {"agent_email_delivery": "redirect", "agent_email_redirect_to": ""}})[0], 400)
        st, r, _ = cos.call("POST", "/api/settings", {"values": {"mode": "shadow", "deadline_day": 12, "daily_check_time": "07:30"}})
        self.assertEqual((st, sorted(r["changed"])), (200, ["daily_check_time", "deadline_day", "mode"]))
        _, s, _ = cos.call("GET", "/api/settings")
        self.assertEqual((s["assistant"]["mode"], s["rules"]["deadline_day"], s["assistant"]["daily_check_time"]), ("shadow", 12, "07:30"))
        out = run_once(as_of="2026-11-04", echo=False, portfolio_ids=["P06"])
        self.assertNotIn("execute", out["decisions"])  # shadow mode from Settings applies to the next run
        audit = self.state.trail.list(limit=5, category="settings")
        self.assertEqual(audit[0]["data"]["changes"]["mode"]["after"], "shadow")
        cos.call("POST", "/api/settings", {"values": {"mode": "auto", "deadline_day": 10, "daily_check_time": ""}})

    def test_pause_from_settings(self):
        cos = Client(self.base).login("cos@bnh.example")
        cos.call("POST", "/api/settings", {"values": {"paused": True}})
        self.assertTrue(self.state.overrides.paused())
        cos.call("POST", "/api/settings", {"values": {"paused": False}})
        self.assertFalse(self.state.overrides.paused())

    def test_secret_endpoint_rejects_bad_keys_and_never_echoes(self):
        cos = Client(self.base).login("cos@bnh.example")
        st, body, _ = cos.call("POST", "/api/settings/secret", {"name": "openrouter", "value": "not-a-key"})
        self.assertEqual(st, 400)
        _, s, _ = cos.call("GET", "/api/settings")
        self.assertNotIn("sk-or-", json.dumps(s)); self.assertNotIn("re_", json.dumps(s["connections"]))

    def test_pages_route_by_role(self):
        cos = Client(self.base).login("cos@bnh.example")
        st, html, _ = cos.call("GET", "/")
        self.assertIn("Needs your decision", html)
        self.assertEqual(cos.call("GET", "/workflow")[0], 200)
        pm = Client(self.base).login("adaeze.okafor@bnh.example")
        st, html, _ = pm.call("GET", "/")
        self.assertIn("My compliance", html)
        self.assertEqual(pm.call("GET", "/workflow")[0], 302)
