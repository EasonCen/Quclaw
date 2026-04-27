"""JSONL file-based conversation history backend."""

import shutil
import uuid

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from runtime.events import EventSource
from provider.llm.base import Message

if TYPE_CHECKING:
    from utils.config import Config


def _now_iso() -> str:
    """Return current datetime as ISO format string."""
    return datetime.now().isoformat()


class HistorySession(BaseModel):
    """Session metadata - store in index.jsonl"""

    id: str
    agent_id: str
    source: str | None = None
    title: str | None = None
    message_count: int = 0
    created_at: str
    updated_at: str

    @field_validator("source", mode="before")
    @classmethod
    def serialize_source(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def get_source(self) -> EventSource | None:
        """Get the session's EventSource."""
        if self.source is None:
            return None
        return EventSource.from_string(self.source)

    def try_get_source(self) -> EventSource | None:
        """Best-effort parse of the session source."""
        try:
            return self.get_source()
        except (ValueError, ImportError, TypeError, AttributeError):
            return None


class HistoryMessage(BaseModel):
    """Single message - stored in session.jsonl."""

    timestamp: str = Field(default_factory=_now_iso)
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @classmethod
    def from_message(cls, message: Message) -> "HistoryMessage":
        """Create a persisted history record from a runtime LLM message."""
        return cls(
            timestamp=str(message.get("timestamp") or _now_iso()),
            role=message["role"],
            content=str(message.get("content") or ""),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
        )

    def to_message(self) -> Message:
        """Convert this history record back to the runtime LLM message shape."""
        message: Message = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }
        if self.tool_calls is not None:
            message["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        return message


class HistoryStore:
    """JSONL file-based history storage."""

    @staticmethod
    def from_config(config: "Config") -> "HistoryStore":
        return HistoryStore(config.history_path)

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)
        self.sessions_path = self.base_path / "sessions"
        self.archive_path = self.base_path / "archive"
        self.index_path = self.base_path / "index.jsonl"

        self.base_path.mkdir(parents=True, exist_ok=True)
        self.sessions_path.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_path / f"{session_id}.jsonl"

    def _read_index(self) -> list[HistorySession]:
        """Read all session entries from index.jsonl."""
        if not self.index_path.exists():
            return []

        with self.index_path.open("r", encoding="utf-8") as f:
            return [
                HistorySession.model_validate_json(line)
                for line in f
                if line.strip()
            ]

    def _write_index(self, sessions: list[HistorySession]) -> None:
        """Write all session entries to index.jsonl."""
        with self.index_path.open("w", encoding="utf-8", newline="\n") as f:
            for session in sessions:
                f.write(session.model_dump_json())
                f.write("\n")

    def _find_session_index(
        self, sessions: list[HistorySession], session_id: str
    ) -> int:
        """Find the index of a session in the list."""
        for index, session in enumerate(sessions):
            if session.id == session_id:
                return index
        return -1

    @staticmethod
    def _derive_title(messages: list[HistoryMessage]) -> str | None:
        """Derive a session title from the first user message."""
        for message in messages:
            if message.role == "user" and message.content:
                return message.content.strip().splitlines()[0][:80]
        return None

    def create_session(
        self,
        agent_id: str,
        session_id: str,
        source: EventSource | None = None,
    ) -> dict[str, Any]:
        """Create a new conversation session."""
        sessions = self._read_index()
        existing_index = self._find_session_index(sessions, session_id)
        if existing_index >= 0:
            existing = sessions[existing_index]
            if existing.source is None and source is not None:
                sessions[existing_index] = existing.model_copy(
                    update={"source": str(source)}
                )
                self._write_index(sessions)
                return sessions[existing_index].model_dump()
            return sessions[existing_index].model_dump()

        now = _now_iso()
        session = HistorySession(
            id=session_id,
            agent_id=agent_id,
            source=str(source) if source is not None else None,
            created_at=now,
            updated_at=now,
        )
        sessions.append(session)
        self._write_index(sessions)
        self._session_path(session_id).touch(exist_ok=True)
        return session.model_dump()

    def bind_session_source(
        self,
        session_id: str,
        source: EventSource,
    ) -> HistorySession | None:
        """Bind a source to an existing session if it does not have one."""
        sessions = self._read_index()
        session_index = self._find_session_index(sessions, session_id)
        if session_index < 0:
            return None

        session = sessions[session_index]
        if session.source is not None:
            return session

        sessions[session_index] = session.model_copy(
            update={"source": str(source)}
        )
        self._write_index(sessions)
        return sessions[session_index]

    def save_message(self, session_id: str, message: HistoryMessage) -> None:
        """Save a message to history."""
        with self._session_path(session_id).open(
            "a",
            encoding="utf-8",
            newline="\n",
        ) as f:
            f.write(message.model_dump_json())
            f.write("\n")

        sessions = self._read_index()
        session_index = self._find_session_index(sessions, session_id)
        if session_index < 0:
            raise ValueError(f"Session not found: {session_id}")

        session = sessions[session_index]
        title = session.title
        if not title and message.role == "user" and message.content:
            title = message.content.strip().splitlines()[0][:80]

        sessions[session_index] = session.model_copy(
            update={
                "title": title,
                "message_count": session.message_count + 1,
                "updated_at": message.timestamp,
            }
        )
        self._write_index(sessions)

    def archive_session(
        self,
        session_id: str,
        reason: str = "compaction",
    ) -> Path | None:
        """Archive the current session message file before rewriting it."""
        session_path = self._session_path(session_id)
        if not session_path.exists() or session_path.stat().st_size == 0:
            return None

        archive_dir = self.archive_path / session_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(
            char if char.isalnum() or char in ("-", "_") else "_"
            for char in reason
        ).strip("_") or "archive"
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        archive_path = archive_dir / f"{timestamp}-{safe_reason}.jsonl"
        shutil.copy2(session_path, archive_path)
        return archive_path

    def replace_messages(
        self,
        session_id: str,
        messages: list[Message],
        *,
        archive: bool = True,
        reason: str = "compaction",
        preserve_title: bool = True,
    ) -> None:
        """Replace a session's persisted messages."""
        sessions = self._read_index()
        session_index = self._find_session_index(sessions, session_id)
        if session_index < 0:
            raise ValueError(f"Session not found: {session_id}")

        history_messages = [
            HistoryMessage.from_message(message)
            for message in messages
        ]
        if archive:
            self.archive_session(session_id, reason)

        session_path = self._session_path(session_id)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = session_path.with_name(
            f"{session_path.name}.{uuid.uuid4().hex}.tmp"
        )
        with temp_path.open("w", encoding="utf-8", newline="\n") as f:
            for message in history_messages:
                f.write(message.model_dump_json())
                f.write("\n")
        temp_path.replace(session_path)

        session = sessions[session_index]
        updated_at = history_messages[-1].timestamp if history_messages else _now_iso()
        title = session.title if preserve_title else self._derive_title(history_messages)
        sessions[session_index] = session.model_copy(
            update={
                "title": title,
                "message_count": len(history_messages),
                "updated_at": updated_at,
            }
        )
        self._write_index(sessions)

    def list_sessions(self) -> list[HistorySession]:
        """List all sessions, most recently updated first."""
        return sorted(
            self._read_index(),
            key=lambda session: session.updated_at,
            reverse=True,
        )

    def get_messages(self, session_id: str) -> list[HistoryMessage]:
        """Get all messages for a session."""
        session_path = self._session_path(session_id)
        if not session_path.exists():
            return []

        with session_path.open("r", encoding="utf-8") as f:
            return [
                HistoryMessage.model_validate_json(line)
                for line in f
                if line.strip()
            ]

    def get_session_info(self, session_id: str) -> HistorySession | None:
        """Get session metadata without loading messages."""
        sessions = self._read_index()
        session_index = self._find_session_index(sessions, session_id)
        if session_index < 0:
            return None
        return sessions[session_index]
