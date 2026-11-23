"""LLM injection classifier for Anvil, on the agents' own provider and keys.

Anvil accepts any async ``(text) -> InjectionResult`` classifier (Phase 1)
and runs it in hybrid mode: regex first, the classifier only for text the
regex passes. This one reuses the platform's ClientProvider, so it uses
the same provider, keys and base URL as the agents. Every call reports
its token usage and estimated cost, so scan cost appears in the run's
trace next to the agents' own.

If the call or the parsing fails, it raises, and Anvil falls back to its
regex scanner. A scan never breaks a run.
"""

from __future__ import annotations

from typing import Any, Callable

from agent_forge.core.context import RunContext, current_run_context
from anvil.injection_prompts import BASE_PROMPT, OUTPUT_TEMPERATURE, classifier_prompt, parse_verdict
from anvil.validators import InjectionResult, ScanContext

from .agents import ClientProvider

# The prompt says what an injection is *and* what it isn't. The first
# version only listed attacks, and on a banking agent gpt-4o-mini then
# flagged 3-4 of 8 ordinary requests ("What's my balance?", "Send $25 to
# ...") as injections. tests/test_e2e.py has a live regression check. It now
# lives in Anvil (anvil.injection_prompts), next to the eval that measures it.
CLASSIFIER_PROMPT = BASE_PROMPT


def is_classifier_request(messages: list) -> bool:
    """True if *messages* is a classifier call (for test doubles of the LLM)."""
    first = messages[0] if messages else None
    content = first.get("content") if isinstance(first, dict) else getattr(first, "content", None)
    return isinstance(content, str) and content.startswith(BASE_PROMPT.split("\n", 1)[0])


_MAX_CHARS = 4000

# (ctx or None, usage dict) -> None
UsageObserver = Callable[[RunContext | None, dict[str, Any]], None]


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Same price table as agent costs (agent_forge.pricing); None if the
    model has no price there."""
    from agent_forge.pricing import estimate_cost

    return estimate_cost(model, input_tokens, output_tokens)


class ProviderClassifier:
    def __init__(
        self,
        clients: ClientProvider,
        provider: str,
        model: str | None = None,
        *,
        on_usage: UsageObserver | None = None,
    ) -> None:
        self.provider = provider
        self.model = model or ClientProvider.default_model(provider)
        self._clients = clients
        self._on_usage = on_usage

    async def __call__(self, text: str, context: ScanContext | None = None) -> InjectionResult:
        text = text[:_MAX_CHARS]
        # Agent replies get the output_context prompt at temperature 0, as the
        # injection eval measured it; other directions the base prompt and the
        # API's default temperature. See anvil.injection_prompts.
        direction = context.direction if context else None
        system = classifier_prompt(direction, context.as_dict() if context else None)
        sampling = {"temperature": OUTPUT_TEMPERATURE} if direction == "output" else {}
        client = self._clients.get(self.provider)
        if self.provider == "anthropic":
            resp = await client.messages.create(
                model=self.model,
                max_tokens=100,
                system=system,
                messages=[{"role": "user", "content": text}],
                **sampling,
            )
            content = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            usage = (resp.usage.input_tokens, resp.usage.output_tokens)
        else:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                **sampling,
            )
            content = resp.choices[0].message.content or ""
            u = resp.usage
            usage = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)

        try:
            # {"injection": ...}, or for output scans {"label": ..., "reason": ...}
            result = parse_verdict(content)
        except ValueError:
            self._report(usage, detected=None, pattern=None)  # unreadable: Anvil falls back to regex
            raise
        self._report(usage, detected=result.detected, pattern=result.matched_pattern, label=result.label)
        return result

    def _report(self, usage: tuple[int, int], *, detected: bool | None, pattern: str | None,
                label: str | None = None) -> None:
        if self._on_usage is None:
            return
        self._on_usage(current_run_context(), {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "cost_usd": _estimate_cost(self.model, *usage),  # None: unpriced model
            "detected": detected,  # None: the reply wasn't a verdict, so Anvil used its regex scan
            "pattern": pattern,
            "label": label,  # output scans: ok, ai_instruction, data_leak, unrequested_action
        })


def default_provider(backends: list[str]) -> str:
    """The most common backend among the bound app's agents (openai on a tie)."""
    if not backends:
        return "openai"
    counts = {b: backends.count(b) for b in set(backends)}
    best = max(counts.values())
    return "openai" if counts.get("openai") == best else max(counts, key=counts.get)
