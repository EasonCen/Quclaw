"""Heartbeat worker for periodic background awareness turns."""

import asyncio
import logging
import time

from datetime import datetime
from typing import TYPE_CHECKING

from .base import Worker
from runtime.events import DispatchEvent, DispatchResultEvent, HeartbeatEventSource

if TYPE_CHECKING:
    from core.context import SharedContext
    from utils.config import Config


logger = logging.getLogger(__name__)
DISABLED_POLL_SECONDS = 5.0
HEARTBEAT_OK = "HEARTBEAT_OK"


def build_heartbeat_prompt(config: "Config", now: datetime | None = None) -> str:
    """Build the user prompt for one heartbeat turn."""
    heartbeat_path = config.workspace / "HEARTBEAT.md"
    now = now or datetime.now()

    try:
        heartbeat_md = heartbeat_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        heartbeat_md = ""

    lines = [
        "Run a silent heartbeat check for this workspace.",
        f"Current local time: {now.isoformat(timespec='seconds')}",
        "",
        "Use this turn for periodic awareness: inspect relevant state, batch",
        "small maintenance checks, and update files only when useful.",
        f"If nothing needs attention, reply exactly {HEARTBEAT_OK}.",
        "Do not send or promise any user-facing notification.",
    ]

    if heartbeat_md:
        lines.extend(["", "HEARTBEAT.md:", "```markdown", heartbeat_md, "```"])
    else:
        lines.extend(
            [
                "",
                "No HEARTBEAT.md exists. Do a minimal workspace check and reply",
                f"{HEARTBEAT_OK} unless there is obvious useful maintenance.",
            ]
        )

    return "\n".join(lines)


def is_heartbeat_ok(content: str) -> bool:
    """Return whether a heartbeat response is the quiet success ack."""
    return content.strip() == HEARTBEAT_OK


class HeartbeatWorker(Worker):
    """Periodically dispatch one silent heartbeat turn."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self._pending_request_id: str | None = None
        self.context.eventbus.subscribe(DispatchResultEvent, self.handle_result)

    async def run(self) -> None:
        """Run heartbeat checks according to hot-reloaded config."""
        self.logger.info("HeartbeatWorker started.")
        last_dispatch_at: float | None = None
        try:
            while True:
                interval = self._interval_seconds()
                if interval <= 0:
                    last_dispatch_at = None
                    await asyncio.sleep(DISABLED_POLL_SECONDS)
                    continue

                now = time.time()
                if last_dispatch_at is None:
                    last_dispatch_at = now

                elapsed = now - last_dispatch_at
                if elapsed >= interval:
                    await self._dispatch_heartbeat()
                    last_dispatch_at = time.time()
                    elapsed = 0

                sleep_seconds = min(
                    DISABLED_POLL_SECONDS,
                    max(1.0, interval - elapsed),
                )
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            raise

    async def _dispatch_heartbeat(self, now: datetime | None = None) -> bool:
        """Publish one heartbeat dispatch event if no run is in flight."""
        if self._pending_request_id is not None:
            self.logger.info("Skipped heartbeat because a prior run is still pending")
            return False

        agent_id = self._target_agent_id()
        source = HeartbeatEventSource(agent_id=agent_id)
        event = DispatchEvent(
            session_id=self._session_id(agent_id),
            content=build_heartbeat_prompt(self.context.config, now),
            source=source,
            target_agent_id=agent_id,
        )

        self._pending_request_id = event.request_id
        try:
            await self.context.eventbus.publish(event)
        except Exception:
            self._pending_request_id = None
            raise

        self.logger.info("Dispatched heartbeat to agent %s", agent_id)
        return True

    async def handle_result(self, event: DispatchResultEvent) -> None:
        """Consume heartbeat results without producing outbound delivery."""
        if not event.source.is_heartbeat:
            return

        if self._pending_request_id == event.request_id:
            self._pending_request_id = None

        if event.error:
            self.logger.error(
                "Heartbeat failed for %s: %s",
                event.source,
                event.error,
            )
            return

        if is_heartbeat_ok(event.content):
            self.logger.debug("Heartbeat completed quietly for %s", event.source)
            return

        content = event.content.strip()
        if content:
            self.logger.info("Heartbeat result from %s: %s", event.source, content)
        else:
            self.logger.info(
                "Heartbeat completed with an empty result from %s",
                event.source,
            )

    def _target_agent_id(self) -> str:
        """Return the configured heartbeat agent, falling back to default_agent."""
        configured = self.context.config.heartbeat.agent
        agent_id = (configured or self.context.config.default_agent).strip()
        return agent_id or self.context.config.default_agent

    def _interval_seconds(self) -> float:
        """Return heartbeat interval in seconds, or zero when disabled."""
        return float(self.context.config.heartbeat.interval_minutes * 60)

    @staticmethod
    def _session_id(agent_id: str) -> str:
        """Build a stable filesystem-safe session id for heartbeat context."""
        safe_agent_id = "".join(
            char if char.isalnum() or char in ("-", "_") else "_"
            for char in agent_id
        ).strip("_") or "agent"
        return f"heartbeat-{safe_agent_id}"
