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


def test_retry_delay_is_read_from_the_api_message():
    from autonomous.providers.gemini import _is_retryable, _retry_after

    message = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota'}, 'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
        "'retryDelay': '52s'}]}"
    )
    assert _is_retryable(message)
    assert _retry_after(message, 2.0) == 53.0


def test_busy_and_rate_limited_are_retryable_but_bad_requests_are_not():
    from autonomous.providers.gemini import _is_retryable

    assert _is_retryable("503 UNAVAILABLE. model is overloaded")
    assert _is_retryable("429 RESOURCE_EXHAUSTED")
    assert not _is_retryable("400 INVALID_ARGUMENT. bad schema")
    assert not _is_retryable("404 NOT_FOUND. no such model")


def test_retry_falls_back_when_no_delay_is_offered():
    from autonomous.providers.gemini import _retry_after

    assert _retry_after("503 UNAVAILABLE", 4.0) == 4.0


async def test_a_rate_limited_call_is_retried_then_succeeds(monkeypatch):
    """The whole point: a 429 must not kill an unattended run."""
    import autonomous.providers.gemini as gemini_module
    from autonomous.providers.base import Message
    from autonomous.providers.gemini import GeminiProvider

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model = "gemini-3.6-flash"
    provider.max_retries = 2
    provider.retry_max_wait = 65.0

    attempts = 0

    class FakeModels:
        async def generate_content(self, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED 'retryDelay': '1s'")
            return type("R", (), {"candidates": [], "text": "recovered"})()

    provider._client = type("C", (), {"aio": type("A", (), {"models": FakeModels()})()})()

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gemini_module.asyncio, "sleep", fake_sleep)

    result = await provider.complete([Message(role="user", text="hi")])

    assert attempts == 2
    assert result.text == "recovered"
    assert slept == [2.0]  # the API asked for 1s, plus a second of headroom


async def test_retries_are_bounded(monkeypatch):
    import autonomous.providers.gemini as gemini_module
    from autonomous.providers.base import Message, ProviderError
    from autonomous.providers.gemini import GeminiProvider

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model = "m"
    provider.max_retries = 2
    provider.retry_max_wait = 65.0

    attempts = 0

    class AlwaysBusy:
        async def generate_content(self, **kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("503 UNAVAILABLE")

    provider._client = type("C", (), {"aio": type("A", (), {"models": AlwaysBusy()})()})()

    async def fake_sleep(_seconds):
        pass

    monkeypatch.setattr(gemini_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(ProviderError, match="503"):
        await provider.complete([Message(role="user", text="hi")])
    assert attempts == 3  # the initial call plus two retries


async def test_a_non_retryable_error_fails_immediately(monkeypatch):
    from autonomous.providers.base import Message, ProviderError
    from autonomous.providers.gemini import GeminiProvider

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model = "m"
    provider.max_retries = 2
    provider.retry_max_wait = 65.0

    attempts = 0

    class BadRequest:
        async def generate_content(self, **kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("400 INVALID_ARGUMENT")

    provider._client = type("C", (), {"aio": type("A", (), {"models": BadRequest()})()})()

    with pytest.raises(ProviderError, match="400"):
        await provider.complete([Message(role="user", text="hi")])
    assert attempts == 1


def test_thought_signatures_are_captured_and_replayed():
    """Gemini 3.x rejects a follow-up turn whose function calls lost their signature."""
    from autonomous.providers.base import Message, ToolCall
    from autonomous.providers.gemini import GeminiProvider, _tool_calls

    signature = b"opaque-signature-bytes"
    part = type(
        "P",
        (),
        {
            "function_call": type(
                "FC", (), {"name": "fetch_url", "args": {"url": "https://x"}, "id": "call-1"}
            )(),
            "thought_signature": signature,
        },
    )()
    response = type(
        "R", (), {"candidates": [type("C", (), {"content": type("Ct", (), {"parts": [part]})()})()]}
    )()

    calls = _tool_calls(response)
    assert calls[0].name == "fetch_url"
    assert calls[0].provider_state["thought_signature"] == signature

    provider = GeminiProvider.__new__(GeminiProvider)
    contents = provider._to_contents(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        **{
                            "name": calls[0].name,
                            "args": calls[0].args,
                            "id": calls[0].id,
                            "provider_state": calls[0].provider_state,
                        }
                    )
                ],
            )
        ]
    )
    assert contents[0].parts[0].thought_signature == signature
