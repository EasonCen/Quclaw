"""Central event bus for pub/sub event distribution."""

import asyncio
import hashlib
import json
import logging
import uuid

from collections import defaultdict
from pathlib import Path
from typing import Awaitable, Callable, TypeVar, TYPE_CHECKING

from server.worker import Worker

from .events import (
    Event,
    OutboundEvent,
    deserialize_event,
    serialize_event,
)

if TYPE_CHECKING:
    from core.context import SharedContext

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=Event)
Handler = Callable[..., Awaitable[None]]


class EventBus(Worker):
    """Central event bus with subscription support and async dispatch."""

    def __init__(self, context: "SharedContext") -> None:
        super().__init__(context)
        self.context = context
        self._subscribers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._persist_lock = asyncio.Lock()

    # 例如：
    # AgentWorker 订阅 InboundEvent
    # CLI 的 response handler 订阅 OutboundEvent
    def subscribe(
        self,
        event_class: type[E],
        handler: Callable[[E], Awaitable[None]],
    ) -> None:
        """Subscribe a handler to an event class."""
        self._subscribers[event_class].append(handler)
        logger.debug("Subscribed handler to %s events", event_class.__name__)

    def unsubscribe(self, handler: Handler) -> None:
        """Remove a handler from all subscriptions."""
        for event_class, handlers in list(self._subscribers.items()):
            self._subscribers[event_class] = [
                registered
                for registered in handlers
                if registered != handler
            ]
            if not self._subscribers[event_class]:
                del self._subscribers[event_class]

    async def publish(self, event: Event) -> None:
        """Publish an event to the internal queue."""
        await self._persist_outbound(event)
        await self._queue.put(event)

    async def run(self) -> None:
        """Process events from the queue until cancelled."""
        try:
            recovered_count = await self._recover()
            if recovered_count:
                logger.info("Recovered %s outbound event(s)", recovered_count)

            while True:
                event = await self._queue.get()
                try:
                    await self._dispatch(event)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, event: Event) -> None:
        """Dispatch an event to subscribers."""
        await self._notify_subscribers(event)
        logger.debug(
            "Dispatched %s event from %s",
            event.__class__.__name__,
            event.source,
        )

    async def _notify_subscribers(self, event: Event) -> None:
        """Notify all subscribers of an event (waits for all handlers to complete)."""
        handlers = [
            handler
            for event_class, subscribers in self._subscribers.items()
            if isinstance(event, event_class)
            for handler in subscribers
        ]
        if not handlers:
            logger.debug("No subscribers for %s", event.__class__.__name__)
            return

        tasks = [handler(event) for handler in handlers]
        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.exception(
                    "Event handler failed",
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _persist_outbound(self, event: Event) -> None:
        """Persist outbound platform events before delivery."""
        if not self._should_persist(event):
            return

        event_path = self._event_path(event)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = event_path.with_name(f"{event_path.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "event": serialize_event(event),
        }

        async with self._persist_lock:
            with temp_path.open("w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.write("\n")
            temp_path.replace(event_path)

    async def _recover(self) -> int:
        """Recover pending outbound events from a previous crash."""
        outbound_path = self._outbound_path
        if not outbound_path.exists():
            return 0

        recovered_events: list[OutboundEvent] = []
        for event_path in sorted(outbound_path.glob("*.json")):
            try:
                with event_path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                event_data = payload.get("event", payload)
                event = deserialize_event(event_data)
                if not isinstance(event, OutboundEvent):
                    logger.warning("Ignored non-outbound event file: %s", event_path)
                    continue
                recovered_events.append(event)
            except Exception:
                logger.exception("Failed to recover outbound event from %s", event_path)

        recovered_events.sort(key=lambda event: event.timestamp)
        for event in recovered_events:
            await self._queue.put(event)

        return len(recovered_events)

    def ack(self, event: Event) -> None:
        """Acknowledge successful delivery, delete persisted event."""
        if not self._should_persist(event):
            return

        try:
            self._event_path(event).unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to acknowledge outbound event %s", event.request_id)

    @property
    def _outbound_path(self) -> Path:
        """Directory for pending outbound events."""
        return self.context.config.event_path / "outbound"

    def _event_path(self, event: Event) -> Path:
        """Return the stable persistence path for an outbound event."""
        event_key = f"{event.session_id}:{event.request_id}"
        event_hash = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        return self._outbound_path / f"{event_hash}.json"

    @staticmethod
    def _should_persist(event: Event) -> bool:
        """Return whether an event needs outbound delivery persistence."""
        if not isinstance(event, OutboundEvent):
            return False

        platform = event.source.platform_name
        return (
            event.source.is_platform
            and platform is not None
            and platform not in {"cli", "ws"}
        )
