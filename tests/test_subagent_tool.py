"""Tests for subagent dispatch tool."""

import asyncio
import json
import sys

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.events import AgentEventSource, DispatchEvent, DispatchResultEvent
from server.agent_worker import AgentWorker
from tools.subagent_tool import create_subagent_dispatch_tool
import tools.subagent_tool as subagent_module


@dataclass
class FakeAgentDef:
    id: str
    description: str = ""


class FakeAgentLoader:
    def __init__(self, agents: list[FakeAgentDef]) -> None:
        self.agents = agents
        self.loaded: list[str] = []

    def discover_agents(self) -> list[FakeAgentDef]:
        return list(self.agents)

    def load(self, agent_id: str) -> FakeAgentDef:
        self.loaded.append(agent_id)
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise AssertionError(f"unexpected agent load: {agent_id}")


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []
        self.subscribers: dict[type[Any], list[Any]] = {}
        self.unsubscribed: list[Any] = []

    def subscribe(self, event_class: type[Any], handler: Any) -> None:
        self.subscribers.setdefault(event_class, []).append(handler)

    def unsubscribe(self, handler: Any) -> None:
        self.unsubscribed.append(handler)
        for event_class, handlers in list(self.subscribers.items()):
            self.subscribers[event_class] = [
                registered
                for registered in handlers
                if registered != handler
            ]
            if not self.subscribers[event_class]:
                del self.subscribers[event_class]

    async def publish(self, event: Any) -> None:
        self.published.append(event)
        if not isinstance(event, DispatchEvent):
            return

        result = DispatchResultEvent(
            session_id=event.session_id,
            content="worker result",
            source=event.source,
            request_id=event.request_id,
        )
        for handler in list(self.subscribers.get(DispatchResultEvent, [])):
            await handler(result)


def make_context(agents: list[FakeAgentDef]) -> Any:
    return SimpleNamespace(
        agent_loader=FakeAgentLoader(agents),
        eventbus=FakeEventBus(),
    )


def test_subagent_dispatch_tool_is_not_created_without_other_agents() -> None:
    context = make_context([FakeAgentDef(id="main")])

    assert create_subagent_dispatch_tool("main", context) is None


def test_subagent_dispatch_tool_schema_excludes_current_agent() -> None:
    context = make_context(
        [
            FakeAgentDef(id="main", description="Current agent"),
            FakeAgentDef(id="worker", description="Handles focused work"),
        ]
    )

    dispatch_tool = create_subagent_dispatch_tool("main", context)

    assert dispatch_tool is not None
    assert dispatch_tool.name == "subagent_dispatch"
    assert 'agent id="worker"' in dispatch_tool.description
    assert 'agent id="main"' not in dispatch_tool.description
    agent_id_schema = dispatch_tool.parameters["properties"]["agent_id"]
    assert agent_id_schema["enum"] == ["worker"]


def test_subagent_dispatch_publishes_parented_dispatch_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            subagent_module.uuid,
            "uuid4",
            lambda: SimpleNamespace(hex="dispatch-session"),
        )
        context = make_context(
            [
                FakeAgentDef(id="main", description="Current agent"),
                FakeAgentDef(id="worker", description="Handles focused work"),
            ]
        )
        dispatch_tool = create_subagent_dispatch_tool("main", context)
        assert dispatch_tool is not None
        parent_session = SimpleNamespace(session_id="parent-session")

        result = await dispatch_tool.execute(
            session=parent_session,
            agent_id="worker",
            task="Do the work",
            context="Extra notes",
        )

        assert json.loads(result) == {
            "result": "worker result",
            "session_id": "dispatch-session",
        }
        assert context.agent_loader.loaded == []
        assert len(context.eventbus.published) == 1
        event = context.eventbus.published[0]
        assert isinstance(event, DispatchEvent)
        assert event.session_id == "dispatch-session"
        assert event.target_agent_id == "worker"
        assert event.parent_session_id == "parent-session"
        assert event.content == "Do the work\n\nContext:\nExtra notes"
        assert str(event.source) == "agent:main"
        assert context.eventbus.unsubscribed
        assert DispatchResultEvent not in context.eventbus.subscribers

    asyncio.run(scenario())


def test_agent_worker_routes_new_dispatch_by_target_agent_id() -> None:
    worker_agent = FakeAgentDef(id="worker", description="Handles focused work")
    context = SimpleNamespace(
        eventbus=FakeEventBus(),
        history_store=SimpleNamespace(get_session_info=lambda session_id: None),
        agent_loader=FakeAgentLoader([worker_agent]),
        routing_table=SimpleNamespace(resolve=lambda source: "main"),
    )
    worker = AgentWorker(context)

    agent_def = worker._route_agent_def(
        DispatchEvent(
            session_id="dispatch-session",
            content="Do the work",
            source=AgentEventSource("main"),
            target_agent_id="worker",
        )
    )

    assert agent_def is worker_agent
    assert context.agent_loader.loaded == ["worker"]
