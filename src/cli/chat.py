"""Chat CLI command for interactive sessions with persistence."""

import asyncio
import uuid
import typer

from rich.console import Console

from channel.cli_channel import CliChannel
from core.context import SharedContext
from runtime.events import CliEventSource
from server.workers.agent import AgentWorker
from server.workers.channel import ChannelWorker
from server.workers.delivery import DeliveryWorker
from server.workers.base import Worker
from utils.config import Config, ConfigReloader
from utils.logging import setup_logging

class ChatLoop:
    """Interactive chat session with persistence."""

    def __init__(self, config: Config, agent_id: str | None = None):
        self.config = config
        self.console = Console()
        self.context = SharedContext(config=config, channels=[])
        self.config_reloader = ConfigReloader(config)

        agent_id = agent_id or config.default_agent
        self.agent_def = self.context.agent_loader.load(agent_id)
        self.context.config.default_agent = self.agent_def.id

        self.cli_channel = CliChannel(
            agent_label=self.agent_def.id,
            source=CliEventSource(conversation_id=uuid.uuid4().hex),
            console=self.console,
        )
        self.context.channels = [self.cli_channel]

        self.delivery_worker = DeliveryWorker(self.context)
        self.agent_worker = AgentWorker(self.context)
        self.channel_worker = ChannelWorker(self.context)
        self.workers: list[Worker] = [
            self.context.eventbus,
            self.delivery_worker,
            self.agent_worker,
            self.channel_worker,
        ]

    def reload_config_and_reset_sessions(self) -> None:
        """Reload config and rebuild sessions on the next request."""
        if self.config.reload():
            self.context.config.default_agent = self.agent_def.id
            self.agent_worker.clear_sessions()

    async def run(self) -> None:
        """Run the interactive chat loop."""
        loop = asyncio.get_running_loop()

        def schedule_config_reload() -> None:
            loop.call_soon_threadsafe(self.reload_config_and_reset_sessions)

        self.config_reloader.set_on_change(schedule_config_reload)

        try:
            self.config_reloader.start()

            for worker in self.workers:
                worker.start()

            await self._wait_until_cli_closed_or_worker_stops()

        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[bold yellow]Goodbye![/bold yellow]")
        finally:
            for worker in reversed(self.workers):
                await worker.stop()
            self.config_reloader.set_on_change(None)
            self.config_reloader.stop()

    async def _wait_until_cli_closed_or_worker_stops(self) -> None:
        """Wait until the CLI exits, while still surfacing worker crashes."""
        cli_closed = asyncio.create_task(
            self.cli_channel.wait_closed(),
            name="cli-channel-closed",
        )
        worker_tasks = [
            worker._task
            for worker in self.workers
            if worker._task is not None
        ]

        done, _ = await asyncio.wait(
            [cli_closed, *worker_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cli_closed in done:
            return

        cli_closed.cancel()
        await asyncio.gather(cli_closed, return_exceptions=True)

        for task in done:
            if task.cancelled():
                raise asyncio.CancelledError

            exception = task.exception()
            if exception is not None:
                raise exception

            raise RuntimeError(f"Worker stopped unexpectedly: {task.get_name()}")


def chat_command(ctx: typer.Context , agent_id: str | None = None) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")
    setup_logging(config, console_output=False)

    chat_loop = ChatLoop(config, agent_id=agent_id)
    asyncio.run(chat_loop.run())
