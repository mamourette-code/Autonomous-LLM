"""Tool registry.

A tool is an async function plus a JSON Schema describing its arguments. The
registry hands specs to the provider and dispatches the calls that come back.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from autonomous.errors import describe
from autonomous.providers.base import ToolSpec

ToolFn = Callable[..., Awaitable[str]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn
    # Tools that change something in the world, rather than just reading.
    mutating: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in self.names]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: no such tool {name!r}. Available: {', '.join(self.names)}"
        try:
            bound = _filter_args(tool.fn, args)
            return await tool.fn(**bound)
        except Exception as exc:
            # Tool failures are information for the model, not crashes for the run.
            return f"Error calling {name}: {describe(exc)}"


def _filter_args(fn: ToolFn, args: dict[str, Any]) -> dict[str, Any]:
    """Drop arguments the model invented that the tool does not accept."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    return {k: v for k, v in args.items() if k in params}
