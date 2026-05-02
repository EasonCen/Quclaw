"""CLI channel implementation."""

import asyncio

from typing import Awaitable, Callable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from channel.base import Channel
from runtime.events import CliEventSource
from runtime.media import MessageAttachment


class CliChannel(Channel[CliEventSource]):
    """Interactive terminal channel backed by the shared channel pipeline."""

    def __init__(
        self,
        agent_label: str,
        source: CliEventSource,
        console: Console | None = None,
        response_timeout: float | None = 60.0,
    ) -> None:
        self.agent_label = agent_label
        self.source = source
        self.console = console or Console()
        self.response_timeout = response_timeout
        self._stop_event: asyncio.Event | None = None
        self._closed_event: asyncio.Event | None = None
        self._reply_event: asyncio.Event | None = None

    @property
    def platform_name(self) -> str:
        return "cli"

    @property
    def allow_normal_completion(self) -> bool:
        return True

    async def run(
        self,
        on_message: Callable[[str, CliEventSource], Awaitable[None]],
    ) -> None:
        """Read terminal messages and publish them through the channel callback."""
        self._stop_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._print_welcome()

        try:
            while not self._stop_event.is_set():
                try:
                    user_input = await asyncio.to_thread(self._get_user_input)
                except (KeyboardInterrupt, EOFError):
                    self._print_goodbye()
                    break

                if user_input.lower() in ("quit", "exit", "q"):
                    self._print_goodbye()
                    break

                if not user_input:
                    continue

                self._reply_event = asyncio.Event()
                await on_message(user_input, self.source)
                await self._wait_for_reply()
        finally:
            if self._closed_event is not None:
                self._closed_event.set()

    async def reply(
        self,
        content: str,
        source: CliEventSource,
        attachments: Sequence[MessageAttachment] | None = None,
    ) -> None:
        """Print an agent reply to the terminal."""
        if str(source) != str(self.source):
            return

        prefix = Text(f"{self.agent_label}: ", style="green")
        if content:
            self.console.print(prefix, end="")
            self.console.print(content)

        for attachment in attachments or ():
            self.console.print(prefix, end="")
            self.console.print(
                f"[attachment:{attachment.kind}] {attachment.display_name} -> "
                f"{attachment.path}"
            )

        if self._reply_event is not None:
            self._reply_event.set()

    async def is_allowed(self, source: CliEventSource) -> bool:
        """The local terminal user is always allowed."""
        return str(source) == str(self.source)

    async def stop(self) -> None:
        """Request the terminal loop to stop."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._reply_event is not None:
            self._reply_event.set()

    async def wait_closed(self) -> None:
        """Wait until the terminal input loop exits."""
        while self._closed_event is None:
            await asyncio.sleep(0.01)
        await self._closed_event.wait()

    def _get_user_input(self) -> str:
        prompt_text = Text("You", style="cyan")
        return Prompt.ask(prompt_text, console=self.console).strip()

    def _print_welcome(self) -> None:
        self.console.print(
            Panel(
                Text("Welcome to Quclaw!", style="bold cyan"),
                title="Chat",
                border_style="magenta",
            )
        )
        self.console.print("Type 'quit' or 'exit' to end the session.\n")

    def _print_goodbye(self) -> None:
        self.console.print("\n[bold yellow]Goodbye![/bold yellow]")

    async def _wait_for_reply(self) -> None:
        reply_event = self._reply_event
        if reply_event is None:
            return

        try:
            if self.response_timeout is None:
                await reply_event.wait()
            else:
                await asyncio.wait_for(reply_event.wait(), self.response_timeout)
        except asyncio.TimeoutError:
            self.console.print("[red]Agent response timed out[/red]")
            self.console.print()
        finally:
            if self._reply_event is reply_event:
                self._reply_event = None
