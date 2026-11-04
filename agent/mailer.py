"""Email: Resend (https://resend.com/docs/api-reference/emails/send-email) plus delivery modes.

Sign-in emails always go for real when Resend is configured (otherwise they are printed to the server console,
for local development). Agent compliance emails follow AGENT_EMAIL_DELIVERY:
  simulated  logged only (default; the demo PMs have placeholder addresses)
  resend     sent to the real recipient
  redirect   sent for real, but to AGENT_EMAIL_REDIRECT_TO, with the intended recipient in the subject"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

from .config import WAT, Config
from .store import State, new_id, now_iso


class MailError(Exception):
    pass


def resend_send(cfg: Config, to: str, subject: str, text: str, html_body: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {"from": cfg.email_from, "to": [to], "subject": subject, "text": text}
    if html_body:
        payload["html"] = html_body
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {cfg.resend_api_key}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "bnh-agentpm/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("id", "")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise MailError(f"Resend HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MailError(f"Resend unreachable: {e}") from e


class Mailer:
    def __init__(self, cfg: Config, state: State, echo: bool = True):
        self.cfg, self.state, self.echo = cfg, state, echo

    def send(self, to: str, subject: str, body: str, kind: str, meta: Optional[Dict[str, Any]] = None,
             html_body: Optional[str] = None, transactional: bool = False,
             log_body: Optional[str] = None, log_subject: Optional[str] = None) -> Dict[str, Any]:
        meta = dict(meta or {})
        intended = None
        if transactional:
            delivery = "resend" if self.cfg.has_resend else "console"
        else:
            delivery = self.cfg.agent_email_delivery
            if delivery in ("resend", "redirect") and not self.cfg.has_resend:
                delivery = "simulated"
            if delivery == "redirect":
                if not self.cfg.agent_email_redirect_to:
                    delivery = "simulated"
                else:
                    intended, to = to, self.cfg.agent_email_redirect_to
                    subject = f"[DEMO → {intended}] {subject}"
        msg = {"id": new_id("MSG-"), "ts": now_iso(), "to_email": to, "intended_to": intended,
               "subject": (log_subject or subject)[:300],
               "body": log_body if log_body is not None else body, "kind": kind, "delivery": delivery,
               "provider_id": None, "status": "simulated", "error": None,
               "run_id": meta.pop("run_id", None), "portfolio_id": meta.pop("portfolio_id", None), "meta": meta}
        if delivery == "resend" or delivery == "redirect":
            try:
                msg["provider_id"] = resend_send(self.cfg, to, subject, body, html_body)
                msg["status"] = "sent"
            except MailError as e:
                msg["status"], msg["error"] = "failed", str(e)
        elif delivery == "console":
            msg["status"] = "console"
        self.state.emails.add(msg)
        self.state.audit(f"email.{msg['status']}", outcome="failure" if msg["status"] == "failed" else "success",
                         target_type="email", target_id=msg["id"], run_id=msg["run_id"],
                         summary=f"{kind} → {to}" + (f" (intended {intended})" if intended else ""),
                         to=to, intended_to=intended, subject=msg["subject"], kind=kind, delivery=delivery,
                         provider_id=msg["provider_id"], error=msg["error"], portfolio_id=msg["portfolio_id"])
        if self.echo or delivery == "console":
            bar = "─" * 72
            label = {"simulated": "📧  SIMULATED EMAIL", "console": "📧  EMAIL (no Resend key: printed here only)",
                     "resend": "📨  SENT VIA RESEND", "redirect": "📨  SENT VIA RESEND (redirected)"}[delivery]
            status = f"  [{msg['status']}{': ' + msg['error'] if msg['error'] else ''}]"
            print(f"\n{bar}\n{label}{status}  to: {to}\n    subject: {subject}\n{bar}\n{body}\n{bar}", flush=True)
        msg["to"] = msg["to_email"]
        return msg


# ── device / IP helpers ──────────────────────────────────────────────────────────────────
def describe_device(ua: Optional[str]) -> str:
    if not ua:
        return "Unknown device"
    browser = ("Edge" if "Edg/" in ua else "Opera" if re.search(r"OPR/|Opera", ua) else "Firefox" if "Firefox/" in ua
               else "Chrome" if "Chrome/" in ua else "Safari" if "Safari/" in ua
               else "curl" if ua.startswith("curl/") else "Unknown browser")
    os_ = ("iOS" if re.search(r"iPhone|iPad|iPod", ua) else "Android" if "Android" in ua
           else "macOS" if re.search(r"Mac OS X|Macintosh", ua) else "Windows" if "Windows" in ua
           else "Linux" if "Linux" in ua else "unknown OS")
    return f"{browser} on {os_}"


def describe_ip(ip: Optional[str]) -> str:
    if not ip:
        return "unknown IP address"
    if ip in ("127.0.0.1", "::1"):
        return f"{ip} (this computer — local development)"
    return ip


def _fmt_time(ts: datetime) -> str:
    return ts.astimezone(WAT).strftime("%d %b %Y, %H:%M WAT")


# ── auth email templates ─────────────────────────────────────────────────────────────────
def login_code_email(name: str, code: str, link: str, ip: Optional[str], ua: Optional[str], at: datetime,
                     ttl_min: int) -> Dict[str, str]:
    first = name.split()[0] if name else "there"
    where = describe_ip(ip)
    device = describe_device(ua)
    when = _fmt_time(at)
    text = (f"Hi {first},\n\nYour sign-in code for the BNH PM Compliance Agent is:\n\n    {code}\n\n"
            f"Or sign in with this link:\n{link}\n\nThe code and link expire in {ttl_min} minutes and work once.\n\n"
            f"This request came from:\n  IP address: {where}\n  Device:     {device}\n  Time:       {when}\n\n"
            "If this wasn't you, ignore this email. Nobody can sign in without the code, and your account is "
            "unchanged. If it keeps happening, tell the Chief of Staff's office.\n\n— BNH PM Compliance Agent")
    e = html.escape
    html_body = f"""<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:520px;color:#1b1b21">
