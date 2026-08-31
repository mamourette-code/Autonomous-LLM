from __future__ import annotations

import asyncio

from autonomous.agent import Agent
from autonomous.events import QUEUE_SIZE, EventBus
from autonomous.providers import LLMResponse, MockProvider, ToolCall
from autonomous.tools.base import Tool, ToolRegistry


async def test_subscribers_receive_published_events():
    bus = EventBus()
    async with bus.subscribe() as queue:
        bus.publish("run.step", run_id=1, kind="answer")
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event.type == "run.step"
    assert event.data == {"run_id": 1, "kind": "answer"}


async def test_unsubscribed_queues_stop_receiving():
    bus = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0
    bus.publish("run.step")  # must not raise with nobody listening


async def test_a_stalled_subscriber_loses_history_not_liveness():
    """A tab that stops reading must not grow the queue without limit."""
    bus = EventBus()
    async with bus.subscribe() as queue:
        for i in range(QUEUE_SIZE + 10):
            bus.publish("run.step", n=i)

        assert queue.qsize() == QUEUE_SIZE
        # The oldest were dropped, so the newest event is still delivered.
        events = [queue.get_nowait() for _ in range(QUEUE_SIZE)]
        assert events[-1].data["n"] == QUEUE_SIZE + 9


async def test_publishing_never_blocks_a_run(settings, db):
    """Every step of a run reaches the bus, in order."""
    bus = EventBus()

    async def look_up() -> str:
        return "42"

    registry = ToolRegistry(
        [Tool(name="look_up", description="", parameters={"type": "object"}, fn=look_up)]
    )
    provider = MockProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="look_up", args={})]),
            LLMResponse(text="the answer is 42"),
        ]
    )

    async with bus.subscribe() as queue:
        result = await Agent(provider, registry, db, settings, bus).run("what is it?")
        received = [queue.get_nowait() for _ in range(queue.qsize())]

    assert result.status == "succeeded"
    assert [e.type for e in received] == [
        "run.started",
        "run.step",  # tool_call
        "run.step",  # tool_result
        "run.step",  # answer
        "run.finished",
    ]
    assert received[-1].data["status"] == "succeeded"


async def test_watcher_polls_are_published(settings, db):
    from autonomous.watchers.base import Observation, Watcher
    from autonomous.watchers.scheduler import Scheduler

    class OneShot(Watcher):
        name = "oneshot"
        interval_seconds = 60

        async def poll(self):
            return [Observation(key="a", title="First")]

    bus = EventBus()
    watcher = OneShot()
    scheduler = Scheduler(settings=settings, db=db, watchers=[watcher], bus=bus)

    async with bus.subscribe() as queue:
        await scheduler.poll_once(watcher)
        event = queue.get_nowait()

    assert event.type == "watcher.polled"
    assert event.data == {"watcher": "oneshot", "new_observations": 1}
