"""Provider-neutral chat types.

The agent loop only ever sees these types. Adding a provider means translating
them in one file; nothing else in the codebase changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ToolCall:
    """A model's request to invoke one tool."""

    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass(slots=True)
class Message:
    """One turn. ``role`` is 'user', 'assistant' or 'tool'."""

    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on role == 'tool': which call this result answers.
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass(slots=True)
class ToolSpec:
    """A tool offered to the model, described with plain JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class LLMProvider(Protocol):
    """What the agent loop needs from any model backend."""

    name: str
    model: str

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> LLMResponse: ...


class ProviderError(RuntimeError):
    """Raised when a provider is unusable (missing key, bad response, API error)."""
