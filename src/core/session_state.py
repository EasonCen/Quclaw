"""Session state container with persistence helpers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.history import HistoryMessage
from provider.llm.base import Message

if TYPE_CHECKING:
    from core.agent import Agent
    from core.context import SharedContext
    from core.events import EventSource

@dataclass
class SessionState:
    """Pure conversation state + persistence."""

    session_id: str
    agent: "Agent"
    messages: list[Message]
    source: "EventSource"
    shared_context: "SharedContext"

    def add_message(self, message: Message) -> None:
        """Add message to in-memory list + persist."""
        self.messages.append(message)
        history_msg = HistoryMessage.from_message(message)
        self.shared_context.history_store.save_message(self.session_id, history_msg)

    def replace_messages(self, messages: list[Message]) -> None:
        """Replace in-memory messages and persist the rewritten session."""
        self.messages = list(messages)
        self.shared_context.history_store.replace_messages(
            self.session_id,
            self.messages,
        )

    def build_messages(self) -> list[Message]:
        """Build messages list with system prompt."""
        system_prompt = self.agent.agent_def.agent_md
        if not system_prompt:
            return list(self.messages)
        messages: list[Message] = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)
        
        return messages
