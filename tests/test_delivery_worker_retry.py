"""Tests for DeliveryWorker outbox retry behavior."""

import asyncio
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from channel.base import Channel
from core.events import (
    AgentEventSource,
    CronEventSource,
    EventSource,
    OutboundEvent,
    WebSocketEventSource,
    deserialize_event,
    serialize_event,
)
from server import delivery_worker as delivery_module
from server.delivery_worker import DeliveryWorker, MAX_RETRY


@dataclass
class StubEventSource(EventSource):
    """Platform source used by delivery retry tests."""

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
        self.acked: list[Any] = []
        self._published_event = asyncio.Event()

    def subscribe(self, event_class: type[Any], handler: Any) -> None:
        del event_class, handler

    async def publish(self, event: Any) -> None:
        self.published.append(event)
        self._published_event.set()

    def ack(self, event: Any) -> None:
        self.acked.append(event)

    async def wait_for_publish(self) -> Any:
        await asyncio.wait_for(self._published_event.wait(), timeout=1)
        return self.published[-1]


class FakeHistorySession:
    def __init__(self, session_id: str, source: EventSource) -> None:
        self.id = session_id
        self.source = str(source)
        self._source = source

    def try_get_source(self) -> EventSource:
        return self._source


class FakeHistoryStore:
    def __init__(self, sessions: list[Any] | None = None) -> None:
        self.sessions = sessions or []

    def list_sessions(self) -> list[Any]:
        return self.sessions


class FakeContext:
    def __init__(
        self,
        channels: list[Channel[Any]] | None = None,
        sessions: list[Any] | None = None,
    ) -> None:
        self.channels = channels or []
        self.eventbus = FakeEventBus()
        self.history_store = FakeHistoryStore(sessions)


class FailingChannel(Channel[StubEventSource]):
    def __init__(self) -> None:
        self.reply_count = 0

    @property
    def platform_name(self) -> str:
        return "test"

    async def run(self, on_message: Any) -> None:
        del on_message

    async def reply(self, content: str, source: StubEventSource) -> None:
        del content, source
        self.reply_count += 1
        raise RuntimeError("platform unavailable")

    async def is_allowed(self, source: StubEventSource) -> bool:
        del source
        return True

    async def stop(self) -> None:
        pass


class RecordingChannel(Channel[StubEventSource]):
    def __init__(
        self,
        *,
        allowed: bool = True,
        allow_error: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.allow_error = allow_error
        self.allow_checked: list[EventSource] = []
        self.delivered: list[tuple[str, EventSource]] = []

    @property
    def platform_name(self) -> str:
        return "test"

    async def run(self, on_message: Any) -> None:
        del on_message

    async def reply(self, content: str, source: StubEventSource) -> None:
        self.delivered.append((content, source))

    async def is_allowed(self, source: StubEventSource) -> bool:
        self.allow_checked.append(source)
        if self.allow_error is not None:
            raise self.allow_error
        return self.allowed

    async def stop(self) -> None:
        pass


def make_outbound_event() -> OutboundEvent:
    return OutboundEvent(
        session_id="session-1",
        request_id="request-1",
        content="hello",
        source=StubEventSource(),
    )


def test_outbound_retry_count_round_trips_through_serialization() -> None:
    event = make_outbound_event()
    event.retry_count = 3

    restored = deserialize_event(serialize_event(event))

    assert isinstance(restored, OutboundEvent)
    assert restored.retry_count == 3
    assert restored.request_id == event.request_id


def test_platform_event_source_takes_precedence_over_session_metadata() -> None:
    async def scenario() -> None:
        channel = RecordingChannel()
        context = FakeContext(
            channels=[channel],
            sessions=[
                FakeHistorySession(
                    "cron-session",
                    CronEventSource("daily-check"),
                )
            ],
        )
        worker = DeliveryWorker(context)
        event = OutboundEvent(
            session_id="cron-session",
            request_id="request-1",
            content="hello",
            source=StubEventSource(),
        )

        await worker.deliver(event)

        assert len(channel.delivered) == 1
        assert channel.delivered[0][0] == "hello"
        assert isinstance(channel.delivered[0][1], StubEventSource)
        assert context.eventbus.acked == [event]
        await worker.stop()

    asyncio.run(scenario())


def test_non_platform_outbound_event_is_acked_without_retry() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = DeliveryWorker(context)
        event = OutboundEvent(
            session_id="session-1",
            request_id="request-1",
            content="hello",
            source=AgentEventSource("Qu"),
        )

        await worker.deliver(event)

        assert context.eventbus.acked == [event]
        assert context.eventbus.published == []
        await worker.stop()

    asyncio.run(scenario())


def test_websocket_outbound_event_is_acked_without_retry() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = DeliveryWorker(context)
        event = OutboundEvent(
            session_id="session-1",
            request_id="request-1",
            content="hello",
            source=WebSocketEventSource(client_id="client-1"),
        )

        await worker.deliver(event)

        assert context.eventbus.acked == [event]
        assert context.eventbus.published == []
        await worker.stop()

    asyncio.run(scenario())


def test_non_whitelisted_delivery_is_acked_without_reply() -> None:
    async def scenario() -> None:
        channel = RecordingChannel(allowed=False)
        context = FakeContext(channels=[channel])
        worker = DeliveryWorker(context)
        event = make_outbound_event()

        await worker.deliver(event)

        assert len(channel.allow_checked) == 1
        assert channel.delivered == []
        assert context.eventbus.acked == [event]
        assert context.eventbus.published == []
        await worker.stop()

    asyncio.run(scenario())


def test_allowlist_error_is_acked_without_reply() -> None:
    async def scenario() -> None:
        channel = RecordingChannel(allow_error=RuntimeError("bad allowlist"))
        context = FakeContext(channels=[channel])
        worker = DeliveryWorker(context)
        event = make_outbound_event()

        await worker.deliver(event)

        assert len(channel.allow_checked) == 1
        assert channel.delivered == []
        assert context.eventbus.acked == [event]
        assert context.eventbus.published == []
        await worker.stop()

    asyncio.run(scenario())


def test_missing_channel_schedules_outbox_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(delivery_module, "compute_backoff_ms", lambda count: 0)
        context = FakeContext()
        worker = DeliveryWorker(context)

        await worker.deliver(make_outbound_event())
        retry_event = await context.eventbus.wait_for_publish()

        assert retry_event.retry_count == 1
        assert retry_event.request_id == "request-1"
        assert context.eventbus.acked == []
        await worker.stop()

    asyncio.run(scenario())


def test_delivery_failure_schedules_retry_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(delivery_module, "compute_backoff_ms", lambda count: 0)
        channel = FailingChannel()
        context = FakeContext(channels=[channel])
        worker = DeliveryWorker(context)

        await worker.deliver(make_outbound_event())
        retry_event = await context.eventbus.wait_for_publish()

        assert channel.reply_count == MAX_RETRY
        assert retry_event.retry_count == 1
        assert context.eventbus.acked == []
        await worker.stop()

    asyncio.run(scenario())


def test_channel_reload_flushes_pending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(delivery_module, "compute_backoff_ms", lambda count: 60_000)
        context = FakeContext()
        worker = DeliveryWorker(context)

        await worker.deliver(make_outbound_event())
        await asyncio.sleep(0)
        assert context.eventbus.published == []

        worker.reload_channels([FailingChannel()])
        retry_event = await context.eventbus.wait_for_publish()

        assert retry_event.retry_count == 1
        assert len(context.eventbus.published) == 1
        await worker.stop()

    asyncio.run(scenario())
