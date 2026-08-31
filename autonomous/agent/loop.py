"""The agent loop: goal in, tool calls out, answer back.

One iteration = one model call plus any tools it asked for. The loop stops when
the model answers with text instead of a tool call, or when the step budget runs
out. Every step is written to the database as it happens, so the UI can follow a
run live and you can read back exactly what it did.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.events import EventBus
from autonomous.providers.base import LLMProvider, Message, ProviderError
from autonomous.storage import Database
from autonomous.tools import ToolRegistry

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an autonomous assistant running on the user's own machine.

Work towards the user's goal using the tools you are given. Guidelines:
- Prefer a tool over guessing. If you need a fact from the web, an API or the \
user's watchers, go and get it.
- Check `recent_observations` first when the goal concerns email, markets \
or anything the background watchers already track.
- Take one step at a time and use what you learn from each result.
- When you have enough to answer, stop calling tools and reply in plain prose.
- If a tool keeps failing or the goal is impossible, say so plainly and explain \
what blocked you. Do not invent results.
- Today's date is {today}.
"""


@dataclass(slots=True)
class RunResult:
    run_id: int
    status: str
    answer: str = ""
    error: str | None = None
    steps_used: int = 0
    transcript: list[Message] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        db: Database,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.db = db
        self.settings = settings
        self.bus = bus

    def _record(
        self, run_id: int, step: int, kind: str, *, name: str | None = None, content: str = ""
    ) -> None:
        """Persist a step and push it to any live panel in one go."""
        self.db.add_step(run_id, step, kind, name=name, content=content)
        if self.bus:
            self.bus.publish(
                "run.step", run_id=run_id, step=step, kind=kind, name=name, content=content
            )

    def _finish(
        self, run_id: int, status: str, *, result: str | None = None, error: str | None = None
    ) -> None:
        self.db.finish_run(run_id, status=status, result=result, error=error)
        if self.bus:
            self.bus.publish("run.finished", run_id=run_id, status=status, error=error)

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(today=datetime.now(UTC).date().isoformat())

    async def run(self, goal: str, *, run_id: int | None = None) -> RunResult:
        if run_id is None:
            run_id = self.db.create_run(goal, self.provider.name, self.provider.model)
        if self.bus:
            self.bus.publish("run.started", run_id=run_id, goal=goal)

        messages: list[Message] = [Message(role="user", text=goal)]
        specs = self.tools.specs()
        step = 0

        try:
            while step < self.settings.max_steps:
                step += 1
                try:
                    async with asyncio.timeout(self.settings.step_timeout_seconds):
                        response = await self.provider.complete(
                            messages, tools=specs, system=self._system_prompt()
                        )
                except TimeoutError:
                    raise ProviderError(
                        f"model call timed out after {self.settings.step_timeout_seconds}s"
                    ) from None

                if not response.tool_calls:
                    answer = response.text.strip() or "(the model returned no text)"
                    self._record(run_id, step, "answer", content=answer)
                    self._finish(run_id, "succeeded", result=answer)
                    messages.append(Message(role="assistant", text=answer))
                    return RunResult(
                        run_id, "succeeded", answer, steps_used=step, transcript=messages
                    )

                if response.text:
                    self._record(run_id, step, "thought", content=response.text)
                messages.append(
                    Message(role="assistant", text=response.text, tool_calls=response.tool_calls)
                )

                # The model may request several tools at once; run them together.
                for call in response.tool_calls:
                    self._record(
                        run_id,
                        step,
                        "tool_call",
                        name=call.name,
                        content=json.dumps(call.args, default=str),
                    )
                results = await asyncio.gather(
                    *(self.tools.call(call.name, call.args) for call in response.tool_calls)
                )
                for call, result in zip(response.tool_calls, results, strict=True):
                    self._record(run_id, step, "tool_result", name=call.name, content=result)
                    messages.append(
                        Message(role="tool", text=result, tool_name=call.name, tool_call_id=call.id)
                    )

            message = (
                f"Stopped after the {self.settings.max_steps}-step budget without reaching an "
                "answer. Raise MAX_STEPS or narrow the goal."
            )
            self._finish(run_id, "failed", error=message)
            return RunResult(run_id, "failed", error=message, steps_used=step, transcript=messages)

        except Exception as exc:
            log.exception("run %s failed", run_id)
            error = describe(exc)
            self._finish(run_id, "failed", error=error)
            return RunResult(run_id, "failed", error=error, steps_used=step, transcript=messages)
