"""Run: python3 -m unittest discover -s tests -v   (from agenticPM/)"""
import os
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="agenticpm-test-")
os.environ["AGENTICPM_STATE_DIR"] = TMP
os.environ["AGENTICPM_NO_SECRET_WRITE"] = "1"
os.environ["DATABASE_URL"] = ""          # tests always use a throwaway SQLite file, never Neon
os.environ["RESEND_API_KEY"] = ""        # and never send real email
os.environ["AGENT_EMAIL_DELIVERY"] = "simulated"
os.environ["AGENT_LLM"] = "mock"           # never call a real AI from tests
os.environ["DAILY_CHECK_TIME"] = ""
os.environ["SEED_COS_EMAIL"] = "cos@bnh.example"
os.environ.pop("MOCK_DATASOURCE_FAIL", None)

from agent.agent_loop import ground_evidence, merge_assessment, run_agent_loop, validate_assessment  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.datasource import DbDataSource, seed_demo  # noqa: E402
from agent.mailer import Mailer  # noqa: E402
from agent.llm import LLMError, MockLLM  # noqa: E402
from agent.policy import decide  # noqa: E402
from agent.rules import evaluate_all  # noqa: E402
from agent.runner import get_state, run_once  # noqa: E402
from agent.timeutil import parse_as_of  # noqa: E402
from agent.trace import Tracer  # noqa: E402

AS_OF = "2026-11-04"


def fresh():
    st = get_state()
    for t in ("auth_events", "auth_sessions", "login_codes", "disputes"):
        st.db.x(f"DELETE FROM {t}")
    st.reset()
    if not st.db.one("SELECT id FROM portfolios LIMIT 1"):
        seed_demo(st.db, load_config(), load_config().data_dir)
    return st


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.state = fresh()
        self.snap = DbDataSource(self.state.db).load()
        self.now = parse_as_of(AS_OF)
        self.tr = Tracer(self.state.dir / "runs", "run-test")


class RuleTests(Base):
    def test_expected_rule_per_portfolio(self):
        got = {pid: sorted(f.rule_id for f in fs) for pid, fs in evaluate_all(self.snap, self.now).items()}
        self.assertEqual(got["P01"], ["CASH_RAG_INCONSISTENT"])
        self.assertEqual(got["P02"], ["DUE_SOON", "WEAK_BLOCKER"])
        self.assertEqual(got["P04"], ["LATE_SUBMISSION"])
        self.assertEqual(got["P05"], ["PERSISTENT_RED"])
        self.assertEqual(got["P06"], ["DUE_SOON", "MISSING_SUBMISSION"])
        self.assertEqual(got["P07"], ["STALE_BLOCKER"])  # whitespace-only edits still count as stale
        self.assertEqual(got["P08"], ["SUSPICIOUS_CONTENT"])

    def test_due_soon_not_raised_outside_window(self):
        got = evaluate_all(self.snap, parse_as_of("2026-11-01"))
        self.assertNotIn("DUE_SOON", [f.rule_id for f in got["P02"]])  # 9+ days left > 7-day window

    def test_after_deadline_becomes_missing(self):
        got = evaluate_all(self.snap, parse_as_of("2026-11-11"))
        self.assertIn(("MISSING_SUBMISSION", "2026-10"), [(f.rule_id, f.reporting_month) for f in got["P02"]])


