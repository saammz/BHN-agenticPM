from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
WAT = timezone(timedelta(hours=1), "WAT")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")


def _ensure_secret(name: str) -> None:
    """Generate AUTH_SECRET / WEBHOOK_SECRET once and persist them to .env (never printed)."""
    if os.environ.get(name):
        return
    value = secrets.token_urlsafe(32)
    os.environ[name] = value
    try:
        with (ROOT / ".env").open("a") as f:
            f.write(f'\n{name}="{value}"\n')
        os.chmod(ROOT / ".env", 0o600)
    except OSError:
        pass


if not os.environ.get("AGENTICPM_NO_SECRET_WRITE") and not os.environ.get("VERCEL"):
    _ensure_secret("AUTH_SECRET")
    _ensure_secret("WEBHOOK_SECRET")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("AGENTICPM_DATA_DIR", str(ROOT / "data"))))
    # Serverless platforms (Vercel) only allow writing under /tmp
    state_dir: Path = field(default_factory=lambda: Path(_env("AGENTICPM_STATE_DIR",
                                                             "/tmp/agentpm-state" if os.environ.get("VERCEL") else str(ROOT / "state"))))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""))
    openrouter_model: str = field(default_factory=lambda: _env("OPENROUTER_MODEL", "anthropic/claude-sonnet-5.5"))
    openrouter_fallback_models: List[str] = field(
        default_factory=lambda: [m.strip() for m in _env("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
    )
    openrouter_base_url: str = field(default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "60")))
    llm_max_retries: int = field(default_factory=lambda: int(_env("LLM_MAX_RETRIES", "3")))
    agent_max_steps: int = field(default_factory=lambda: int(_env("AGENT_MAX_STEPS", "6")))
    mode: str = field(default_factory=lambda: _env("AGENT_MODE", "auto"))
    conf_min_act: float = field(default_factory=lambda: float(_env("CONF_MIN_ACT", "0.6")))
    conf_auto_remind: float = field(default_factory=lambda: float(_env("CONF_AUTO_REMIND", "0.75")))

    # Storage: Neon Postgres when DATABASE_URL is set, otherwise SQLite in state/
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))
    # Email (Resend)
    resend_api_key: str = field(default_factory=lambda: _env("RESEND_API_KEY", ""))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM", ""))
    agent_email_delivery: str = field(default_factory=lambda: _env("AGENT_EMAIL_DELIVERY", "simulated"))
    agent_email_redirect_to: str = field(default_factory=lambda: _env("AGENT_EMAIL_REDIRECT_TO", ""))
    # Web / auth
    app_url: str = field(default_factory=lambda: _env("APP_URL", "http://localhost:8765").rstrip("/"))
    auth_secret: str = field(default_factory=lambda: _env("AUTH_SECRET", "dev-only-secret"))
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET", ""))
    trust_proxy: bool = field(default_factory=lambda: _env("TRUST_PROXY", "0") == "1")
    login_code_ttl_min: int = field(default_factory=lambda: int(_env("LOGIN_CODE_TTL_MIN", "10")))
    # "email" (default): sign-in codes go by email. "screen": no email; the code is shown on the sign-in page (demo).
    auth_code_delivery: str = field(default_factory=lambda: "screen" if _env("AUTH_CODE_DELIVERY", "email").strip().lower() == "screen" else "email")
    session_idle_min: int = field(default_factory=lambda: int(_env("SESSION_IDLE_MIN", "30")))
    session_max_hours: int = field(default_factory=lambda: int(_env("SESSION_MAX_HOURS", "12")))
    seed_cos_name: str = field(default_factory=lambda: _env("SEED_COS_NAME", "Chief of Staff"))
    seed_cos_email: str = field(default_factory=lambda: _env("SEED_COS_EMAIL", ""))
    # Plug-and-play settings (editable in the Settings page; stored in the app_config table)
    llm_kind: str = field(default_factory=lambda: _env("AGENT_LLM", "openrouter" if _env("OPENROUTER_API_KEY", "") else "mock"))
    daily_check_time: str = field(default_factory=lambda: _env("DAILY_CHECK_TIME", "08:00"))
    demo_as_of: str = field(default_factory=lambda: _env("DEMO_AS_OF", ""))

    @property
    def sqlite_path(self) -> Path:
        return self.state_dir / "agentpm.db"

    @property
    def has_resend(self) -> bool:
        return bool(self.resend_api_key and self.email_from)

    @property
    def has_openrouter_key(self) -> bool:
        return bool(self.openrouter_api_key)


# Values saved from the Settings page. They override .env defaults for non-secret settings.
EDITABLE: Dict[str, type] = {
    "mode": str, "llm_kind": str, "openrouter_model": str, "agent_email_delivery": str,
    "agent_email_redirect_to": str, "email_from": str, "conf_min_act": float, "conf_auto_remind": float,
    "daily_check_time": str, "demo_as_of": str,
}
_OVERLAY: Dict[str, Any] = {}


def set_overlay(values: Dict[str, Any]) -> None:
    _OVERLAY.clear()
    for k, v in values.items():
        if k in EDITABLE and v is not None:
            try:
                _OVERLAY[k] = EDITABLE[k](v)
            except (TypeError, ValueError):
                pass


def load_config() -> Config:
    cfg = Config()
    for k, v in _OVERLAY.items():
        setattr(cfg, k, v)
    return cfg


def write_env_secret(name: str, value: str) -> None:
    """Persist a secret (API key) to .env and the running process. Used by the Settings page."""
    import re
    path = ROOT / ".env"
    text = path.read_text() if path.exists() else ""
    line = f'{name}="{value}"'
    text = re.sub(rf"^{name}=.*$", line, text, flags=re.M) if re.search(rf"^{name}=", text, re.M) else text.rstrip() + "\n" + line + "\n"
    path.write_text(text)
    os.chmod(path, 0o600)
    os.environ[name] = value
