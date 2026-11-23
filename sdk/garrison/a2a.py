"""Agent-to-agent (A2A) calls: identity from keys, never from headers.

The caller side is built into the platform: a ``type: remote_agent`` tool
POSTs ``{message, session_id}`` to the other app's ``/agent/chat`` with the
caller's own credential (issued to it by the callee) and two informational
headers, the call chain and the caller's trace ID.

The receiving side uses :class:`CallerKeys` to authenticate the caller by
its key alone, then starts the run with ``caller_app`` and
``parent_trace_id``::

    caller = keys.authenticate(request.headers)
    if caller is None:
        raise HTTPException(401)
    await platform.run_agent(AGENT, message, session_id=...,
                             caller_app=caller.app, parent_trace_id=caller.parent_trace_id,
                             chain=caller.chain)

**Limits.** Only the direct caller is authenticated (by its key). The chain
header and the trace ID are whatever the caller sent: a hop beyond the
direct caller is unverified, and a caller can claim any chain. Signed
chains (each hop signs the chain it forwards, e.g. with keys from the
garrison control plane) are future work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

from .compat import header

CALL_CHAIN_HEADER = "X-Garrison-Call-Chain"
TRACE_ID_HEADER = "X-Garrison-Trace-Id"
KEY_PREFIX = "ak_"
_HOP = re.compile(r"^[A-Za-z][\w-]{0,62}\.[A-Za-z][\w-]{0,62}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_HOPS = 10


def new_key(prefix: str = KEY_PREFIX) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_chain(value: str | None) -> list[str]:
    """The chain header as a list of "app.agent" hops. Malformed hops are
    dropped and it is cut to MAX_HOPS: informational only."""
    hops = [h.strip() for h in (value or "").split(",")]
    return [h for h in hops if _HOP.match(h)][:MAX_HOPS]


@dataclass(frozen=True)
class Caller:
    """An authenticated caller. ``app`` comes from the key; ``chain`` and
    ``parent_trace_id`` from headers (unverified)."""

    app: str
    user_id: str | None = None
    key_name: str | None = None
    chain: list[str] = field(default_factory=list)
    parent_trace_id: str | None = None


class CallerKeys:
    """Keys this app issued to other apps: SHA-256 of the key -> {"app",
    optional "user_id", optional "name"}. Only hashes are stored."""

    def __init__(self, entries: Mapping[str, Mapping[str, Any]], prefix: str = KEY_PREFIX) -> None:
        self.prefix = prefix
        self._entries = {h: dict(v) for h, v in entries.items()}
        for h, v in self._entries.items():
            if not v.get("app"):
                raise ValueError(f"caller key {h[:8]}... has no app")

    @classmethod
    def from_json(cls, text: str | None, prefix: str = KEY_PREFIX) -> CallerKeys:
        return cls(json.loads(text) if text else {}, prefix)

    @classmethod
    def from_env(cls, var: str, prefix: str = KEY_PREFIX) -> CallerKeys:
        """From an env var holding the JSON map, or a path to a JSON file."""
        value = os.environ.get(var, "")
        if value and os.path.exists(value):
            with open(value) as f:
                value = f.read()
        return cls.from_json(value or None, prefix)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def authenticate(self, headers: Mapping[str, str]) -> Caller | None:
        """The caller for these request headers, or None (unknown or no key).
        Identity comes from the Authorization key alone."""
        lower = {k.lower(): v for k, v in headers.items()}
        auth = lower.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token.startswith(self.prefix):
            return None
        entry = self._entries.get(hash_key(token))
        if entry is None:
            return None
        trace = header(lower, TRACE_ID_HEADER)
        return Caller(app=entry["app"], user_id=entry.get("user_id"), key_name=entry.get("name"),
                      chain=parse_chain(header(lower, CALL_CHAIN_HEADER)),
                      parent_trace_id=trace if trace and _TRACE_ID.match(trace) else None)
