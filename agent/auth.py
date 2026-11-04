"""Passwordless sign-in: email -> 6-digit code + one-click link (Resend), showing the requesting IP and device.

Security properties
- Codes and link tokens are stored only as HMAC-SHA256 hashes (keyed with AUTH_SECRET); they expire after
  LOGIN_CODE_TTL_MIN, work once, and requesting a new code invalidates older ones.
- At most 5 wrong guesses per code; rate limits per email and per IP; responses never reveal whether an
  email has an account (no user enumeration).
- Sessions: random 256-bit token in an HttpOnly, SameSite=Lax cookie (Secure on https); only its SHA-256 is
  stored. Idle timeout SESSION_IDLE_MIN, absolute limit SESSION_MAX_HOURS. Deactivating a user ends their sessions.
- Every request, success and failure is written to auth_events with IP and user agent."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from .config import WAT, Config
from .mailer import Mailer, login_code_email, new_signin_alert_email
from .store import State, now_iso

MAX_CODE_ATTEMPTS = 5
RL_WINDOW_MIN = 15
RL_PER_EMAIL = 5
RL_PER_IP = 20
RL_VERIFY_FAIL_PER_IP = 25
SESSION_TOUCH_SECONDS = 60


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(WAT)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="microseconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Auth:
    def __init__(self, cfg: Config, state: State, mailer: Mailer):
        self.cfg, self.state, self.db, self.mailer = cfg, state, state.db, mailer

    # ── helpers ─────────────────────────────────────────────────────────────────────
    def _h(self, value: str) -> str:
        return hmac.new(self.cfg.auth_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _event(self, event: str, email: Optional[str] = None, user_id: Optional[str] = None, ip: Optional[str] = None,
               ua: Optional[str] = None, **detail: Any) -> None:
        self.db.insert("auth_events", {"id": str(uuid.uuid4()), "ts": now_iso(), "event": event, "email": email,
                                       "user_id": user_id, "ip": ip, "user_agent": (ua or "")[:512], "detail": detail})
        failure = event in ("verify_failed", "rate_limited", "code_requested_unknown")
        self.state.audit(f"auth.{event}", outcome="failure" if failure else "success",
                         actor=email or "anonymous", actor_id=user_id, ip=ip, user_agent=ua,
                         target_type="user", target_id=user_id or email, **detail)

    def _count(self, events: Tuple[str, ...], col: str, value: Optional[str]) -> int:
        since = _iso(_now() - timedelta(minutes=RL_WINDOW_MIN))
        ph = ", ".join(["%s"] * len(events))
        row = self.db.one(f"SELECT COUNT(*) AS n FROM auth_events WHERE event IN ({ph}) AND {col} = %s AND ts > %s",
                          [*events, value, since])
        return int(row["n"]) if row else 0

    def user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM users WHERE email = %s", [email.strip().lower()])

    # ── step 1: request a code ──────────────────────────────────────────────────────
    def request_code(self, email: str, ip: Optional[str], ua: Optional[str]) -> Optional[str]:
        """Creates a code. Email mode: emails it and returns None. Screen mode: returns it for the page; no email."""
        email = (email or "").strip().lower()
        if "@" not in email or len(email) > 254:
            raise AuthError("Enter a valid email address.")
        if self._count(("code_requested", "code_requested_unknown"), "ip", ip) >= RL_PER_IP:
            self._event("rate_limited", email, ip=ip, ua=ua, scope="ip")
            raise AuthError("Too many sign-in requests from this network. Try again in 15 minutes.", 429)
        if self._count(("code_requested",), "email", email) >= RL_PER_EMAIL:
            self._event("rate_limited", email, ip=ip, ua=ua, scope="email")
            raise AuthError("Too many codes requested for this email. Try again in 15 minutes.", 429)
        user = self.user_by_email(email)
        if not user or user["status"] != "ACTIVE":
            self._event("code_requested_unknown", email, ip=ip, ua=ua)
            if self.cfg.auth_code_delivery == "screen":
                raise AuthError("No active account uses that email address.", 404)
            return None  # same response as success: no account enumeration
        now = _now()
        self.db.x("UPDATE login_codes SET used_at = %s WHERE user_id = %s AND used_at IS NULL", [_iso(now), user["id"]])
        code = f"{secrets.randbelow(1_000_000):06d}"
        link_token = secrets.token_urlsafe(32)
        self.db.insert("login_codes", {
            "id": str(uuid.uuid4()), "user_id": user["id"], "code_hash": self._h(f"{user['id']}:{code}"),
            "link_hash": self._h(f"link:{link_token}"), "ip": ip, "user_agent": (ua or "")[:512], "attempts": 0,
            "expires_at": _iso(now + timedelta(minutes=self.cfg.login_code_ttl_min)), "used_at": None,
            "created_at": _iso(now)})
        if self.cfg.auth_code_delivery == "screen":
            self._event("code_requested", email, user["id"], ip, ua, delivery="screen", status="shown")
            return code
        link = f"{self.cfg.app_url}/auth/link?token={link_token}"
        mail = login_code_email(user["name"], code, link, ip, ua, now, self.cfg.login_code_ttl_min)
        sent = self.mailer.send(user["email"], mail["subject"], mail["text"], "auth_code", html_body=mail["html"],
                                transactional=True, meta={"ip": ip},
                                log_body=f"[sign-in code and link redacted] requested from {ip}",
                                log_subject="[code redacted] is your BNH sign-in code")
        self._event("code_requested", email, user["id"], ip, ua, delivery=sent["delivery"], status=sent["status"])
        if sent["status"] == "failed":
            raise AuthError("We couldn't send the email just now. Try again in a minute.", 503)
        return None

    # ── step 2a: verify the code ────────────────────────────────────────────────────
    def verify_code(self, email: str, code: str, ip: Optional[str], ua: Optional[str]) -> Tuple[str, Dict[str, Any]]:
        email = (email or "").strip().lower()
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        if self._count(("verify_failed",), "ip", ip) >= RL_VERIFY_FAIL_PER_IP:
            raise AuthError("Too many wrong codes from this network. Try again in 15 minutes.", 429)
        generic = AuthError("That code is wrong or has expired. Request a new one.", 401)
        user = self.user_by_email(email)
        if not user or user["status"] != "ACTIVE" or len(code) != 6:
            self._event("verify_failed", email, ip=ip, ua=ua, reason="unknown user or malformed code")
            raise generic
        row = self.db.one("SELECT * FROM login_codes WHERE user_id = %s AND used_at IS NULL "
                          "ORDER BY created_at DESC LIMIT 1", [user["id"]])
        if not row or _parse(row["expires_at"]) < _now() or row["attempts"] >= MAX_CODE_ATTEMPTS:
            self._event("verify_failed", email, user["id"], ip, ua, reason="no live code")
            raise generic
        if not hmac.compare_digest(row["code_hash"], self._h(f"{user['id']}:{code}")):
            self.db.x("UPDATE login_codes SET attempts = attempts + 1 WHERE id = %s", [row["id"]])
            left = MAX_CODE_ATTEMPTS - row["attempts"] - 1
            self._event("verify_failed", email, user["id"], ip, ua, reason="wrong code", attempts_left=left)
            if left <= 0:
                self.db.x("UPDATE login_codes SET used_at = %s WHERE id = %s", [now_iso(), row["id"]])
                raise AuthError("Too many wrong attempts. Request a new code.", 401)
            raise generic
        return self._complete(user, row, ip, ua, "code")

    # ── step 2b: verify the one-click link (POST from the confirm page, so mail scanners can't use it) ──
    def verify_link(self, token: str, ip: Optional[str], ua: Optional[str]) -> Tuple[str, Dict[str, Any]]:
        row = self.db.one("SELECT * FROM login_codes WHERE link_hash = %s", [self._h(f"link:{token or ''}")])
        if not row or row["used_at"] or _parse(row["expires_at"]) < _now():
            self._event("verify_failed", ip=ip, ua=ua, reason="bad or expired link")
            raise AuthError("This sign-in link has expired or was already used. Request a new one.", 401)
        user = self.db.one("SELECT * FROM users WHERE id = %s", [row["user_id"]])
        if not user or user["status"] != "ACTIVE":
            raise AuthError("This account is not active.", 403)
        return self._complete(user, row, ip, ua, "link")

    def _complete(self, user, code_row, ip, ua, method) -> Tuple[str, Dict[str, Any]]:
        now = _now()
        n = self.db.x("UPDATE login_codes SET used_at = %s WHERE id = %s AND used_at IS NULL", [_iso(now), code_row["id"]])
        if n == 0:
            raise AuthError("This code was already used. Request a new one.", 401)
        token = secrets.token_urlsafe(32)
        self.db.insert("auth_sessions", {
            "id": str(uuid.uuid4()), "token_hash": hashlib.sha256(token.encode()).hexdigest(), "user_id": user["id"],
            "ip": ip, "user_agent": (ua or "")[:512],
            "expires_at": _iso(now + timedelta(hours=self.cfg.session_max_hours)),
            "last_seen_at": _iso(now), "revoked_at": None, "created_at": _iso(now)})
        self.db.x("UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s", [_iso(now), ip, user["id"]])
        self._event("signed_in", user["email"], user["id"], ip, ua, method=method, requested_from=code_row["ip"])
        if self.cfg.auth_code_delivery == "email" and code_row.get("ip") and ip and code_row["ip"] != ip:
            mail = new_signin_alert_email(user["name"], code_row["ip"], ip, ua, now)
            self.mailer.send(user["email"], mail["subject"], mail["text"], "auth_alert", transactional=True)
        return token, user

    # ── sessions ─────────────────────────────────────────────────────────────────────
    def session_user(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        row = self.db.one(
            "SELECT s.id AS session_id, s.expires_at, s.last_seen_at, s.revoked_at, u.* FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s", [hashlib.sha256(token.encode()).hexdigest()])
        if not row or row["revoked_at"] or row["status"] != "ACTIVE":
            return None
        now = _now()
        if _parse(row["expires_at"]) < now or _parse(row["last_seen_at"]) + timedelta(minutes=self.cfg.session_idle_min) < now:
            return None
        if (now - _parse(row["last_seen_at"])).total_seconds() > SESSION_TOUCH_SECONDS:
            self.db.x("UPDATE auth_sessions SET last_seen_at = %s WHERE id = %s", [_iso(now), row["session_id"]])
        return row

    def logout(self, token: Optional[str], ip: Optional[str] = None) -> None:
        if token:
            self.db.x("UPDATE auth_sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
                      [now_iso(), hashlib.sha256(token.encode()).hexdigest()])
            self._event("signed_out", ip=ip)

    def revoke_user_sessions(self, user_id: str) -> None:
        self.db.x("UPDATE auth_sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                  [now_iso(), user_id])
