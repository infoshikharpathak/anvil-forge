"""Scripted stand-ins for the OpenAI and Anthropic async clients.

Each agent (keyed by its system prompt) gets a list of steps:
    {"tool": "get_accounts", "args": {...}}   -> the model calls a tool
    {"text": "..."}                           -> the model answers
Requests are recorded per agent so tests can see exactly what the model
was sent, including tool results (e.g. Anvil's corrective responses).
Classifier requests get a scripted verdict and are recorded separately.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from anthropic.types import Message
from openai.types.chat import ChatCompletion

from anvil_forge.classifier import CLASSIFIER_PROMPT

CLASSIFIER_MARKER = CLASSIFIER_PROMPT[:60]  # classifier calls carry the SDK's prompt


class _Script:
    def __init__(self, scripts: dict[str, list[dict]], injection_words=()):
        self.scripts = {k: v if callable(v) else list(v) for k, v in scripts.items()}
        self.requests: dict[str, list[dict]] = {}
        self.classifier_requests: list[str] = []
        self.injection_words = tuple(injection_words)

    def _classify(self, text: str) -> str:
        self.classifier_requests.append(text)
        hit = next((w for w in self.injection_words if w in text.lower()), None)
        return json.dumps({"injection": True, "pattern": f"says {hit}"} if hit else {"injection": False})

    def _next(self, system: str, kwargs: dict) -> dict:
        self.requests.setdefault(system, []).append(kwargs)
        steps = self.scripts.get(system)
        if callable(steps):  # a responder: step = fn(request kwargs)
            return steps(kwargs)
        if not steps:
            raise AssertionError(f"no scripted reply left for agent with prompt {system[:60]!r}")
        return steps.pop(0)


class ScriptedOpenAI(_Script):
    def __init__(self, scripts, injection_words=()):
        super().__init__(scripts, injection_words)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _completion(message: dict, finish: str, tokens=(10, 5)) -> ChatCompletion:
        return ChatCompletion.model_validate({
            "id": "c", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)},
        })

    async def _create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        if CLASSIFIER_MARKER in system:
            text = self._classify(kwargs["messages"][-1]["content"])
            return self._completion({"role": "assistant", "content": text}, "stop", tokens=(7, 3))
        step = self._next(system, kwargs)
        if "tool" in step:
            return self._completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{len(self.requests[system])}", "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))},
            }]}, "tool_calls")
        return self._completion({"role": "assistant", "content": step["text"]}, "stop")

    def tool_results(self, system: str) -> list[dict]:
        """Every tool result message the agent's model was sent, parsed."""
        out = []
        for req in self.requests.get(system, []):
            for m in req["messages"]:
                if isinstance(m, dict) and m.get("role") == "tool":
                    out.append(json.loads(m["content"]))
        # each request repeats history; keep the unique ones in order
        uniq = []
        for r in out:
            if r not in uniq:
                uniq.append(r)
        return uniq


class ScriptedAnthropic(_Script):
    def __init__(self, scripts, injection_words=()):
        super().__init__(scripts, injection_words)
        self.messages = SimpleNamespace(create=self._create)

    @staticmethod
    def _message(content: list, stop: str, tokens=(12, 6)) -> Message:
        return Message.model_validate({
            "id": "m", "type": "message", "role": "assistant", "model": "stub",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        })

    async def _create(self, **kwargs):
        system = kwargs.get("system", "")
        if CLASSIFIER_MARKER in system:
            text = self._classify(kwargs["messages"][-1]["content"])
            return self._message([{"type": "text", "text": text}], "end_turn", tokens=(7, 3))
        step = self._next(system, kwargs)
        if "tool" in step:
            n = len(self.requests[system])
            return self._message(
                [{"type": "tool_use", "id": f"tu_{n}", "name": step["tool"], "input": step.get("args", {})}], "tool_use"
            )
        return self._message([{"type": "text", "text": step["text"]}], "end_turn")

    def tool_results(self, system: str) -> list[dict]:
        out = []
        for req in self.requests.get(system, []):
            for m in req["messages"]:
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for block in m["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            r = json.loads(block["content"])
                            if r not in out:
                                out.append(r)
        return out