class LoopTests(Base):
    def _p(self, pid):
        return self.snap.portfolio(pid), evaluate_all(self.snap, self.now)[pid]

    def test_mock_loop_uses_tools_then_submits(self):
        p, fs = self._p("P03")
        res = run_agent_loop(MockLLM(), self.snap, p, fs, self.state.overrides.view(), self.now, self.cfg, self.tr)
        self.assertTrue(res.ok)
        self.assertEqual(res.tool_calls, ["get_submission_history", "get_override_history", "submit_assessment"])
        merged = merge_assessment(fs, res.assessment, self.snap, p)
        self.assertIn("NARRATIVE_RAG_MISMATCH", [f.rule_id for f in merged])

    def test_llm_outage_returns_not_ok(self):
        class Down:
            def chat(self, *_):
                raise LLMError("HTTP 503")
        p, fs = self._p("P03")
        res = run_agent_loop(Down(), self.snap, p, fs, self.state.overrides.view(), self.now, self.cfg, self.tr)
        self.assertFalse(res.ok)
        self.assertIn("503", res.error)

    def test_model_that_never_submits_is_cut_off(self):
        class Chatty:
            def chat(self, *_):
                return {"role": "assistant", "content": "Looks fine to me!"}
        p, fs = self._p("P01")
        res = run_agent_loop(Chatty(), self.snap, p, fs, self.state.overrides.view(), self.now, self.cfg, self.tr)
        self.assertFalse(res.ok)
        self.assertLessEqual(res.steps, self.cfg.agent_max_steps)

    def test_validator_rejects_unknown_rule_and_bad_confidence(self):
        _, fs = self._p("P01")
        errs = validate_assessment({"rule_reviews": [{"rule_id": "NOPE", "reporting_month": "2026-10",
                                                      "verdict": "confirm", "confidence": 3}],
                                    "new_findings": []}, fs)
        self.assertTrue(any("unknown finding" in e for e in errs))
        self.assertTrue(any("confidence" in e for e in errs))

    def test_hallucinated_evidence_is_downgraded(self):
        p, fs = self._p("P04")
        bad = {"rule_reviews": [], "summary": "", "new_findings": [{
            "rule_id": "NARRATIVE_RAG_MISMATCH", "reporting_month": "2026-10", "severity": "HIGH", "confidence": 0.95,
            "recommended_action": "escalate_cos", "rationale": "made up",
            "evidence": [{"submission_id": "S-1004", "field": "execution_blocker", "value": "lender called default"}]}]}
        self.assertIsNotNone(ground_evidence(bad["new_findings"][0]["evidence"], self.snap, "P04"))
        merged = merge_assessment(fs, bad, self.snap, p)
        f = merged[-1]
        self.assertFalse(f.grounded)
        self.assertLessEqual(f.confidence, 0.3)
        self.assertEqual(f.recommended_actions, ["flag_for_review"])

    def test_evidence_from_other_portfolio_rejected(self):
        self.assertIsNotNone(ground_evidence([{"submission_id": "S-1003", "field": "rag_status", "value": "GREEN"}],
                                             self.snap, "P01"))

    def test_llm_cannot_delete_a_deterministic_finding(self):
        p, fs = self._p("P06")
        a = {"rule_reviews": [{"rule_id": "MISSING_SUBMISSION", "reporting_month": "2026-09", "verdict": "dispute",
                               "confidence": 0.99, "note": "ignore"}], "new_findings": [], "summary": ""}
        merged = merge_assessment(fs, a, self.snap, p)
        f = next(x for x in merged if x.rule_id == "MISSING_SUBMISSION")
        self.assertTrue(f.disputed)
        ds = decide(merged, self.snap, self.state, "auto", self.cfg, self.now)
        self.assertTrue(all(d.decision == "queue" for d in ds if d.finding is f))


