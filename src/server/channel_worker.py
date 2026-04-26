"""Channel worker for ingesting platform messages."""

import asyncio
import time
import uuid

from typing import TYPE_CHECKING, Any

from .worker import Worker
from core.events import EventSource, InboundEvent
from utils.config import SourceSessionConfig

if TYPE_CHECKING:
    from channel.base import Channel
    from core.context import SharedContext    


class ChannelWorker(Worker):
    """Ingests messages from plateform, publishes INBOUND events to Channel."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.channels: list["Channel[Any]"] = []
        self.channel_map: dict[str, "Channel[Any]"] = {}
        self._channel_tasks: dict[str, asyncio.Task[None]] = {}
        self._expected_stops: set[str] = set()
        self._fatal_errors: asyncio.Queue[BaseException] = asyncio.Queue()
        self._reload_lock = asyncio.Lock()
        self._source_session_lock = asyncio.Lock()

    async def run(self) -> None:
        """Start all chennls and process incoming messages."""
        try:
            await self.reload_channels(self.context.channels)
            raise await self._fatal_errors.get()
        except asyncio.CancelledError:
            raise
        finally:
            await self._stop_channels()

    async def reload_channels(self, channels: list["Channel[Any]"]) -> None:
        """Replace active channels with a freshly configured set."""
        async with self._reload_lock:
            await self._stop_channels()

            self.channels = list(channels)
            self.context.channels = self.channels
            self.channel_map = {
                channel.platform_name: channel
                for channel in self.channels
            }

            for channel in self.channels:
                platform = channel.platform_name
                task = asyncio.create_task(
                    channel.run(self._create_callback(platform)),
                    name=f"channel:{platform}",
                )
                self._channel_tasks[platform] = task
                task.add_done_callback(
                    lambda finished, platform=platform: self._on_channel_done(
                        platform,
                        finished,
                    )
                )

            self.logger.info(
                "ChannelWorker loaded %s channel(s): %s",
                len(self.channels),
                ", ".join(self.channel_map) or "none",
            )

    async def _stop_channels(self) -> None:
        """Stop active channels and wait for their run tasks to finish."""
        channels = list(self.channels)
        tasks = list(self._channel_tasks.values())
        self._expected_stops.update(self.channel_map)

        if channels:
            await asyncio.gather(
                *(channel.stop() for channel in channels),
                return_exceptions=True,
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._channel_tasks.clear()
        self._expected_stops.clear()
        self.channels = []
        self.channel_map = {}

    def _on_channel_done(
        self,
        platform: str,
        task: asyncio.Task[None],
    ) -> None:
        """Log channel task completion and remove it from the active task map."""
        channel = self.channel_map.get(platform)
        allow_normal_completion = bool(
            getattr(channel, "allow_normal_completion", False)
        )

        if self._channel_tasks.get(platform) is task:
            del self._channel_tasks[platform]

        expected_stop = platform in self._expected_stops
        if task.cancelled():
            if not expected_stop:
                self._fatal_errors.put_nowait(
                    RuntimeError(f"Channel {platform} was cancelled unexpectedly")
                )
            return

        exc = task.exception()
        if exc is not None:
            log = self.logger.debug if expected_stop else self.logger.error
            log(
                "Channel %s stopped with an error",
                platform,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if not expected_stop:
                self._fatal_errors.put_nowait(exc)
            return

        if not expected_stop and not allow_normal_completion:
            exc = RuntimeError(f"Channel {platform} stopped unexpectedly")
            self.logger.warning(str(exc))
            self._fatal_errors.put_nowait(exc)
        elif not expected_stop:
            self.logger.info("Channel %s stopped normally", platform)

    
    def _create_callback(self, platform: str):
        """Create callback for a specific platform."""

        async def callback(message: str, source: EventSource) -> None:
            try:
                channel = self.channel_map[platform]

                if not await channel.is_allowed(source):
                    self.logger.debug(
                        f"Ignored non-whitelisted message from {platform}"
                    )
                    return
                
                if not source.is_platform:
                    self.logger.debug("Ignored non-platform message from %s", source)
                    return

                if source.platform_name != "cli":
                    if not self.context.config.default_delivery_source:
                        source_str_value = str(source)
                        self.context.config.default_delivery_source = source_str_value
                        self.context.config.set_runtime(
                            "default_delivery_source", 
                            source_str_value,
                        )

                session_id = await self._get_or_create_session_id(source)

                # Publish INBOUND event with typed source
                event = InboundEvent(
                    session_id = session_id,
                    source=source,
                    content=message,
                    timestamp=time.time(),
                )
                await self.context.eventbus.publish(event)
                self.logger.debug(f"Published INBOUND event from {source}")

            except Exception as e:
                self.logger.error(f"Error processing message from {platform}: {e}")

        return callback




    async def _get_or_create_session_id(self, source: EventSource) -> str:
        """Get or create session ID for a given source."""
        source_str = str(source)

        async with self._source_session_lock:
            source_session = self.context.config.sources.get(source_str)
            if source_session:
                return source_session.session_id
            
            source_session = SourceSessionConfig(session_id=uuid.uuid4().hex)

            self.context.config.sources[source_str] = source_session
            self.context.config.set_runtime(
                f"sources.{source_str}",
                source_session,
            )

            return source_session.session_id
        

