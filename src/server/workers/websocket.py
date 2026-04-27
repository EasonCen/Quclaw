"""WebSocket worker for programmatic access to the event bus."""

import asyncio
import time
import uuid

from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .base import Worker
from runtime.events import (
    Event,
    EventSource,
    InboundEvent,
    OutboundEvent,
    WebSocketEventSource,
)

if TYPE_CHECKING:
    from core.context import SharedContext


class WebSocketMessage(BaseModel):
    """Incoming WebSocket message from a client."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1, description="Client source identifier")
    content: str = Field(..., min_length=1, description="Message content")


class WebSocketWorker(Worker):
    """Hosts the WebSocket endpoint and bridges messages through EventBus."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.clients: dict[WebSocket, set[str]] = {}
        self._clients_lock = asyncio.Lock()
        self._source_session_lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None

        self.context.websocket_worker = self
        self.context.eventbus.subscribe(InboundEvent, self.handle_event)
        self.context.eventbus.subscribe(OutboundEvent, self.handle_event)
        self.logger.info("WebSocketWorker subscribed to EventBus events")

    async def run(self) -> None:
        """Run the FastAPI/uvicorn server for WebSocket clients."""
        from server.app import create_app

        websocket_config = self.context.config.websocket
        app = create_app(self)
        config = uvicorn.Config(
            app,
            host=websocket_config.host,
            port=websocket_config.port,
            log_config=None,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def handle_connection(self, ws: WebSocket) -> None:
        """Handle a single WebSocket connection lifecycle."""
        if not self._is_authorized(ws):
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await ws.accept()
        async with self._clients_lock:
            self.clients[ws] = set()

        try:
            await self._run_client_loop(ws)
        except WebSocketDisconnect:
            self.logger.debug("WebSocket client disconnected")
        finally:
            async with self._clients_lock:
                self.clients.pop(ws, None)

    async def _run_client_loop(self, ws: WebSocket) -> None:
        """Receive client messages, normalize them, and publish inbound events."""
        while True:
            try:
                payload = await ws.receive_json()
                msg = WebSocketMessage.model_validate(payload)
                event = await self._normalize_message(msg)
            except ValidationError as exc:
                await self._send_error(ws, "invalid_message", exc.errors())
                continue
            except ValueError as exc:
                await self._send_error(ws, "invalid_message", str(exc))
                continue

            await self._subscribe_client(ws, str(event.source))

            await ws.send_json(
                {
                    "type": "accepted",
                    "request_id": event.request_id,
                    "session_id": event.session_id,
                    "source": str(event.source),
                    "timestamp": event.timestamp,
                }
            )
            await self.context.eventbus.publish(event)

    async def _normalize_message(self, msg: WebSocketMessage) -> InboundEvent:
        """Normalize a WebSocket message into an InboundEvent."""
        source = WebSocketEventSource.from_string(f"platform-ws:{msg.source}")
        session_id = await self._get_or_create_session_id(source)

        return InboundEvent(
            session_id=session_id,
            content=msg.content,
            source=source,
            request_id=uuid.uuid4().hex,
            timestamp=time.time(),
        )

    async def handle_event(self, event: Event) -> None:
        """Broadcast matching EventBus events to connected WebSocket clients."""
        if not isinstance(event.source, WebSocketEventSource):
            return

        source_key = str(event.source)
        payload = {
            "type": "event",
            "event": event.to_dict(),
            "direction": self._event_direction(event),
        }

        async with self._clients_lock:
            targets = [
                ws
                for ws, client_sources in self.clients.items()
                if source_key in client_sources
            ]

        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                self.logger.exception("Failed to send WebSocket event")
                async with self._clients_lock:
                    self.clients.pop(ws, None)

    async def stop(self) -> None:
        """Stop the WebSocket server and close active clients."""
        self.context.eventbus.unsubscribe(self.handle_event)
        if self.context.websocket_worker is self:
            self.context.websocket_worker = None

        if self._server is not None:
            self._server.should_exit = True

        async with self._clients_lock:
            clients = list(self.clients)
            self.clients.clear()

        for ws in clients:
            try:
                await ws.close()
            except Exception:
                self.logger.debug("Failed to close WebSocket client", exc_info=True)

        await super().stop()

    async def _get_or_create_session_id(
        self,
        source: EventSource,
    ) -> str:
        """Get or create session affinity for a WebSocket source."""
        async with self._source_session_lock:
            return self.context.routing_table.get_or_create_session_id(source)

    async def _subscribe_client(self, ws: WebSocket, source_key: str) -> None:
        """Subscribe a connection to a source without dropping earlier sources."""
        async with self._clients_lock:
            self.clients.setdefault(ws, set()).add(source_key)

    def _is_authorized(self, ws: WebSocket) -> bool:
        """Check optional token auth for a WebSocket connection."""
        auth_token = self.context.config.websocket.auth_token
        if auth_token is None:
            return True

        query_token = ws.query_params.get("token")
        if query_token == auth_token:
            return True

        header = ws.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and token == auth_token

    @staticmethod
    def _event_direction(event: Event) -> str:
        if isinstance(event, InboundEvent):
            return "inbound"
        if isinstance(event, OutboundEvent):
            return "outbound"
        return "unknown"

    @staticmethod
    async def _send_error(ws: WebSocket, code: str, detail: Any) -> None:
        await ws.send_json(
            {
                "type": "error",
                "code": code,
                "detail": detail,
            }
        )
