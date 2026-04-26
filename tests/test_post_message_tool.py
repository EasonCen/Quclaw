"""Tests for the proactive post_message tool."""

import asyncio
import sys

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.agent import Agent
from core.agent_loader import AgentDef
from core.events import (
    AgentEventSource,
    CronEventSource,
    EventSource,
    OutboundEvent,
)
from tools.post_message_tool import create_post_message_tool
from utils.config import LLMConfig


@dataclass
class StubEventSource(EventSource):
    """Platform source used by post_message tests."""

    _namespace = "platform-test"
    target_id: str = "target"

    def __str__(self) -> str:
        return f"{self._namespace}:{self.target_id}"

    @classmethod
    def from_string(cls, s: str) -> "StubEventSource":
        _, target_id = s.split(":", 1)
        return cls(target_id=target_id)

    @property
    def platform_name(self) -> str:
        return "test"


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class FakeHistoryStore:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    def create_session(
        self,
        agent_id: str,
        session_id: str,
        source: EventSource,
    ) -> dict[str, Any]:
        self.created.append((agent_id, session_id, str(source)))
        return {}


class FakeAgentLoader:
    def discover_agents(self) -> list[Any]:
        return []


class FakeContext:
    def __init__(
        self,
        *,
        channels_enabled: bool = True,
        default_delivery_source: str | None = "platform-test:target",
    ) -> None:
        self.channels = []
        self.config = SimpleNamespace(
            channels=SimpleNamespace(enabled=channels_enabled),
            default_delivery_source=default_delivery_source,
            webread=None,
            websearch=None,
            context=SimpleNamespace(token_threshold=100000),
        )
        self.eventbus = FakeEventBus()
        self.history_store = FakeHistoryStore()
        self.skill_loader = SimpleNamespace()
        self.agent_loader = FakeAgentLoader()


def test_post_message_tool_is_not_created_when_channels_disabled() -> None:
    context = FakeContext(channels_enabled=False)

    assert create_post_message_tool(context) is None


def test_agent_registers_post_message_only_when_requested() -> None:
    context = FakeContext()
    agent = Agent(
        AgentDef(
            id="Qu",
            name="Qu",
            agent_md="You are Qu.",
            llm=LLMConfig(
                provider="openai",
                model="gpt-test",
                api_key="sk-test",
            ),
        ),
        context,
    )

    assert (
        agent._build_tools(include_post_message=True).get("post_message")
        is not None
    )
    assert agent._build_tools(include_post_message=False).get("post_message") is None


def test_agent_sessions_register_post_message_only_for_cron_sources() -> None:
    context = FakeContext()
    agent = Agent(
        AgentDef(
            id="Qu",
            name="Qu",
            agent_md="You are Qu.",
            llm=LLMConfig(
                provider="openai",
                model="gpt-test",
                api_key="sk-test",
            ),
        ),
        context,
    )

    cron_session = agent.new_session(CronEventSource("daily-check"), "cron-session")
    agent_session = agent.new_session(AgentEventSource("Qu"), "agent-session")

    assert cron_session.tools.get("post_message") is not None
    assert agent_session.tools.get("post_message") is None


def test_post_message_queues_outbound_event_for_cron_session() -> None:
    async def scenario() -> None:
        context = FakeContext()
        tool = create_post_message_tool(context)
        assert tool is not None
        session = SimpleNamespace(
            session_id="cron-session",
            source=CronEventSource("daily-check"),
        )

        result = await tool.execute(session=session, content=" hello ")

        assert result == "Queued outbound message for DeliveryWorker."
        assert len(context.eventbus.published) == 1
        event = context.eventbus.published[0]
        assert isinstance(event, OutboundEvent)
        assert event.session_id == "cron-session"
        assert event.content == "hello"
        assert str(event.source) == "platform-test:target"

    asyncio.run(scenario())


def test_post_message_rejects_non_cron_session() -> None:
    async def scenario() -> None:
        context = FakeContext()
        tool = create_post_message_tool(context)
        assert tool is not None
        session = SimpleNamespace(
            session_id="agent-session",
            source=AgentEventSource("Qu"),
        )

        result = await tool.execute(session=session, content="hello")

        assert result == "Error: post_message can only be used by cron jobs."
        assert context.eventbus.published == []

    asyncio.run(scenario())


def test_post_message_requires_default_delivery_source() -> None:
    async def scenario() -> None:
        context = FakeContext(default_delivery_source=None)
        tool = create_post_message_tool(context)
        assert tool is not None
        session = SimpleNamespace(
            session_id="cron-session",
            source=CronEventSource("daily-check"),
        )

        result = await tool.execute(session=session, content="hello")

        assert result == "Error: no default delivery source is configured."
        assert context.eventbus.published == []

    asyncio.run(scenario())
