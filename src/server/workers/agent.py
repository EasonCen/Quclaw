"""Agent worker for executing agent jobs."""
import asyncio
import base64

from pathlib import Path
from typing import TYPE_CHECKING

from .heartbeat import is_heartbeat_ok
from .base import SubscriberWorker
from core.agent import Agent, AgentSession
from runtime.events import (
    DispatchEvent,
    DispatchResultEvent,
    InboundEvent,
    OutboundEvent,
)
from runtime.media import MessageAttachment
from provider.llm.base import Message
from utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from core.agent_loader import AgentDef

AgentWorkEvent = InboundEvent | DispatchEvent


def _format_attachment(
    index: int,
    attachment: MessageAttachment,
) -> list[str]:
    """Render one attachment as text for the agent."""
    media_type = attachment.media_type or "unknown"
    lines = [
        f"{index}. {attachment.kind} {media_type} {attachment.display_name}",
        f"   path: {attachment.path}",
    ]
    return lines


def _image_attachment_part(attachment: MessageAttachment) -> dict | None:
    """Return an OpenAI-compatible image content part for image attachments."""
    if attachment.kind != "image":
        return None

    path = Path(attachment.path)
    if not path.is_file():
        return None

    media_type = attachment.media_type or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media_type};base64,{encoded}",
        },
    }


class AgentWorker(SubscriberWorker):
    """Routes inbound and dispatch events to agent sessions."""

    def __init__(self, context):
        super().__init__(context)
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

        # Auto-subscribe to events
        self.context.eventbus.subscribe(InboundEvent, self.dispatch_event)
        self.context.eventbus.subscribe(DispatchEvent, self.dispatch_event)
        self.logger.info(
            "AgentWorker subscribed to InboundEvent and DispatchEvent events"
        )

    def clear_sessions(self) -> None:
        """Drop cached sessions and concurrency gates after config reload."""
        session_count = len(self._sessions)
        semaphore_count = len(self._semaphores)
        self._sessions.clear()
        self._semaphores.clear()
        self.logger.info(
            "Cleared %s cached agent sessions and %s semaphore(s) after config reload",
            session_count,
            semaphore_count,
        )

    async def dispatch_event(self, event: AgentWorkEvent) -> None:
        """Create a background executor task for an agent work event."""
        task = asyncio.create_task(
            self.exec_session(event),
            name=f"agent-session:{event.__class__.__name__}:{event.session_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def exec_session(self, event: AgentWorkEvent) -> None:
        """Execute an event against an agent session."""
        session_id = event.session_id
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        heartbeat_snapshot: list[Message] | None = None
        response_attachments: list[MessageAttachment] = []
        try:
            agent_def = self._route_agent_def(event)
            semaphore = self._get_or_create_semaphore(agent_def)

            async with lock:
                async with semaphore:
                    session = self._get_or_create_session(event, agent_def)
                    if self._should_prune_heartbeat_ok(event):
                        heartbeat_snapshot = list(session.state.messages)
                    content = await self._run_command_or_chat(event, session)
                    response_attachments = session.consume_response_attachments()
                    if heartbeat_snapshot is not None and is_heartbeat_ok(content):
                        session.state.replace_messages(
                            heartbeat_snapshot,
                            archive=False,
                            preserve_title=bool(heartbeat_snapshot),
                        )
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

        await self._emit_response(event, content, attachments=response_attachments)

    @staticmethod
    def _should_prune_heartbeat_ok(event: AgentWorkEvent) -> bool:
        """Return whether quiet heartbeat acks should be removed from history."""
        return isinstance(event, DispatchEvent) and event.source.is_heartbeat

    def _route_agent_def(self, event: AgentWorkEvent) -> "AgentDef":
        """Route an event to its agent definition."""
        session_id = event.session_id
        session_info = self.context.history_store.get_session_info(session_id)
        if session_info is None:
            agent_id = self._resolve_agent_id(event)
            self.logger.debug(
                "Session %s not found in history; routed source %s to agent %s",
                session_id,
                event.source,
                agent_id,
            )
        else:
            agent_id = session_info.agent_id

        return self.context.agent_loader.load(agent_id)

    def _resolve_agent_id(self, event: AgentWorkEvent) -> str:
        """Resolve the target agent for a new session."""
        if isinstance(event, DispatchEvent) and event.target_agent_id:
            return event.target_agent_id

        return self.context.routing_table.resolve(str(event.source))

    def _get_or_create_session(
        self,
        event: AgentWorkEvent,
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
        event: AgentWorkEvent,
        session: AgentSession,
    ) -> str:
        """Run slash commands in the command registry before chatting."""
        content = event.content
        command_result = await self.context.command_registry.dispatch(content, session)
        if command_result is not None:
            return command_result

        content = self._content_with_attachments(event)
        return await session.chat(
            content,
            llm_content=self._llm_content_with_attachments(event, content),
        )

    @staticmethod
    def _content_with_attachments(event: AgentWorkEvent) -> str:
        """Append attachment metadata to content for text-only agent input."""
        attachments = event.attachments
        if not attachments:
            return event.content

        lines = ["[Attachments]"]
        for index, attachment in enumerate(attachments, start=1):
            lines.extend(_format_attachment(index, attachment))

        attachment_block = "\n".join(lines)
        content = event.content.strip()
        if not content:
            return attachment_block
        return f"{content}\n\n{attachment_block}"

    @staticmethod
    def _llm_content_with_attachments(
        event: AgentWorkEvent,
        content: str,
    ) -> object | None:
        """Build OpenAI-compatible multimodal content for image attachments."""
        image_parts = [
            image_part
            for attachment in event.attachments
            if (image_part := _image_attachment_part(attachment)) is not None
        ]
        if not image_parts:
            return None

        return [
            {"type": "text", "text": content},
            *image_parts,
        ]

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
        event: AgentWorkEvent,
        content: str,
        attachments: list[MessageAttachment] | None = None,
        error: str | None = None,
    ) -> None:
        """Emit response event with content."""
        response_attachments = [] if error else (attachments or [])
        if isinstance(event, DispatchEvent):
            await self.context.eventbus.publish(
                DispatchResultEvent(
                    session_id=event.session_id,
                    content=content,
                    source=event.source,
                    request_id=event.request_id,
                    attachments=response_attachments,
                    error=error,
                )
            )
            return

        await self.context.eventbus.publish(
            OutboundEvent(
                session_id=event.session_id,
                content=content,
                source=event.source,
                request_id=event.request_id,
                attachments=response_attachments,
                error=error,
            )
        )

    def _get_or_create_semaphore(self, agent_def: "AgentDef") -> asyncio.Semaphore:
        """Get existing or create new semaphore for agent."""
        if agent_def.id not in self._semaphores:
            self._semaphores[agent_def.id] = asyncio.Semaphore(
                agent_def.max_concurrency
            )
            self.logger.debug(
                "Created semaphore for %s with limit %s",
                agent_def.id,
                agent_def.max_concurrency,
            )
        return self._semaphores[agent_def.id]
