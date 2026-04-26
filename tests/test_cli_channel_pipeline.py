"""Tests for routing CLI through the shared channel pipeline."""

import asyncio
import sys

from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.events import CliEventSource, EventSource, InboundEvent
from server.channel_worker import ChannelWorker
from utils.config import SourceSessionConfig


class FakeEventBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class FakeConfig:
    def __init__(self) -> None:
        self.sources: dict[str, SourceSessionConfig] = {}
        self.default_delivery_source: str | None = None
        self.runtime: dict[str, Any] = {}

    def set_runtime(self, key: str, value: Any) -> None:
        self.runtime[key] = value


class FakeContext:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.eventbus = FakeEventBus()


class FakeCliChannel:
    platform_name = "cli"

    async def is_allowed(self, source: CliEventSource) -> bool:
        return True


def test_cli_event_source_round_trips_with_conversation_id() -> None:
    source = CliEventSource(conversation_id="session-1")

    parsed = EventSource.from_string(str(source))

    assert isinstance(parsed, CliEventSource)
    assert str(parsed) == "platform-cli:cli-user/session-1"


def test_channel_worker_publishes_cli_inbound_event() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = ChannelWorker(context)
        worker.channel_map = {"cli": FakeCliChannel()}
        callback = worker._create_callback("cli")
        source = CliEventSource(conversation_id="session-1")

        await callback("hello", source)

        assert len(context.eventbus.events) == 1
        event = context.eventbus.events[0]
        assert isinstance(event, InboundEvent)
        assert event.content == "hello"
        assert str(event.source) == str(source)
        assert str(source) in context.config.sources
        assert context.config.default_delivery_source is None

    asyncio.run(scenario())
