"""Server settings, from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./bastion_server.db")
    )
    session_days: int = field(default_factory=lambda: _int("BASTION_SESSION_DAYS", 7))
    signup_open: bool = field(default_factory=lambda: os.environ.get("BASTION_SIGNUP", "") == "open")
    # Login rate limit: failures allowed per window, per email and per client IP.
    login_window_minutes: int = field(default_factory=lambda: _int("BASTION_LOGIN_WINDOW_MIN", 15))
    login_max_per_email: int = field(default_factory=lambda: _int("BASTION_LOGIN_MAX_PER_EMAIL", 5))
    login_max_per_ip: int = field(default_factory=lambda: _int("BASTION_LOGIN_MAX_PER_IP", 20))
    # Cookies get Secure when the service is reached over https (set for deploys behind TLS).
    secure_cookies: bool = field(default_factory=lambda: os.environ.get("BASTION_SECURE_COOKIES", "") == "1")
    auto_migrate: bool = field(default_factory=lambda: os.environ.get("BASTION_AUTO_MIGRATE", "1") == "1")
    # Behind a reverse proxy, take the client IP from X-Forwarded-For (for the login rate limit).
    trust_proxy: bool = field(default_factory=lambda: os.environ.get("BASTION_TRUST_PROXY", "") == "1")
    # Trace ingestion: traces accepted per minute per app key, and the largest batch.
    trace_rate_per_minute: int = field(default_factory=lambda: _int("BASTION_TRACE_RATE", 100))
    trace_batch_max: int = field(default_factory=lambda: _int("BASTION_TRACE_BATCH_MAX", 100))


def load_settings() -> Settings:
    return Settings()