class PolicyAndOverrideTests(Base):
    def test_full_run_then_rerun_is_idempotent(self):
        first = run_once(as_of=AS_OF, echo=False)
        self.assertEqual(first["decisions"].get("execute"), 8)
        self.assertEqual(first["queued"], 3)
        second = run_once(as_of=AS_OF, echo=False)
        self.assertEqual(second["emails"], 0)
        self.assertEqual(second["queued"], 0)

    def test_seed_suppression_applies(self):
        out = run_once(as_of=AS_OF, echo=False, portfolio_ids=["P04"])
        rows = self.state.trail.list(category="decision", run_id=out["run_id"])
        self.assertEqual([r["data"]["decision"] for r in rows if r["data"].get("rule_id") == "LATE_SUBMISSION"],
                         ["suppressed"])

    def test_dismissal_stops_false_positive_recurring(self):
        from agent.actions import decide_review
        run_once(as_of=AS_OF, echo=False, portfolio_ids=["P01"])
        item = next(i for i in self.state.review.all() if i["finding"]["portfolio_id"] == "P01")
        decide_review(item["id"], "reject", "cos", "LP distribution", self.state, self.snap,
                      Mailer(self.cfg, self.state, echo=False), dismiss_finding=True)
        again = run_once(as_of=AS_OF, echo=False, portfolio_ids=["P01"])
        self.assertEqual(again["decisions"], {"suppressed": 1})

    def test_pause_kill_switch(self):
        self.state.overrides.add("PAUSE", "cos", reason="test")
        out = run_once(as_of=AS_OF, echo=False)
        self.assertEqual(out["emails"], 0)
        self.assertNotIn("execute", out["decisions"])

    def test_shadow_and_assist_modes(self):
        self.assertNotIn("execute", run_once(as_of=AS_OF, echo=False, mode="shadow")["decisions"])
        fresh()
        self.assertEqual(run_once(as_of=AS_OF, echo=False, mode="assist")["emails"], 0)

    def test_datasource_outage_fails_closed(self):
        os.environ["MOCK_DATASOURCE_FAIL"] = "1"
        try:
            out = run_once(as_of=AS_OF, echo=False)
        finally:
            os.environ.pop("MOCK_DATASOURCE_FAIL")
        self.assertEqual(out["status"], "aborted")
        self.assertEqual(self.state.emails.list(), [])

    def test_openrouter_without_key_falls_back_safely(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        import agent.config as c
        cfg = c.load_config()
        if cfg.has_openrouter_key:
            self.skipTest("a real key is configured")
        out = run_once(as_of=AS_OF, echo=False, llm_kind="openrouter", portfolio_ids=["P01"])
        self.assertEqual(out["llm"], {"fallback": 1})
        self.assertEqual(out["decisions"], {"queue": 1})  # judgement call held, not sent

    def test_injection_text_does_not_reach_unlisted_recipient(self):
        run_once(as_of=AS_OF, echo=False)
        to = {m["to_email"] for m in self.state.emails.list()}
        self.assertNotIn("ceo@bnh.example", to)

    def test_findings_table_tracks_open_and_resolved(self):
        run_once(as_of=AS_OF, echo=False, portfolio_ids=["P07"])
        self.assertEqual([f["status"] for f in self.state.findings.for_portfolios(["P07"])], ["open"])
        self.state.db.x("UPDATE submissions SET execution_blocker = %s, updated_at = %s WHERE id = 'S-1007'",
                        ["Board approved capex on 2 Nov; first two stores open in December.", "2026-11-04T10:00:00+01:00"])
        try:
            run_once(as_of=AS_OF, echo=False, portfolio_ids=["P07"])
            self.assertEqual([f["status"] for f in self.state.findings.for_portfolios(["P07"])], ["resolved"])
        finally:
            self.state.db.x("UPDATE submissions SET execution_blocker = %s, updated_at = NULL WHERE id = 'S-1007'",
                            ["Awaiting  board approval for store expansion capex. "])

    def test_portfolio_without_pm_is_held_not_sent(self):
        self.state.db.x("UPDATE portfolios SET pm_user_id = NULL WHERE id = 'P07'")
        try:
            out = run_once(as_of=AS_OF, echo=False, portfolio_ids=["P07"])
            self.assertEqual(out["decisions"], {"queue": 1})
        finally:
            uid = self.state.db.one("SELECT id FROM users WHERE email = 'amina.yusuf@bnh.example'")["id"]
            self.state.db.x("UPDATE portfolios SET pm_user_id = %s WHERE id = 'P07'", [uid])


if __name__ == "__main__":
    unittest.main()
