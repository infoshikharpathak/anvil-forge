"""The reply a user gets when Anvil blocks the agent's own reply.

When the output scan flags the agent's final reply, none of the model's
text may be shown. The user still needs to know what happened, so the reply
is rebuilt from this turn's structured tool results:

- If the last tool call ended as an upstream error whose body carries a
  readable code and message (the body wasn't withheld, and the message
  passes Anvil's regex scan): "I couldn't complete that: {message} ({code})".
- Otherwise a generic line naming the last tool and how it ended, e.g.
  "Your request wasn't completed (pay_for_order: blocked)".
"""

from __future__ import annotations

from typing import Any

from anvil.validators import scan_for_injection

_MAX_MESSAGE = 300
_CODE_KEYS = ("error", "code", "error_code")
_MESSAGE_KEYS = ("message", "detail", "reason", "error_description")


def output_block(hook_denials: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The after_agent_run denial for an output that failed the injection
    scan, if this run had one."""
    # Agent Forge labels a denial "hook:<event>:<outcome>:<agent>".
    for denial in reversed(hook_denials):
        after_run = ":after_agent_run:" in (denial.get("guardrail") or "")
        if after_run and "injection_detected_agent_output" in (denial.get("message") or ""):
            return denial
    return None


def fallback_reply(tool_calls: list[dict[str, Any]]) -> tuple[str, str]:
    """(reply, kind) where kind is "template" or "generic"."""
    if not tool_calls:
        return "I couldn't send my reply: it was blocked by a safety check, and no tool was called.", "generic"
    last = tool_calls[-1]
    if last.get("violation") == "upstream_error":
        found = _code_and_message(last.get("response"))
        if found is not None:
            code, message = found
            return f"I couldn't complete that: {message} ({code})", "template"
    status = last.get("violation") or last.get("status") or ("allowed" if last.get("success") else "failed")
    return f"Your request wasn't completed ({last.get('tool', 'tool')}: {status})", "generic"


def _code_and_message(response: Any) -> tuple[str, str] | None:
    """Code and message from an upstream error, as Anvil hands it over:
    {"http_status": 403, "body": {...}}. None if either is missing, or the
    message doesn't pass the regex scan."""
    if not isinstance(response, dict) or not isinstance(response.get("body"), (dict, str)):
        return None
    body = response["body"]
    code = message = None
    for node in _dicts(body):
        code = code or next((node[k] for k in _CODE_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
        message = message or next(
            (node[k] for k in _MESSAGE_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
    if code is None and response.get("http_status"):
        code = f"HTTP {response['http_status']}"
    if code is None or message is None:
        return None
    message = " ".join(message.split())[:_MAX_MESSAGE]
    code = " ".join(str(code).split())[:60]
    if scan_for_injection(f"{code} {message}").detected:
        return None
    return code, message


def _dicts(node: Any, depth: int = 0):
    if depth > 4:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value, depth + 1)
