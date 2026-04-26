"""Server orchestrator for worker-based architecture."""

import asyncio
import logging
from typing import TYPE_CHECKING, TypeVar

from channel.base import Channel

from .agent_worker import AgentWorker
from .channel_worker import ChannelWorker
from .cron_worker import CronWorker
from .delivery_worker import DeliveryWorker
from .heartbeat_worker import HeartbeatWorker
from .websocket_worker import WebSocketWorker
from .worker import Worker
from utils.config import ConfigReloader

if TYPE_CHECKING:
    from core.context import SharedContext
    from utils.config import WebSocketConfig

logger = logging.getLogger(__name__)
W = TypeVar("W", bound=Worker)


class Server:
    """Orchestrates workers with queue-based communication."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        self.workers: list[Worker] = []
        self.config_reloader = ConfigReloader(self.context.config)
        self._reload_lock = asyncio.Lock()
        self._reload_task: asyncio.Task[None] | None = None
        self._expected_stopped_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Start all workers and monitor for crashes."""
        self._setup_workers()
        self._setup_config_reload()
        self._start_workers()

        try:
            self.config_reloader.start()
            await self._monitor_workers()
        except asyncio.CancelledError:
            logger.info("Server shutting down...")
            raise
        finally:
            self.config_reloader.set_on_change(None)
            self.config_reloader.stop()
            await self._cancel_reload_task()
            await self._stop_all()

    def _setup_workers(self) -> None:
        """Create workers and register event subscriptions."""
        self.workers = [
            self.context.eventbus,
            DeliveryWorker(self.context),
            AgentWorker(self.context),
            ChannelWorker(self.context),
            CronWorker(self.context),
            HeartbeatWorker(self.context),
        ]
        if self.context.config.websocket.enabled:
            self.workers.append(WebSocketWorker(self.context))

    def _setup_config_reload(self) -> None:
        """Wire config hot reload into the running event loop."""
        loop = asyncio.get_running_loop()

        def schedule_config_reload() -> None:
            loop.call_soon_threadsafe(self._schedule_config_reload)

        self.config_reloader.set_on_change(schedule_config_reload)

    def _schedule_config_reload(self) -> None:
        """Schedule a single config reload task in the event loop."""
        if self._reload_task is not None and not self._reload_task.done():
            logger.debug("Config reload already in progress")
            return

        self._reload_task = asyncio.create_task(
            self._reload_config(),
            name="config-reload",
        )
        self._reload_task.add_done_callback(self._on_reload_done)

    async def _reload_config(self) -> None:
        """Reload config, rebuild channels, and clear cached agent sessions."""
        async with self._reload_lock:
            old_websocket_config = self.context.config.websocket.model_copy(deep=True)
            if not self.context.config.reload():
                logger.warning("Config reload failed")
                return

            new_channels = Channel.from_config(self.context.config)
            channel_worker = self._get_worker(ChannelWorker)
            delivery_worker = self._get_worker(DeliveryWorker)

            if channel_worker is not None:
                await channel_worker.reload_channels(new_channels)
            else:
                self.context.channels = new_channels

            if delivery_worker is not None:
                delivery_worker.reload_channels(self.context.channels)

            await self._reload_websocket(old_websocket_config)

            for worker in self.workers:
                if isinstance(worker, AgentWorker):
                    worker.clear_sessions()
            logger.info("Config reloaded")

    async def _reload_websocket(
        self,
        old_websocket_config: "WebSocketConfig",
    ) -> None:
        """Start, stop, or restart WebSocketWorker after config reload."""
        websocket_worker = self._get_worker(WebSocketWorker)
        websocket_config = self.context.config.websocket

        if not websocket_config.enabled:
            if websocket_worker is not None:
                await self._stop_worker(websocket_worker)
            return

        if websocket_worker is not None and old_websocket_config == websocket_config:
            return

        if websocket_worker is not None:
            await self._stop_worker(websocket_worker)

        websocket_worker = WebSocketWorker(self.context)
        self.workers.append(websocket_worker)
        self._start_worker(websocket_worker)

    def _on_reload_done(self, task: asyncio.Task[None]) -> None:
        """Log unexpected config reload task failures."""
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            logger.exception(
                "Config reload failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _get_worker(self, worker_type: type[W]) -> W | None:
        """Return the first worker matching a type."""
        for worker in self.workers:
            if isinstance(worker, worker_type):
                return worker
        return None

    def _start_workers(self) -> None:
        """Start all configured workers."""
        for worker in self.workers:
            self._start_worker(worker)

    def _start_worker(self, worker: Worker) -> None:
        """Start one worker and log the lifecycle event."""
        worker.start()
        logger.info("Started %s", worker.__class__.__name__)

    async def _stop_worker(self, worker: Worker) -> None:
        """Stop and remove one worker without treating it as a crash."""
        if worker._task is not None:
            self._expected_stopped_tasks.add(worker._task)

        await worker.stop()
        if worker in self.workers:
            self.workers.remove(worker)
        logger.info("Stopped %s", worker.__class__.__name__)

    async def _monitor_workers(self) -> None:
        """Wait until a worker exits or crashes."""
        while True:
            tasks = [
                worker._task
                for worker in self.workers
                if worker._task is not None
            ]
            if not tasks:
                raise RuntimeError("Server has no workers to monitor")

            done, _ = await asyncio.wait(
                tasks,
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue

            for task in done:
                if task in self._expected_stopped_tasks:
                    self._expected_stopped_tasks.discard(task)
                    continue

                if task.cancelled():
                    raise asyncio.CancelledError

                exception = task.exception()
                if exception is not None:
                    raise exception

                worker_name = task.get_name()
                raise RuntimeError(f"Worker stopped unexpectedly: {worker_name}")

    async def _stop_all(self) -> None:
        """Stop all workers in reverse startup order."""
        for worker in reversed(self.workers):
            try:
                await worker.stop()
                logger.info("Stopped %s", worker.__class__.__name__)
            except Exception:
                logger.exception("Failed to stop %s", worker.__class__.__name__)

    async def _cancel_reload_task(self) -> None:
        """Cancel an in-flight reload task during shutdown."""
        task = self._reload_task
        if task is None or task.done():
            return

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
