"""Google Gemini backend, built on the google-genai SDK.

Automatic function calling is disabled on purpose: the agent loop in
``autonomous.agent.loop`` drives every tool call so each one can be persisted,
inspected in the UI and bounded by the step budget.
"""

from __future__ import annotations

from typing import Any

from autonomous.providers.base import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None, model: str) -> None:
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example) "
                "or pick another provider."
            )
        from google import genai  # imported lazily so the app starts without the key

        self.model = model
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
                    parts.append(types.Part.from_function_call(name=call.name, args=call.args))
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
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=self._to_contents(messages),
                config=config,
            )
        except Exception as exc:  # SDK raises a family of transport/API errors
            raise ProviderError(f"Gemini request failed: {exc}") from exc

        calls = [
            ToolCall(name=fc.name or "", args=dict(fc.args or {}), id=fc.id)
            for fc in (response.function_calls or [])
        ]
        # .text raises if the candidate holds only function calls, hence the guard.
        text = "" if calls else (response.text or "")
        return LLMResponse(text=text, tool_calls=calls)
