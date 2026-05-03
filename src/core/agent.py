"""Agent and AgentSession with persistence support."""

import asyncio
import json
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from core.context_guard import ContextGuard
from runtime.events import EventSource
from core.session_state import SessionState
from provider.llm.base import LLMProvider, LLMToolCall, Message
from runtime.media import MessageAttachment
from tools.attach_file_tool import create_attach_file_tool
from tools.base import tool_call_to_message
from tools.post_message_tool import create_post_message_tool
from tools.registry import ToolRegistry
from tools.skill_tools import create_skill_tool
from tools.webread_tool import create_webread_tool
from tools.websearch_tool import create_websearch_tool
from tools.subagent_tool import create_subagent_dispatch_tool

if TYPE_CHECKING:
    from core.agent_loader import AgentDef
    from core.context import SharedContext
    from core.history import HistoryStore
    from core.skill_loader import SkillLoader
    from utils.config import Config


class Agent:
    """A configured agent that creates and manages conversation sessions."""

    def __init__(self, agent_def: "AgentDef", context: "SharedContext") -> None:
        self.agent_def = agent_def
        self.context = context
        self.llm = LLMProvider.from_config(agent_def.llm)

    @property
    def config(self) -> "Config":
        """Return the shared runtime config."""
        return self.context.config

    @property
    def history_store(self) -> "HistoryStore":
        """Return the shared history store."""
        return self.context.history_store

    @property
    def skill_loader(self) -> "SkillLoader":
        """Return the shared skill loader."""
        return self.context.skill_loader

    def _get_token_threshold(self) -> int:
        """Get token threshold from workspace configuration."""
        return self.context.config.context.token_threshold

    def _build_tools(self, include_post_message: bool) -> ToolRegistry:
        """Build a ToolRegistry with tools appropriate for the session."""
        registry = ToolRegistry.with_builtins()

        websearch_tool = create_websearch_tool(self.context.config)
        if websearch_tool:
            registry.register(websearch_tool)

        webread_tool = create_webread_tool(self.context.config)
        if webread_tool:
            registry.register(webread_tool)

        if self.agent_def.allow_skills:
            skill_tool = create_skill_tool(self.context.skill_loader)
            if skill_tool:
                registry.register(skill_tool)

        registry.register(create_attach_file_tool(self.context))

        if include_post_message:
            post_tool = create_post_message_tool(self.context)
            if post_tool:
                registry.register(post_tool)


        subagent_tool = create_subagent_dispatch_tool(self.agent_def.id, self.context)
        if subagent_tool:
            registry.register(subagent_tool)

        return registry

    def new_session(
        self,
        source: EventSource,
        session_id: str | None = None,
    ) -> "AgentSession":
        """Create a new conversation session for an event source."""
        session_id = session_id or uuid.uuid4().hex
        tools = self._build_tools(include_post_message=source.is_cron)

        # Create context guard for this session
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )
        state = SessionState(
            session_id=session_id,
            agent=self,
            messages=[],
            source=source,
            shared_context=self.context
        )

        session = AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )

        self.context.history_store.create_session(
            self.agent_def.id,
            session_id,
            source,
        )
        return session

    def resume_session(
        self,
        session_id: str,
        source: EventSource | None = None,
    ) -> "AgentSession":
        """Load an existing conversation session."""
        session_info = self.context.history_store.get_session_info(session_id)
        if session_info is None:
            raise ValueError(f"Session not found: {session_id}")
        if session_info.agent_id != self.agent_def.id:
            raise ValueError(
                f"Session {session_id} belongs to agent {session_info.agent_id}, "
                f"not {self.agent_def.id}"
            )

        session_source = session_info.get_source()
        if session_source is None:
            if source is None:
                raise ValueError(f"Session {session_id} has no event source")
            self.context.history_store.bind_session_source(session_id, source)
            session_source = source

        # Get all messages (no max_history limit)
        history_messages = self.context.history_store.get_messages(session_id)

        # Convert HistoryMessage to Message format
        messages: list[Message] = [msg.to_message() for msg in history_messages]

        # Build tools for resumed session
        tools = self._build_tools(include_post_message=session_source.is_cron)

        # Create context guard
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        # Create SessionState with loaded messages
        state = SessionState(
            session_id=session_info.id,
            agent=self,
            messages=messages,
            source=session_source,
            shared_context=self.context,
        )

        return AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )


@dataclass
class AgentSession:
    """LLM chat orchestrator over a swappable SessionState."""

    agent: Agent
    state: SessionState
    tools: ToolRegistry
    context_guard: ContextGuard
    started_at: datetime = field(default_factory=datetime.now)
    _response_attachments: list[MessageAttachment] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        """Delegate to state."""
        return self.state.session_id

    @property
    def source(self) -> EventSource:
        """Delegate to state."""
        return self.state.source

    @property
    def shared_context(self) -> "SharedContext":
        """Delegate to state."""
        return self.agent.context

    def add_response_attachment(self, attachment: MessageAttachment) -> None:
        """Queue an attachment for the current agent reply."""
        self._response_attachments.append(attachment)

    def consume_response_attachments(self) -> list[MessageAttachment]:
        """Return and clear attachments queued for the current reply."""
        attachments = self._response_attachments
        self._response_attachments = []
        return attachments

    def clear_response_attachments(self) -> None:
        """Discard attachments queued for the current reply."""
        self._response_attachments = []

    async def chat(self, message: str, llm_content: object | None = None) -> str:
        """Send a message to the LLM and get a response."""
        user_msg: Message = {"role": "user", "content": message}
        self.state.add_message(user_msg)
        user_message_index = len(self.state.messages) - 1

        tool_schemas = self.tools.get_tool_schemas() or None

        while True:
            self.state = await self.context_guard.check_and_compact(
                self.state,
                self.agent.llm,
            )
            messages = self._build_messages_for_llm(
                user_message_index,
                llm_content,
            )
            content, tool_calls = await self.agent.llm.chat(
                messages,
                tools=tool_schemas,
            )
            assistant_msg: Message = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    tool_call_to_message(tool_call) for tool_call in tool_calls
                ]
            self.state.add_message(assistant_msg)

            if not tool_calls:
                return content

            await self._handle_tool_calls(tool_calls)

    def _build_messages_for_llm(
        self,
        user_message_index: int,
        llm_content: object | None,
    ) -> list[Message]:
        """Build messages, optionally replacing this turn with multimodal input."""
        messages = self.state.build_messages()
        if llm_content is None:
            return messages

        state_message_count = len(self.state.messages)
        offset = len(messages) - state_message_count
        message_index = offset + user_message_index
        if message_index < 0 or message_index >= len(messages):
            return messages

        message = messages[message_index]
        if message.get("role") != "user":
            return messages

        messages[message_index] = {
            **message,
            "content": llm_content,
        }
        return messages

    async def _handle_tool_calls(
        self,
        tool_calls: list[LLMToolCall],
    ) -> None:
        """Handle tool calls from the LLM response."""
        if not tool_calls:
            return

        results = await asyncio.gather(
            *(self._execute_tool_call(tool_call) for tool_call in tool_calls)
        )
        for tool_call, result in zip(tool_calls, results):
            self.state.add_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    async def _execute_tool_call(
        self,
        tool_call: LLMToolCall,
    ) -> str:
        """Execute a single tool call."""
        try:
            args = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            args = {}

        try:
            result = await self.tools.execute_tool(tool_call.name, session=self, **args)
        except Exception as e:
            result = f"Error executing tool: {e}"

        return result
