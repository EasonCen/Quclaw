"""Post message tool factory for agent-initiated messaging."""

from typing import TYPE_CHECKING

from core.events import EventSource, OutboundEvent
from tools.base import BaseTool, tool

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.context import SharedContext


def create_post_message_tool(context: "SharedContext") -> BaseTool | None:
    """Factory to create post_message tool."""
    if not context.config.channels.enabled:
        return None

    @tool(
        name="post_message",
        description=(
            "Send a proactive user-facing message to the configured default "
            "delivery channel. This tool is only allowed during background "
            "cron jobs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send.",
                }
            },
            "required": ["content"],
        },
    )
    async def post_message(content: str, session: "AgentSession") -> str:
        """Queue a proactive outbound message for DeliveryWorker."""
        if not getattr(session.source, "is_cron", False):
            return "Error: post_message can only be used by cron jobs."

        if not isinstance(content, str):
            return "Error: content must be a string."

        message = content.strip()
        if not message:
            return "Error: content cannot be empty."

        delivery_source = _default_delivery_source(context)
        if isinstance(delivery_source, str):
            return delivery_source

        event = OutboundEvent(
            session_id=session.session_id,
            content=message,
            source=delivery_source,
        )
        await context.eventbus.publish(event)

        return "Queued outbound message for DeliveryWorker."

    return post_message


def _default_delivery_source(context: "SharedContext") -> EventSource | str:
    """Parse the configured proactive delivery destination."""
    source_value = (context.config.default_delivery_source or "").strip()
    if not source_value:
        return "Error: no default delivery source is configured."

    try:
        return EventSource.from_string(source_value)
    except (ValueError, ImportError) as exc:
        return f"Error: invalid default delivery source {source_value!r}: {exc}"
