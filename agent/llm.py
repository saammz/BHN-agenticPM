"""LLM clients. OpenRouterClient speaks the OpenAI-compatible chat/completions API with tool
calling. MockLLM is a deterministic stand-in so the loop runs without a key (and in tests)."""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List

from .config import Config


class LLMError(Exception):
    pass


class OpenRouterClient:
    name = "openrouter"

    def __init__(self, cfg: Config):
        if not cfg.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set (add it to agenticPM/.env)")
        self.cfg = cfg
        self.model = cfg.openrouter_model
        self.last_usage: Dict[str, Any] = {}

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 1500,
        }
        if self.cfg.openrouter_fallback_models:
            body["models"] = [self.model] + self.cfg.openrouter_fallback_models
        req_body = json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://bnh.example/agentic-pm",
            "X-Title": "BNH PM Compliance Agent",
        }
        last_err = ""
        for attempt in range(self.cfg.llm_max_retries + 1):
            req = urllib.request.Request(f"{self.cfg.openrouter_base_url}/chat/completions",
                                         data=req_body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout_s) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode(errors="replace")
                last_err = f"HTTP {e.code}: {detail}"
                if e.code in (408, 429, 500, 502, 503, 504) and attempt < self.cfg.llm_max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(last_err) from e
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                last_err = f"network: {e}"
                if attempt < self.cfg.llm_max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(last_err) from e
            if "error" in data:
                raise LLMError(f"OpenRouter error: {data['error']}")
            try:
                msg = data["choices"][0]["message"]
            except (KeyError, IndexError) as e:
                raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
            self.last_usage = {"model": data.get("model"), **(data.get("usage") or {})}
            return msg
        raise LLMError(last_err or "exhausted retries")


DISTRESS = ("suspend", "halt", "frozen", "freeze", "default", "insolv", "litigation",
            "fraud", "revoked", "shut down", "liquidat", "write off", "writing off")


def _tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


class MockLLM:
    """Behaves like a cautious analyst: fetches history, checks precedents, then submits."""
    name = "mock"
    model = "mock-analyst-v1"
    last_usage: Dict[str, Any] = {}

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        called = [tc["function"]["name"] for m in messages if m["role"] == "assistant"
                  for tc in (m.get("tool_calls") or [])]
        if "get_submission_history" not in called:
            return _tool_call("get_submission_history", {"months": 6})
        if "get_override_history" not in called:
            return _tool_call("get_override_history", {})

        ctx = json.loads(re.search(r"```json\n(.*?)\n```", messages[1]["content"], re.S).group(1))
        tool_results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        history = next((t["submissions"] for t in tool_results if "submissions" in t), [])
        reviews, new = [], []
        latest = history[-1] if history else None

        for f in ctx["rule_findings"]:
            rid, month = f["rule_id"], f["reporting_month"]
            conf, note = 1.0, "Confirmed: the records show this clearly."
            if rid == "CASH_RAG_INCONSISTENT":
                sub = next((s for s in history if s["reporting_month"] == month), {})
                text = sub.get("execution_blocker", "").lower()
                if "distribution" in text or "paid to" in text:
                    conf, note = 0.62, ("Cash fell sharply, but the manager says it was a planned payout to investors, "
                                        "which would be fine. I can't confirm that from the reports alone, so a person "
                                        "should check before we contact the manager.")
                else:
                    conf, note = 0.92, "Cash is falling or negative, the status still says on track, and nothing in the update explains why."
            elif rid == "WEAK_BLOCKER":
                conf, note = 0.9, "A Red or Amber status needs a real explanation so the Chief of Staff knows how to help."
            elif rid == "STALE_BLOCKER":
                conf, note = 0.85, "The same words every month suggest the explanation was copied rather than updated."
            elif rid == "SUSPICIOUS_CONTENT":
                conf, note = 0.95, ("The update tries to give the AI instructions. I ignored them and judged the "
                                    "portfolio on its numbers. Someone should ask the manager why that text is there.")
            reviews.append({"rule_id": rid, "reporting_month": month, "verdict": "confirm",
                            "confidence": conf, "note": note})

        if latest and latest["rag_status"] == "GREEN":
            text = latest["execution_blocker"]
            hit = next((w for w in DISTRESS if w in text.lower()), None)
            if hit:
                sentence = next((s for s in re.split(r"(?<=[.;])\s+", text) if hit in s.lower()), text)
                new.append({
                    "rule_id": "NARRATIVE_RAG_MISMATCH", "reporting_month": latest["reporting_month"],
                    "severity": "HIGH", "confidence": 0.9, "recommended_action": "escalate_cos",
                    "rationale": "The manager marked this Green (on track), but the update describes serious trouble. "
                                 "By BNH's own definitions this looks Red, meaning it needs your intervention.",
                    "evidence": [
                        {"submission_id": latest["id"], "field": "rag_status", "value": "GREEN"},
                        {"submission_id": latest["id"], "field": "execution_blocker", "value": sentence.strip()},
                    ]})
        summary = f"{len(reviews)} rule finding(s) reviewed, {len(new)} new finding(s)."
        return _tool_call("submit_assessment", {"rule_reviews": reviews, "new_findings": new, "summary": summary})


def get_llm(kind: str, cfg: Config):
    if kind == "mock":
        return MockLLM()
    if kind == "openrouter":
        return OpenRouterClient(cfg)
    raise LLMError(f"unknown llm kind {kind!r}")
