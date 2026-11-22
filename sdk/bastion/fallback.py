"""The reply a user gets when Anvil blocks the agent's own reply.

When the output scan flags the agent's final reply, none of the model's
text may be shown. The user still needs to know what happened, so the reply
is rebuilt from this turn's structured tool results and from what the app
cancelled (Platform(on_output_blocked=...)):

- If the last tool call ended as an upstream error whose body carries a
  readable code and message (the body wasn't withheld, and the message
  passes Anvil's regex scan): "I couldn't complete that: {message} ({code})".
- Otherwise: "My reply was blocked by a safety check." and what the last
  call actually did: completed, failed, was blocked, or had its result
  withheld. It never says something wasn't done when it was.
- Either way, anything the turn created and the app cancelled is named:
  "initiate_transfer created a pending transfer, which has been cancelled."
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


def fallback_reply(
    tool_calls: list[dict[str, Any]],
    cancelled: list[dict[str, Any]] | None = None,
    not_cancelled: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """(reply, kind) where kind is "template" or "generic". *cancelled* and
    *not_cancelled* are what the app's on_output_blocked hook reported:
    [{"tool", "id", "what"}], e.g. what = "a pending transfer"."""
    after = [f"{c['tool']} created {c['what']}, which has been cancelled." for c in cancelled or []]
    after += [f"{c['tool']} created {c['what']}, which couldn't be cancelled automatically: "
              "decline it in your app." for c in not_cancelled or []]
    handled = {c["tool"] for c in [*(cancelled or []), *(not_cancelled or [])]}
    last = tool_calls[-1] if tool_calls else None
    if last is not None and last.get("violation") == "upstream_error":
        found = _code_and_message(last.get("response"))
        if found is not None:
            code, message = found
            return " ".join([f"I couldn't complete that: {message} ({code})", *after]), "template"
    lines = ["My reply was blocked by a safety check."]
    if last is None:
        lines.append("No action was taken.")
    elif last.get("tool") not in handled:
        lines.append(_what_happened(last))
    return " ".join(lines + after), "generic"


def _what_happened(call: dict[str, Any]) -> str:
    tool, violation = call.get("tool", "tool"), call.get("violation")
    if call.get("success"):
        return f"{tool} completed."
    if violation == "upstream_error":
        status = (call.get("response") or {}).get("http_status") if isinstance(call.get("response"), dict) else None
        return f"{tool} failed" + (f" (HTTP {status})." if status else ".")
    if call.get("tool_executed"):
        return f"{tool} ran, but its result was withheld ({violation})."
    return f"{tool} was blocked ({violation or call.get('status') or 'not run'})."


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
