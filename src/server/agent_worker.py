"""Agent worker for executing agent jobs."""
import asyncio
from typing import TYPE_CHECKING

from .worker import SubscriberWorker
from core.agent import Agent, AgentSession
from core.events import (
    InboundEvent,
    OutboundEvent,
)
from utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from core.agent_loader import AgentDef


class AgentWorker(SubscriberWorker):
    """Routes inbound events to agent sessions and emits responses."""

    def __init__(self, context):
        super().__init__(context)
        self._sessions: dict[str, AgentSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

        # Auto-subscribe to events
        self.context.eventbus.subscribe(InboundEvent, self.dispatch_event)
        self.logger.info("AgentWorker subscribed to InboundEvent events")

    def clear_sessions(self) -> None:
        """Drop cached sessions so future requests use the latest config."""
        count = len(self._sessions)
        self._sessions.clear()
        self.logger.info("Cleared %s cached agent sessions after config reload", count)

    async def dispatch_event(self, event: InboundEvent) -> None:
        """Create a background executor task for an inbound event."""
        task = asyncio.create_task(
            self.exec_session(event),
            name=f"agent-session:{event.session_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def exec_session(self, event: InboundEvent) -> None:
        """Execute an inbound event against an agent session."""
        session_id = event.session_id
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            try:
                agent_def = self._route_agent_def(session_id)
                session = self._get_or_create_session(event, agent_def)
                content = await self._run_command_or_chat(event.content, session)
            except asyncio.CancelledError:
                raise
            except DefNotFoundError as exc:
                await self._emit_response(event, "", error=str(exc))
                return
            except Exception as exc:
                self.logger.exception(
                    "Agent execution failed for session %s",
                    session_id,
                )
                await self._emit_response(event, "", error=str(exc))
                return

            await self._emit_response(event, content)

    def _route_agent_def(self, session_id: str) -> "AgentDef":
        """Route a session to its agent definition."""
        session_info = self.context.history_store.get_session_info(session_id)
        if session_info is None:
            agent_id = self.context.config.default_agent
            self.logger.debug(
                "Session %s not found in history; using default agent %s",
                session_id,
                agent_id,
            )
        else:
            agent_id = session_info.agent_id

        return self.context.agent_loader.load(agent_id)

    def _get_or_create_session(
        self,
        event: InboundEvent,
        agent_def: "AgentDef",
    ) -> AgentSession:
        """Return cached session or create one for the requested agent."""
        session_id = event.session_id
        session = self._sessions.get(session_id)
        if session is not None and session.agent.agent_def.id == agent_def.id:
            return session

        agent = Agent(agent_def, self.context)
        if self.context.history_store.get_session_info(session_id) is None:
            session = agent.new_session(
                source=event.source,
                session_id=session_id,
            )
        else:
            session = agent.resume_session(session_id, source=event.source)
        self._sessions[session_id] = session
        return session

    async def _run_command_or_chat(
        self,
        content: str,
        session: AgentSession,
    ) -> str:
        """Run slash commands in the command registry before chatting."""
        command_result = await self.context.command_registry.dispatch(content, session)
        if command_result is not None:
            return command_result

        return await session.chat(content)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Cleanup finished executor tasks and log unexpected crashes."""
        self._tasks.discard(task)
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            self.logger.error(
                "Agent executor task crashed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def stop(self) -> None:
        """Stop the worker and cancel any in-flight session executors."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await super().stop()

    async def _emit_response(
        self,
        event: InboundEvent,
        content: str,
        error: str | None = None,
    ) -> None:
        """Emit response event with content."""
        await self.context.eventbus.publish(
            OutboundEvent(
                session_id=event.session_id,
                content=content,
                source=event.source,
                request_id=event.request_id,
                error=error,
            )
        )
