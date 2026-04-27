"""Event types and data classes for the event bus."""

import importlib
import time
import uuid

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


_EVENT_SOURCE_MODULES: dict[str, str] = {
    "platform-telegram": "channel.telegram_channel",
    "platform-discord": "channel.discord_channel",
}


class EventSource(ABC):
    """Abstract base for all event sources."""

    _registry: ClassVar[dict[str,type["EventSource"]]] = {}
    _namespace: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls._namespace:
            cls._registry[cls._namespace] = cls

    @property
    def is_platform(self) -> bool:
        return self._namespace.startswith("platform-")

    @property
    def is_agent(self) -> bool:
        return self._namespace == "agent"

    @property
    def is_cron(self) -> bool:
        return self._namespace == "cron"

    @property
    def is_heartbeat(self) -> bool:
        return self._namespace == "heartbeat"

    @property
    def platform_name(self) -> str | None:
        if not self.is_platform:
            return None
        return self._namespace.split("-",1)[1]

    @classmethod
    def from_string(cls, s: str) -> "EventSource":
        """Parse string to EventSource using namespace registry."""
        namespace = s.split(":")[0]
        source_cls = cls._registry.get(namespace)
        if source_cls is None:
            cls._load_source_module(namespace)
            source_cls = cls._registry.get(namespace)
        if not source_cls:
            raise ValueError(f"Unknown source namespace: {namespace}")
        return source_cls.from_string(s)

    @classmethod
    def _load_source_module(cls, namespace: str) -> None:
        """Load known platform source modules so subclasses can register."""
        module_name = _EVENT_SOURCE_MODULES.get(namespace)
        if module_name is None:
            return
        importlib.import_module(module_name)

@dataclass
class AgentEventSource(EventSource):
    """Source for agent-generated events."""

    _namespace =  "agent"
    agent_id: str

    def __str__(self) -> str:
        return f"agent:{self.agent_id}"

    @classmethod
    def from_string(cls, s: str) -> "AgentEventSource":
        _, agent_id = s.split(":", 1)
        return cls(agent_id = agent_id)

@dataclass
class CliEventSource(EventSource):
    """Source for CLI-originated events."""

    _namespace =  "platform-cli"
    user_id: str = "cli-user"
    conversation_id: str | None = None

    def __str__(self) -> str:
        if self.conversation_id is None:
            return f"{self._namespace}:{self.user_id}"
        return f"{self._namespace}:{self.user_id}/{self.conversation_id}"

    @classmethod
    def from_string(cls, s: str) -> "CliEventSource":
        _, payload = s.split(":", 1)
        if "/" not in payload:
            return cls(user_id=payload)

        user_id, conversation_id = payload.split("/", 1)
        return cls(user_id=user_id, conversation_id=conversation_id)

    @property
    def platform_name(self) -> str:
        return "cli"


@dataclass
class CronEventSource(EventSource):
    """Source for cron-triggered events."""

    _namespace = "cron"
    cron_id: str

    def __str__(self) -> str:
        return f"{self._namespace}:{self.cron_id}"

    @classmethod
    def from_string(cls, s: str) -> "CronEventSource":
        _, cron_id = s.split(":", 1)
        return cls(cron_id=cron_id)


@dataclass
class HeartbeatEventSource(EventSource):
    """Source for heartbeat-triggered events."""

    _namespace = "heartbeat"
    agent_id: str

    def __str__(self) -> str:
        return f"{self._namespace}:{self.agent_id}"

    @classmethod
    def from_string(cls, s: str) -> "HeartbeatEventSource":
        _, agent_id = s.split(":", 1)
        return cls(agent_id=agent_id)


@dataclass
class WebSocketEventSource(EventSource):
    """Event from WebSocket client."""

    _namespace = "platform-ws"
    client_id: str
    conversation_id: str | None = None

    @classmethod
    def from_string(cls, s: str) -> "WebSocketEventSource":
        """Parse source string into WebSocketEventSource."""
        parts = s.split(":", 1)
        if len(parts) != 2 or parts[0] != cls._namespace or not parts[1]:
            raise ValueError(f"Invalid WebSocketEventSource: {s}")

        payload = parts[1]
        if "/" not in payload:
            return cls(client_id=payload)

        client_id, conversation_id = payload.split("/", 1)
        return cls(client_id=client_id, conversation_id=conversation_id)

    def __str__(self) -> str:
        """Convert to source string format."""
        if self.conversation_id is None:
            return f"{self._namespace}:{self.client_id}"
        return f"{self._namespace}:{self.client_id}/{self.conversation_id}"

    @property
    def platform_name(self) -> str:
        return "ws"


@dataclass
class Event:
    """Base class for all typed events."""

    session_id: str
    content: str
    source: EventSource
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize event to dictionary, including type."""
        result: dict[str, Any] = {"type": self.__class__.__name__}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if field_name == "source" and isinstance(value, EventSource):
                result[field_name] = str(value)
            else:
                result[field_name] = value

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Deserialize event from dictionary."""
        kwargs = {}
        for k, v in data.items():
            if k == "type":
                continue
            elif k in cls.__dataclass_fields__:
                if k == "source" and isinstance(v, str):
                    kwargs[k] = EventSource.from_string(v)
                else:
                    kwargs[k] = v

        return cls(**kwargs)


@dataclass
class InboundEvent(Event):
    """Event for external work entering the system from platforms."""

    retry_count: int = 0



@dataclass
class OutboundEvent(Event):
    """Event for agent responses to deliver to platforms."""

    retry_count: int = 0
    error: str | None = None

@dataclass
class DispatchEvent(Event):
    """Event for internal work dispatched to an agent."""

    target_agent_id: str | None = None
    parent_session_id: str | None = None
    retry_count: int = 0


@dataclass
class DispatchResultEvent(Event):
    """Event for result of a dispatched job."""

    error: str | None = None





# Registry mapping event class names to event classes
_EVENT_CLASSES: dict[str, type[Event]] = {
    "InboundEvent": InboundEvent,
    "OutboundEvent": OutboundEvent,
    "DispatchEvent": DispatchEvent,
    "DispatchResultEvent": DispatchResultEvent,
}

def serialize_event(event: Event) -> dict[str, Any]:
    """Serialize any event type to dict"""
    return event.to_dict()

def deserialize_event(data: dict[str, Any]) -> Event:
    """Deserialize dict to appropriate event type"""
    event_type: str = data.get("type", "")

    event_class = _EVENT_CLASSES.get(event_type)
    if event_class is None:
        raise ValueError(f"Unknown event type: {event_type}")

    return event_class.from_dict(data)



