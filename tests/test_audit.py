"""Audit trail: coverage, hash chain, tamper detection, append-only guard, redaction."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AGENTICPM_STATE_DIR", tempfile.mkdtemp(prefix="agenticpm-audit-"))
os.environ["AGENTICPM_NO_SECRET_WRITE"] = "1"
os.environ["DATABASE_URL"] = ""
os.environ["RESEND_API_KEY"] = ""
os.environ["AGENT_EMAIL_DELIVERY"] = "simulated"
os.environ["AGENT_LLM"] = "mock"           # never call a real AI from tests
os.environ["DAILY_CHECK_TIME"] = ""
os.environ["SEED_COS_EMAIL"] = "cos@bnh.example"

from agent.audit import AuditTrail, audit_context, install_guards  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.datasource import seed_demo  # noqa: E402
from agent.db import Database, DbError  # noqa: E402
from agent.runner import get_state, run_once  # noqa: E402
from agent.store import State  # noqa: E402


def scratch_db():
    d = Database(None, Path(tempfile.mkdtemp()) / "audit.db")
    d.init_schema()
    install_guards(d)
    return d


class ChainTests(unittest.TestCase):
    def test_chain_verifies_and_links(self):
        db = scratch_db(); t = AuditTrail(db)
        for i in range(25):
            t.write("system.test", summary=f"entry {i}", n=i)
        res = t.verify()
        self.assertTrue(res["ok"]); self.assertEqual(res["checked"], 25)
        rows = list(reversed(t.list(limit=25)))
        self.assertEqual([r["seq"] for r in rows], list(range(1, 26)))
        self.assertTrue(all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, 25)))

    def test_update_and_delete_are_refused(self):
        db = scratch_db(); t = AuditTrail(db)
        t.write("system.test", summary="x")
        with self.assertRaises(DbError):
            db.x("UPDATE audit_trail SET summary = 'edited'")
        with self.assertRaises(DbError):
            db.x("DELETE FROM audit_trail")

    def test_tampering_is_detected_even_if_guards_are_bypassed(self):
        db = scratch_db(); t = AuditTrail(db)
        for i in range(5):
            t.write("system.test", summary=f"entry {i}", actor=f"user{i}@x")
        db.x("DROP TRIGGER audit_trail_no_update"); db.x("DROP TRIGGER audit_trail_no_delete")
        db.x("UPDATE audit_trail SET actor = 'someone-else@x' WHERE seq = 3")
        res = t.verify()
        self.assertFalse(res["ok"]); self.assertEqual(res["broken_at"], 3)
        db.x("UPDATE audit_trail SET actor = 'user2@x' WHERE seq = 3")
        self.assertTrue(t.verify()["ok"])
        db.x("DELETE FROM audit_trail WHERE seq = 4")
        res = t.verify()
        self.assertFalse(res["ok"]); self.assertIn("gap", res["reason"])

    def test_context_fills_actor_and_ip(self):
        db = scratch_db(); t = AuditTrail(db)
        with audit_context(actor="cos@x", actor_role="COS", ip="41.58.1.1", user_agent="UA/1"):
            r = t.write("override.created", target_type="override", target_id="OVR-1")
        self.assertEqual((r["actor"], r["actor_role"], r["ip"], r["user_agent"]), ("cos@x", "COS", "41.58.1.1", "UA/1"))

    def test_csv_export_neutralises_formulas(self):
        db = scratch_db(); t = AuditTrail(db)
        t.write("system.test", summary="=HYPERLINK(\"http://evil\")")
        self.assertIn("'=HYPERLINK", t.export_csv())


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = load_config()
        cls.state = get_state(cfg)
        if not cls.state.db.one("SELECT id FROM portfolios LIMIT 1"):
            seed_demo(cls.state.db, cfg, cfg.data_dir)
        cls.state.reset()

    def test_run_is_fully_audited(self):
        out = run_once(as_of="2026-11-04", echo=False, triggered_by="unit-test")
        rows = self.state.trail.list(limit=500, run_id=out["run_id"])
        events = [r["event"] for r in rows]
        for e in ("run.started", "data.loaded", "data.overrides_loaded", "rules.evaluated", "llm.assessment",
                  "decision.made", "email.simulated", "review.queued", "findings.recorded", "run.completed"):
            self.assertIn(e, events, e)
        self.assertEqual(events.count("llm.assessment"), 8)
        self.assertEqual(events.count("decision.made"), 12)
        self.assertTrue(all(r["actor"] == "agent" for r in rows))
        started = next(r for r in rows if r["event"] == "run.started")
        self.assertEqual(started["data"]["triggered_by"], "unit-test")
        llm = next(r for r in rows if r["event"] == "llm.assessment")
        for k in ("model", "steps", "tool_calls", "latency_ms", "usage", "rule_reviews"):
            self.assertIn(k, llm["data"])
        self.assertTrue(self.state.trail.verify()["ok"])

    def test_reset_keeps_audit_and_is_audited(self):
        before = self.state.trail.stats()["last"]["seq"]
        self.state.reset()
        after = self.state.trail.list(limit=5)
        self.assertGreater(after[0]["seq"], before)
        self.assertIn("system.reset", [r["event"] for r in after])

    def test_sign_in_code_never_stored_in_email_log_or_audit(self):
        from agent.auth import Auth
        from agent.mailer import Mailer
        cfg = load_config()
        m = Mailer(cfg, self.state, echo=False)
        Auth(cfg, self.state, m).request_code("cos@bnh.example", "41.58.9.9", "UA/1")
        log = self.state.emails.list(1)[0]
        self.assertNotRegex(log["subject"] + log["body"], r"\d{6}")
        aud = self.state.trail.list(limit=1, category="email")[0]
        self.assertNotRegex(str(aud["data"]) + (aud["summary"] or ""), r"\b\d{6}\b")
        req = self.state.trail.list(limit=1, q="auth.code_requested")[0]
        self.assertEqual((req["ip"], req["outcome"]), ("41.58.9.9", "success"))


if __name__ == "__main__":
    unittest.main()
