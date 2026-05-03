"""Attach local files to the current agent reply."""

from typing import TYPE_CHECKING

from tools.base import BaseTool, tool
from tools.shared.attachment_loader import ATTACHMENT_SCHEMA, load_attachment

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.context import SharedContext


def create_attach_file_tool(context: "SharedContext") -> BaseTool:
    """Factory to create attach_file tool."""

    @tool(
        name="attach_file",
        description=(
            "Attach a local file to the current reply. Use this when the user "
            "asks you to send an existing file from the workspace."
        ),
        parameters={
            "type": "object",
            "properties": ATTACHMENT_SCHEMA["properties"],
            "required": ATTACHMENT_SCHEMA["required"],
        },
    )
    async def attach_file(
        session: "AgentSession",
        path: str,
        filename: str | None = None,
        media_type: str | None = None,
        kind: str | None = None,
    ) -> str:
        """Queue a local file for the current reply."""
        attachment_data = {
            "path": path,
            "filename": filename,
            "media_type": media_type,
            "kind": kind,
        }
        attachment = load_attachment(context, attachment_data)
        if isinstance(attachment, str):
            return attachment

        session.add_response_attachment(attachment)
        return f"Attached {attachment.display_name} to the current reply."

    return attach_file
