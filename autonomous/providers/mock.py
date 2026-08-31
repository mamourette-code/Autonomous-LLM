"""A deterministic provider used by the tests and by ``PROVIDER=mock``.

It never touches the network, so the whole agent loop - including tool
execution - can be exercised without an API key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from autonomous.providers.base import LLMResponse, Message, ToolSpec

Script = Iterable[LLMResponse] | Callable[[list[Message]], LLMResponse]


class MockProvider:
    name = "mock"

    def __init__(self, script: Script | None = None, model: str = "mock-1") -> None:
        self.model = model
        self.calls: list[list[Message]] = []
        if script is None:
            self._responses = iter([])
            self._fn = None
        elif callable(script):
            self._responses = iter([])
            self._fn = script
        else:
            self._responses = iter(script)
            self._fn = None

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self._fn is not None:
            return self._fn(messages)
        try:
            return next(self._responses)
        except StopIteration:
            last = messages[-1].text if messages else ""
            return LLMResponse(text=f"mock provider has no scripted reply for: {last[:200]}")
