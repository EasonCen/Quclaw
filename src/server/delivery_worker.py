"""Worker that delivers outbound messages to platforms."""

import asyncio
import logging
import random
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .worker import SubscriberWorker
from core.events import EventSource, OutboundEvent

if TYPE_CHECKING:
    from core.context import SharedContext
    from core.history import HistorySession
    from channel.base import Channel

logger = logging.getLogger(__name__)

# Retry configuration
BACKOFF_MS = [5000, 25000, 120000, 300000] # 5s, 25s, 2min, 5min
MAX_RETRY = 5


def compute_backoff_ms(retry_count: int) -> int:
    """Compute backoff time with jitter."""
    if retry_count <= 0:
        return 0

    # Cap at last backoff value
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]

    # Add +/- 20% jitter
    jitter = random.randint(-base // 5, base // 5)
    return max(0, base + jitter)


# Platform message size limits
PLATFORM_LIMITS: dict[str, int | None] = {
    "telegram": 4096,
    "discord": 2000,
    "cli": None,  # no limit
}


def chunk_message(content: str, limit: int) -> list[str]:
    """Split message at paragraph boundaries, respecting limit."""
    if len(content) <= limit:
        return [content]

    chunks = []
    paragraphs = content.split("\n\n")
    current = ""

    for para in paragraphs:
        # Try to add to current chunk
        if current:
            potential = current + "\n\n" + para
        else:
            potential = para

        if len(potential) <= limit:
            current = potential
        else:
            if current:
                chunks.append(current)

            # Handle paragraph that exceeds limit
            if len(para) > limit:
                # Hard split
                for i in range(0, len(para), limit):
                    chunks.append(para[i : i + limit])
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


class DeliveryWorker(SubscriberWorker):
    """Worker that delivers outbound messages to platforms."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.channel_map = {
            channel.platform_name: channel
            for channel in self.context.channels
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_events: dict[str, OutboundEvent] = {}
        self._session_cache: dict[str, "HistorySession"] = {}
        self.context.eventbus.subscribe(OutboundEvent, self.dispatch_event)
        self.logger.info("DeliveryWorker subscribed to OutboundEvent events")

    def reload_channels(self, channels: list["Channel[Any]"]) -> None:
        """Replace the delivery channel map after config hot reload."""
        self.channel_map = {
            channel.platform_name: channel
            for channel in channels
        }
        self._session_cache.clear()
        self._flush_pending_retries("channel reload")
        self.logger.info(
            "DeliveryWorker loaded %s channel(s): %s",
            len(self.channel_map),
            ", ".join(self.channel_map) or "none",
        )

    async def dispatch_event(self, event: OutboundEvent) -> None:
        """Create a background delivery task for an outbound event."""
        task = asyncio.create_task(
            self.deliver(event),
            name=f"delivery:{event.session_id}:{event.request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def deliver(self, event: OutboundEvent) -> None:
        """Deliver an outbound event to its original platform source."""
        try:
            source = self._resolve_delivery_source(event)
        except Exception:
            self.logger.exception(
                "Failed to resolve delivery source for session %s; "
                "using outbound event source",
                event.session_id,
            )
            source = event.source

        platform = source.platform_name

        if not source.is_platform or platform is None:
            self.logger.debug("Ignored non-platform outbound event from %s", source)
            return

        if platform == "ws":
            self.logger.debug("Ignored websocket outbound event from %s", source)
            return

        channel = self.channel_map.get(platform)
        if channel is None:
            self.logger.error("No channel configured for platform %s", platform)
            self._schedule_retry(
                event,
                source,
                reason=f"no channel configured for platform {platform}",
            )
            return

        content = event.content
        if event.error:
            content = f"Agent processing failed: {event.error}"

        limit = PLATFORM_LIMITS.get(platform)
        chunks = [content] if limit is None else chunk_message(content, limit)

        try:
            for chunk in chunks:
                await self._deliver_with_retry(channel, chunk, source)
            self.logger.debug("Delivered outbound event to %s", source)
            self._cancel_pending_retry(event)
            self.context.eventbus.ack(event)
        except Exception as exc:
            self.logger.exception("Failed to deliver outbound event to %s", source)
            self._schedule_retry(event, source, reason=str(exc))

    def _resolve_delivery_source(self, event: OutboundEvent) -> EventSource:
        """Resolve the delivery source from session metadata, falling back to event."""
        session = self._get_session_info(event.session_id)
        if session is None:
            self.logger.debug(
                "No history session found for outbound event %s",
                event.session_id,
            )
            return event.source

        source = session.try_get_source()
        if source is None:
            if session.source is None:
                self.logger.debug(
                    "No source bound to session %s; using outbound event source",
                    event.session_id,
                )
            else:
                self.logger.warning(
                    "Invalid source %s for session %s; using outbound event source",
                    session.source,
                    event.session_id,
                )
            return event.source

        return source

    def _get_session_info(self, session_id: str) -> "HistorySession | None":
        """Get session info from HistoryStore (cached)."""
        cached = self._session_cache.get(session_id)
        if cached is not None:
            return cached

        for session in self.context.history_store.list_sessions():
            if session.id == session_id:
                self._session_cache[session_id] = session
                return session
        return None

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Cleanup finished delivery tasks and log unexpected crashes."""
        self._tasks.discard(task)
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            self.logger.error(
                "Delivery task crashed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def stop(self) -> None:
        """Stop the worker and cancel in-flight deliveries."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        retry_tasks = list(self._retry_tasks.values())
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        self._retry_events.clear()

        await super().stop()

    def _schedule_retry(
        self,
        event: OutboundEvent,
        source: EventSource,
        reason: str,
    ) -> None:
        """Schedule a failed persisted outbound event for another delivery pass."""
        if not self._should_retry(event, source):
            self.logger.debug("No in-process retry scheduled for %s", source)
            return

        retry_event = replace(event, retry_count=event.retry_count + 1)
        delay_ms = compute_backoff_ms(retry_event.retry_count)
        self._enqueue_retry_event(retry_event, delay_ms, reason)

    def _enqueue_retry_event(
        self,
        event: OutboundEvent,
        delay_ms: int,
        reason: str,
    ) -> None:
        """Create a retry task for an outbound event."""
        key = self._retry_key(event)
        existing = self._retry_tasks.get(key)
        if existing is not None and not existing.done():
            self.logger.debug("Retry already scheduled for outbound event %s", key)
            return

        self._retry_events[key] = event
        task = asyncio.create_task(
            self._retry_after_delay(key, event, delay_ms),
            name=f"delivery-retry:{event.session_id}:{event.request_id}",
        )
        self._retry_tasks[key] = task
        task.add_done_callback(
            lambda finished, key=key: self._on_retry_done(key, finished)
        )
        self.logger.warning(
            "Scheduled outbound retry %s for %s in %sms: %s",
            event.retry_count,
            event.source,
            delay_ms,
            reason,
        )

    async def _retry_after_delay(
        self,
        key: str,
        event: OutboundEvent,
        delay_ms: int,
    ) -> None:
        """Re-publish an outbound event after a retry delay."""
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)

        if self._retry_events.get(key) is not event:
            return

        await self.context.eventbus.publish(event)

    def _on_retry_done(self, key: str, task: asyncio.Task[None]) -> None:
        """Cleanup finished retry tasks and log unexpected retry failures."""
        if self._retry_tasks.get(key) is task:
            del self._retry_tasks[key]
            self._retry_events.pop(key, None)

        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            self.logger.error(
                "Delivery retry task crashed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _flush_pending_retries(self, reason: str) -> None:
        """Immediately requeue delayed retries, used after channel reload."""
        retry_events = list(self._retry_events.values())
        if not retry_events:
            return

        for task in list(self._retry_tasks.values()):
            task.cancel()
        self._retry_tasks.clear()
        self._retry_events.clear()

        for event in retry_events:
            self._enqueue_retry_event(event, 0, reason)

    def _cancel_pending_retry(self, event: OutboundEvent) -> None:
        """Cancel a delayed retry for an event that has now been delivered."""
        key = self._retry_key(event)
        task = self._retry_tasks.pop(key, None)
        self._retry_events.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _should_retry(event: OutboundEvent, source: EventSource) -> bool:
        """Return whether an event should keep retrying in the current process."""
        platform = source.platform_name
        return (
            source.is_platform
            and platform is not None
            and platform not in {"cli", "ws"}
        )

    @staticmethod
    def _retry_key(event: OutboundEvent) -> str:
        """Return the stable retry key for an outbound event."""
        return f"{event.session_id}:{event.request_id}"

    async def _deliver_with_retry(
        self,
        channel: "Channel",
        content: str,
        source: EventSource,
    ) -> None:
        """Deliver content with bounded retries and backoff."""
        for attempt in range(1, MAX_RETRY + 1):
            try:
                await channel.reply(content, source)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= MAX_RETRY:
                    raise

                retry_count = attempt
                delay_ms = compute_backoff_ms(retry_count)
                self.logger.warning(
                    "Delivery attempt %s/%s failed for %s; retrying in %sms",
                    retry_count,
                    MAX_RETRY,
                    source,
                    delay_ms,
                    exc_info=True,
                )
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)

