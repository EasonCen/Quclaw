"""Tests for AgentWorker per-agent concurrency control."""

import asyncio
import sys

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.agent_loader import AgentLoader
from core.events import AgentEventSource, DispatchEvent, DispatchResultEvent
from server.agent_worker import AgentWorker
from utils.config import LLMConfig
from utils.def_loader import InvalidDefError


@dataclass
class FakeAgentDef:
    id: str
    max_concurrency: int = 1


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def subscribe(self, event_class: type[Any], handler: Any) -> None:
        del event_class, handler

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class BlockingAgentWorker(AgentWorker):
    def __init__(self, context: Any, agent_def: FakeAgentDef) -> None:
        super().__init__(context)
        self.agent_def = agent_def
        self.started: list[str] = []
        self.active_count = 0
        self.max_active_count = 0
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()

    def _route_agent_def(self, event: DispatchEvent) -> FakeAgentDef:
        del event
        return self.agent_def

    def _get_or_create_session(
        self,
        event: DispatchEvent,
        agent_def: FakeAgentDef,
    ) -> Any:
        del event, agent_def
        return SimpleNamespace(state=SimpleNamespace(messages=[]))

    async def _run_command_or_chat(self, content: str, session: Any) -> str:
        del session
        self.started.append(content)
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        if len(self.started) == 1:
            self.first_started.set()
        try:
            await self.release.wait()
        finally:
            self.active_count -= 1
        return f"done:{content}"


def make_context() -> Any:
    return SimpleNamespace(eventbus=FakeEventBus())


def make_dispatch(session_id: str, content: str) -> DispatchEvent:
    return DispatchEvent(
        session_id=session_id,
        content=content,
        source=AgentEventSource("caller"),
        target_agent_id="worker",
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_agent_def_parses_max_concurrency() -> None:
    loader = AgentLoader(
        SimpleNamespace(
            llm=LLMConfig(
                provider="openai",
                model="gpt-test",
                api_key="sk-test",
            ),
            agents_path=Path("unused"),
        )
    )

    agent_def = loader._parse_agent_def(
        "worker",
        {"name": "Worker", "max_concurrency": 3},
        "Worker prompt.",
    )
    default_def = loader._parse_agent_def(
        "default",
        {"name": "Default"},
        "Default prompt.",
    )

    assert agent_def.max_concurrency == 3
    assert default_def.max_concurrency == 1


def test_agent_def_rejects_invalid_max_concurrency() -> None:
    loader = AgentLoader(
        SimpleNamespace(
            llm=LLMConfig(
                provider="openai",
                model="gpt-test",
                api_key="sk-test",
            ),
            agents_path=Path("unused"),
        )
    )

    with pytest.raises(InvalidDefError):
        loader._parse_agent_def(
            "worker",
            {"name": "Worker", "max_concurrency": 0},
            "Worker prompt.",
        )


def test_agent_worker_blocks_above_agent_max_concurrency() -> None:
    async def scenario() -> None:
        worker = BlockingAgentWorker(make_context(), FakeAgentDef("worker", 1))

        first = asyncio.create_task(worker.exec_session(make_dispatch("s1", "one")))
        await asyncio.wait_for(worker.first_started.wait(), timeout=1)

        second = asyncio.create_task(worker.exec_session(make_dispatch("s2", "two")))
        await asyncio.sleep(0)

        assert worker.started == ["one"]
        worker.release.set()
        await asyncio.gather(first, second)

        assert worker.started == ["one", "two"]
        assert worker.max_active_count == 1
        assert [
            event.content
            for event in worker.context.eventbus.published
            if isinstance(event, DispatchResultEvent)
        ] == ["done:one", "done:two"]

    asyncio.run(scenario())


def test_agent_worker_allows_work_up_to_agent_max_concurrency() -> None:
    async def scenario() -> None:
        worker = BlockingAgentWorker(make_context(), FakeAgentDef("worker", 2))

        first = asyncio.create_task(worker.exec_session(make_dispatch("s1", "one")))
        await asyncio.wait_for(worker.first_started.wait(), timeout=1)
        second = asyncio.create_task(worker.exec_session(make_dispatch("s2", "two")))

        await wait_until(lambda: len(worker.started) == 2)

        assert worker.max_active_count == 2
        worker.release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_clear_sessions_also_resets_agent_semaphores() -> None:
    async def scenario() -> None:
        worker = BlockingAgentWorker(make_context(), FakeAgentDef("worker", 1))

        first_semaphore = worker._get_or_create_semaphore(worker.agent_def)
        worker.agent_def = FakeAgentDef("worker", 2)
        worker.clear_sessions()
        second_semaphore = worker._get_or_create_semaphore(worker.agent_def)

        assert first_semaphore is not second_semaphore
        await second_semaphore.acquire()
        assert second_semaphore.locked() is False
        await second_semaphore.acquire()
        assert second_semaphore.locked() is True
        second_semaphore.release()
        second_semaphore.release()

    asyncio.run(scenario())
