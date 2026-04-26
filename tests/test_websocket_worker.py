"""Tests for WebSocket event bridge behavior."""

import asyncio
import sys

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from fastapi.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.events import (
    EventSource,
    InboundEvent,
    OutboundEvent,
    WebSocketEventSource,
)
from core.eventbus import EventBus
from core.routing import RoutingTable
from server.websocket_worker import WebSocketMessage, WebSocketWorker
from utils.config import SourceSessionConfig, WebSocketConfig


class FakeEventBus:
    def __init__(self) -> None:
        self.subscriptions: list[type[Any]] = []
        self.published: list[Any] = []
        self.acked: list[Any] = []
        self.on_publish: Any = None

    def subscribe(self, event_class: type[Any], handler: Any) -> None:
        del handler
        self.subscriptions.append(event_class)

    def unsubscribe(self, handler: Any) -> None:
        del handler

    async def publish(self, event: Any) -> None:
        self.published.append(event)
        if self.on_publish is not None:
            await self.on_publish(event)

    def ack(self, event: Any) -> None:
        self.acked.append(event)


class FakeHistoryStore:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    def create_session(
        self,
        agent_id: str,
        session_id: str,
        source: EventSource | None = None,
    ) -> dict[str, Any]:
        self.created.append((agent_id, session_id, str(source)))
        return {}

    def list_sessions(self) -> list[Any]:
        return []


class FakeContext:
    def __init__(self) -> None:
        self.runtime: dict[str, Any] = {}
        self.config = FakeConfig(self.runtime)
        self.history_store = FakeHistoryStore()
        self.eventbus = FakeEventBus()
        self.channels = []
        self.websocket_worker = None
        self.routing_table = RoutingTable(self)


class FakeConfig:
    def __init__(self, runtime: dict[str, Any]) -> None:
        self.websocket = WebSocketConfig()
        self.sources: dict[str, SourceSessionConfig] = {}
        self._runtime = runtime

    def set_runtime(self, key: str, value: Any) -> None:
        self._runtime[key] = value

    def set_runtime_source(self, source: str, value: SourceSessionConfig) -> None:
        self.sources[source] = value
        self._runtime.setdefault("sources", {})[source] = value


class FakeWebSocket:
    def __init__(self, incoming: list[Any] | None = None) -> None:
        self.sent: list[Any] = []
        self.incoming = list(incoming or [])

    async def send_json(self, payload: Any) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> Any:
        if not self.incoming:
            raise WebSocketDisconnect
        return self.incoming.pop(0)


def test_websocket_event_source_round_trips() -> None:
    source = WebSocketEventSource(client_id="client-1", conversation_id="chat-1")

    parsed = EventSource.from_string(str(source))

    assert isinstance(parsed, WebSocketEventSource)
    assert parsed.client_id == "client-1"
    assert parsed.conversation_id == "chat-1"
    assert parsed.platform_name == "ws"


def test_websocket_message_normalizes_to_inbound_event() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = WebSocketWorker(context)
        msg = WebSocketMessage(
            source="client-1/chat-1",
            content="hello",
        )

        event = await worker._normalize_message(msg)

        assert isinstance(event, InboundEvent)
        assert event.content == "hello"
        assert isinstance(event.source, WebSocketEventSource)
        assert event.source.client_id == "client-1"
        assert event.source.conversation_id == "chat-1"
        assert str(event.source) == "platform-ws:client-1/chat-1"
        assert event.request_id
        assert context.config.sources[str(event.source)].session_id == event.session_id
        assert context.history_store.created == []

    asyncio.run(scenario())


def test_websocket_message_reuses_existing_source_session() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = WebSocketWorker(context)
        source_key = "platform-ws:client-1"
        context.config.sources[source_key] = SourceSessionConfig(
            session_id="session-1",
        )
        msg = WebSocketMessage(
            source="client-1",
            content="hello",
        )

        event = await worker._normalize_message(msg)

        assert event.session_id == "session-1"
        assert context.runtime == {}

    asyncio.run(scenario())


def test_websocket_message_rejects_client_session_id() -> None:
    with pytest.raises(ValidationError):
        WebSocketMessage.model_validate(
            {
                "source": "client-1",
                "content": "hello",
                "session_id": "session-1",
            }
        )


def test_websocket_worker_sends_matching_outbound_events() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = WebSocketWorker(context)
        matching = FakeWebSocket()
        other = FakeWebSocket()
        source = WebSocketEventSource(client_id="client-1")
        worker.clients[matching] = {str(source)}
        worker.clients[other] = {"platform-ws:other"}

        await worker.handle_event(
            OutboundEvent(
                session_id="session-1",
                content="hi",
                source=source,
                request_id="request-1",
            )
        )

        assert len(matching.sent) == 1
        assert matching.sent[0]["direction"] == "outbound"
        assert matching.sent[0]["event"]["content"] == "hi"
        assert other.sent == []

    asyncio.run(scenario())


def test_websocket_connection_keeps_multiple_source_subscriptions() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = WebSocketWorker(context)
        ws = FakeWebSocket()
        source_a = WebSocketEventSource(
            client_id="client-1",
            conversation_id="conversation-1",
        )
        source_b = WebSocketEventSource(
            client_id="client-1",
            conversation_id="conversation-2",
        )
        worker.clients[ws] = set()

        await worker._subscribe_client(ws, str(source_a))
        await worker._subscribe_client(ws, str(source_b))
        await worker.handle_event(
            OutboundEvent(
                session_id="session-a",
                content="late reply for A",
                source=source_a,
                request_id="request-a",
            )
        )

        assert worker.clients[ws] == {str(source_a), str(source_b)}
        assert len(ws.sent) == 1
        assert ws.sent[0]["event"]["content"] == "late reply for A"

    asyncio.run(scenario())


def test_websocket_accepts_message_before_publishing_event() -> None:
    async def scenario() -> None:
        context = FakeContext()
        worker = WebSocketWorker(context)
        ws = FakeWebSocket(
            incoming=[
                {
                    "source": "client-1",
                    "content": "hello",
                }
            ]
        )
        context.eventbus.on_publish = worker.handle_event

        try:
            await worker._run_client_loop(ws)
        except WebSocketDisconnect:
            pass

        assert [payload["type"] for payload in ws.sent] == ["accepted", "event"]
        assert ws.sent[1]["direction"] == "inbound"

    asyncio.run(scenario())


def test_websocket_outbound_event_is_not_outbox_persisted() -> None:
    event = OutboundEvent(
        session_id="session-1",
        content="hi",
        source=WebSocketEventSource(client_id="client-1"),
    )

    assert EventBus._should_persist(event) is False
