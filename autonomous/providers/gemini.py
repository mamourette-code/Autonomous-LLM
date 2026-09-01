"""Google Gemini backend, built on the google-genai SDK.

Automatic function calling is disabled on purpose: the agent loop in
``autonomous.agent.loop`` drives every tool call so each one can be persisted,
inspected in the UI and bounded by the step budget.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from autonomous.providers.base import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger(__name__)

# 429 is the free tier's requests-per-minute cap; 503 is a busy model. Both are
# worth waiting out: an agent run makes several calls, and a rule firing at 3am
# has nobody to retry it by hand.
RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")
_RETRY_DELAY = re.compile(r"retryDelay[\'\"]?\s*:\s*[\'\"]?(\d+(?:\.\d+)?)s")


def _retry_after(message: str, fallback: float) -> float:
    """Honour the delay the API asks for, when it gives one."""
    match = _RETRY_DELAY.search(message)
    if match:
        return float(match.group(1)) + 1.0
    return fallback


def _is_retryable(message: str) -> bool:
    return any(marker in message for marker in RETRYABLE)


def _tool_calls(response: Any) -> list[ToolCall]:
    """Pull tool calls off the response parts.

    Deliberately not ``response.function_calls``: that convenience view drops
    the per-part thought signature, which must be sent back on the next turn.
    """
    calls: list[ToolCall] = []
    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []) if content else []:
            function_call = getattr(part, "function_call", None)
            if not function_call:
                continue
            state: dict[str, Any] = {}
            if getattr(part, "thought_signature", None):
                state["thought_signature"] = part.thought_signature
            calls.append(
                ToolCall(
                    name=function_call.name or "",
                    args=dict(function_call.args or {}),
                    id=function_call.id,
                    provider_state=state,
                )
            )
    return calls


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        max_retries: int = 2,
        retry_max_wait: float = 65.0,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example) "
                "or pick another provider."
            )
        from google import genai  # imported lazily so the app starts without the key

        self.model = model
        self.max_retries = max_retries
        self.retry_max_wait = retry_max_wait
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    # --- translation -------------------------------------------------------

    def _to_contents(self, messages: list[Message]) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for msg in messages:
            if msg.role == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part.from_text(text=msg.text)])
                )
            elif msg.role == "assistant":
                parts = []
                if msg.text:
                    parts.append(types.Part.from_text(text=msg.text))
                for call in msg.tool_calls:
                    part = types.Part.from_function_call(name=call.name, args=call.args)
                    # Gemini 3.x rejects a follow-up turn whose function calls
                    # come back without the thought signature it issued.
                    signature = call.provider_state.get("thought_signature")
                    if signature:
                        part.thought_signature = signature
                    parts.append(part)
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif msg.role == "tool":
                # Gemini expects tool output as a function_response part.
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=msg.tool_name or "tool",
                                response={"result": msg.text},
                            )
                        ],
                    )
                )
            else:  # pragma: no cover - guarded by the dataclass contract
                raise ProviderError(f"unknown message role: {msg.role!r}")
        return contents

    def _to_tools(self, tools: list[ToolSpec] | None) -> list[Any] | None:
        if not tools:
            return None
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters_json_schema=spec.parameters,
            )
            for spec in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    # --- api ---------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._to_tools(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = self._to_contents(messages)
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except Exception as exc:  # SDK raises a family of transport/API errors
                message = str(exc)
                if attempt >= self.max_retries or not _is_retryable(message):
                    raise ProviderError(f"Gemini request failed: {exc}") from exc
                wait = min(_retry_after(message, delay), self.retry_max_wait)
                log.warning(
                    "Gemini is rate-limited or busy; retrying in %.0fs (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(wait)
                delay *= 2

        calls = _tool_calls(response)
        # .text raises if the candidate holds only function calls, hence the guard.
        text = "" if calls else (response.text or "")
        return LLMResponse(text=text, tool_calls=calls)
