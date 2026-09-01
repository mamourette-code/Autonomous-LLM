from __future__ import annotations

import pytest

from autonomous.tools.base import Tool, ToolRegistry
from autonomous.tools.services import Service, call_service
from autonomous.tools.web import html_to_text


async def _echo(value: str, times: int = 1) -> str:
    return value * times


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(
        [Tool(name="echo", description="echo", parameters={"type": "object"}, fn=_echo)]
    )


async def test_call_dispatches(registry):
    assert await registry.call("echo", {"value": "ab", "times": 2}) == "abab"


async def test_unknown_tool_reports_instead_of_raising(registry):
    assert "no such tool" in await registry.call("nope", {})


async def test_hallucinated_arguments_are_dropped(registry):
    assert await registry.call("echo", {"value": "x", "colour": "blue"}) == "x"


async def test_tool_exceptions_become_messages():
    async def boom() -> str:
        raise ValueError("kaboom")

    registry = ToolRegistry(
        [Tool(name="boom", description="", parameters={"type": "object"}, fn=boom)]
    )
    result = await registry.call("boom", {})
    assert "Error calling boom" in result and "kaboom" in result


def test_html_to_text_strips_scripts_and_markup():
    html = "<html><body><script>evil()</script><h1>Title</h1><p>Body text</p></body></html>"
    out = html_to_text(html)
    assert "evil()" not in out
    assert "Title" in out and "Body text" in out


async def test_writes_refused_unless_service_allows_them(settings):
    services = {"ro": Service(name="ro", base_url="https://example.invalid", allow_writes=False)}
    result = await call_service(services, settings, "ro", "/thing", method="POST")
    assert "not allowed" in result


async def test_unconfigured_service_reports_missing_credential(settings, monkeypatch):
    monkeypatch.delenv("TOKEN_X", raising=False)
    services = {
        "svc": Service(
            name="svc", base_url="https://example.invalid", auth_type="bearer", auth_env="TOKEN_X"
        )
    }
    result = await call_service(services, settings, "svc", "/thing")
    assert "TOKEN_X" in result


async def test_empty_exception_messages_still_name_the_failure():
    """httpx timeouts stringify to '', which must not surface as a blank error."""
    import httpx

    async def timeout() -> str:
        raise httpx.ConnectTimeout("", request=httpx.Request("GET", "https://x"))

    registry = ToolRegistry(
        [Tool(name="t", description="", parameters={"type": "object"}, fn=timeout)]
    )
    assert await registry.call("t", {}) == "Error calling t: ConnectTimeout"


async def test_empty_observations_tell_the_model_what_to_do_next(settings, db):
    """A vague empty result makes the model retry the same tool, wasting a call."""
    from autonomous.tools import build_registry

    registry = build_registry(settings, db)
    result = await registry.call("recent_observations", {})

    assert "Do not call this tool again" in result
    assert "fetch_url" in result
