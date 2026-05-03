"""Post message tool factory for agent-initiated messaging."""

from typing import TYPE_CHECKING, Any

from runtime.events import EventSource, OutboundEvent
from tools.base import BaseTool, tool
from tools.shared.attachment_loader import ATTACHMENT_SCHEMA, load_attachments

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
                },
                "attachments": {
                    "type": "array",
                    "description": "Optional local files to send with the message.",
                    "items": ATTACHMENT_SCHEMA,
                }
            },
        },
    )
    async def post_message(
        session: "AgentSession",
        content: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        """Queue a proactive outbound message for DeliveryWorker."""
        if not getattr(session.source, "is_cron", False):
            return "Error: post_message can only be used by cron jobs."

        if not isinstance(content, str):
            return "Error: content must be a string."

        message = content.strip()
        parsed_attachments = load_attachments(context, attachments)
        if isinstance(parsed_attachments, str):
            return parsed_attachments

        if not message and not parsed_attachments:
            return "Error: content or attachments must be provided."

        delivery_source = _default_delivery_source(context)
        if isinstance(delivery_source, str):
            return delivery_source

        event = OutboundEvent(
            session_id=session.session_id,
            content=message,
            source=delivery_source,
            attachments=parsed_attachments,
        )
        await context.eventbus.publish(event)

        count = len(parsed_attachments)
        if count:
            return f"Queued outbound message with {count} attachment(s)."
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
