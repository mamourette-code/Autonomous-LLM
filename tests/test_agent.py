from __future__ import annotations

from autonomous.agent import Agent
from autonomous.providers import LLMResponse, MockProvider, ToolCall
from autonomous.tools.base import Tool, ToolRegistry


def _registry(calls: list[dict]) -> ToolRegistry:
    async def lookup(city: str) -> str:
        calls.append({"city": city})
        return f"{city}: 18C, wind 12 kt"

    return ToolRegistry(
        [
            Tool(
                name="lookup",
                description="look up conditions",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                fn=lookup,
            )
        ]
    )


async def test_agent_calls_tool_then_answers(settings, db):
    calls: list[dict] = []
    provider = MockProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="lookup", args={"city": "Brest"})]),
            LLMResponse(text="Brest is 18C with 12 kt of wind."),
        ]
    )
    agent = Agent(provider, _registry(calls), db, settings)
    result = await agent.run("What are conditions in Brest?")

    assert result.status == "succeeded"
    assert result.answer == "Brest is 18C with 12 kt of wind."
    assert calls == [{"city": "Brest"}]

    steps = db.list_steps(result.run_id)
    assert [s["kind"] for s in steps] == ["tool_call", "tool_result", "answer"]
    assert "18C" in steps[1]["content"]
    assert db.get_run(result.run_id)["status"] == "succeeded"


async def test_tool_results_are_fed_back_to_the_model(settings, db):
    provider = MockProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="lookup", args={"city": "Brest"})]),
            LLMResponse(text="done"),
        ]
    )
    agent = Agent(provider, _registry([]), db, settings)
    await agent.run("check Brest")

    second_call = provider.calls[1]
    assert second_call[-1].role == "tool"
    assert second_call[-1].tool_name == "lookup"
    assert "18C" in second_call[-1].text


async def test_parallel_tool_calls_all_execute(settings, db):
    calls: list[dict] = []
    provider = MockProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(name="lookup", args={"city": "Brest"}),
                    ToolCall(name="lookup", args={"city": "Cherbourg"}),
                ]
            ),
            LLMResponse(text="both checked"),
        ]
    )
    agent = Agent(provider, _registry(calls), db, settings)
    result = await agent.run("compare two ports")

    assert result.status == "succeeded"
    assert {c["city"] for c in calls} == {"Brest", "Cherbourg"}


async def test_step_budget_stops_a_looping_model(settings, db):
    provider = MockProvider(
        lambda _messages: LLMResponse(tool_calls=[ToolCall(name="lookup", args={"city": "X"})])
    )
    agent = Agent(provider, _registry([]), db, settings)
    result = await agent.run("loop forever")

    assert result.status == "failed"
    assert "budget" in result.error
    assert result.steps_used == settings.max_steps
    assert db.get_run(result.run_id)["status"] == "failed"


async def test_provider_failure_is_recorded_not_raised(settings, db):
    def explode(_messages):
        raise RuntimeError("API is down")

    agent = Agent(MockProvider(explode), _registry([]), db, settings)
    result = await agent.run("anything")

    assert result.status == "failed"
    assert "API is down" in result.error
    assert db.get_run(result.run_id)["error"]