<p>Hi {e(first)},</p><p>Your sign-in code for the <b>BNH PM Compliance Agent</b>:</p>
<p style="font:600 32px/1 ui-monospace,Menlo,monospace;letter-spacing:8px;background:#f4f4f6;padding:16px 20px;border-radius:8px;display:inline-block">{e(code)}</p>
<p><a href="{e(link)}" style="background:#ff6d5a;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:600">Sign in with one click</a></p>
<p style="color:#555">The code and link expire in {ttl_min} minutes and work once.</p>
<table style="border-collapse:collapse;font-size:14px;margin:16px 0;background:#fafafa;border:1px solid #eee;border-radius:6px">
<tr><td colspan="2" style="padding:8px 12px;font-weight:600">This request came from</td></tr>
<tr><td style="padding:4px 12px;color:#666">IP address</td><td style="padding:4px 12px;font-family:ui-monospace,Menlo,monospace">{e(where)}</td></tr>
<tr><td style="padding:4px 12px;color:#666">Device</td><td style="padding:4px 12px">{e(device)}</td></tr>
<tr><td style="padding:4px 12px 8px;color:#666">Time</td><td style="padding:4px 12px 8px">{e(when)}</td></tr></table>
<p style="color:#555;font-size:13px">If this wasn't you, ignore this email. Nobody can sign in without the code, and your account is unchanged.</p>
<p style="color:#999;font-size:12px">BNH PM Compliance Agent</p></div>"""
    return {"subject": f"{code} is your BNH sign-in code", "text": text, "html": html_body}


def new_signin_alert_email(name: str, req_ip: Optional[str], ip: Optional[str], ua: Optional[str],
                           at: datetime) -> Dict[str, str]:
    first = name.split()[0] if name else "there"
    text = (f"Hi {first},\n\nYour account was just signed in to the BNH PM Compliance Agent from a different place "
            f"than where the code was requested.\n\n  Code requested from: {describe_ip(req_ip)}\n"
            f"  Signed in from:      {describe_ip(ip)}\n  Device:              {describe_device(ua)}\n"
            f"  Time:                {_fmt_time(at)}\n\nIf this was you (for example, you opened the link on your "
            "phone), no action is needed. If not, contact the Chief of Staff's office straight away so the session "
            "can be revoked.\n\n— BNH PM Compliance Agent")
    return {"subject": "New sign-in to the BNH PM Compliance Agent", "text": text}
