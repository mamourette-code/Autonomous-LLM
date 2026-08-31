from __future__ import annotations

import pytest

from autonomous.providers import ProviderError, build_provider
from autonomous.providers.base import Message, ToolCall, ToolSpec


def test_unknown_provider_is_rejected(settings):
    with pytest.raises(ProviderError, match="unknown provider"):
        build_provider(settings, "definitely-not-a-provider")


def test_gemini_requires_an_api_key(settings):
    settings.gemini_api_key = None
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        build_provider(settings, "gemini")


def test_gemini_translates_a_full_conversation():
    """Message -> Gemini Content mapping, without touching the network."""
    from autonomous.providers.gemini import GeminiProvider

    provider = GeminiProvider.__new__(GeminiProvider)  # skip __init__: no key needed
    contents = provider._to_contents(
        [
            Message(role="user", text="what is the wave height?"),
            Message(
                role="assistant",
                text="checking",
                tool_calls=[ToolCall(name="fetch_url", args={"url": "https://x"})],
            ),
            Message(role="tool", text="1.4 m", tool_name="fetch_url"),
        ]
    )

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[1].function_call.name == "fetch_url"
    assert contents[2].parts[0].function_response.response == {"result": "1.4 m"}


def test_gemini_declares_tools_with_json_schema():
    from autonomous.providers.gemini import GeminiProvider

    provider = GeminiProvider.__new__(GeminiProvider)
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    tools = provider._to_tools([ToolSpec(name="fetch_url", description="fetch", parameters=schema)])

    declaration = tools[0].function_declarations[0]
    assert declaration.name == "fetch_url"
    assert declaration.parameters_json_schema == schema
