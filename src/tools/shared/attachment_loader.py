"""Shared helpers for tool attachment arguments."""

from typing import TYPE_CHECKING, Any

from runtime.media import ATTACHMENT_KINDS, MediaLoadError, MediaLoader, MessageAttachment

if TYPE_CHECKING:
    from core.context import SharedContext


ATTACHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Local file path, absolute or relative to workspace.",
        },
        "filename": {
            "type": "string",
            "description": "Optional filename shown to the recipient.",
        },
        "media_type": {
            "type": "string",
            "description": "Optional MIME type, such as application/pdf.",
        },
        "kind": {
            "type": "string",
            "enum": sorted(ATTACHMENT_KINDS),
            "description": "Optional attachment kind.",
        },
    },
    "required": ["path"],
}


def load_attachments(
    context: "SharedContext",
    attachments: list[dict[str, Any]] | None,
) -> list[MessageAttachment] | str:
    """Load attachment metadata using the shared media policy."""
    loader = MediaLoader(
        workspace=context.config.workspace,
        local_roots=context.config.media.outbound_local_roots,
        max_size_bytes=context.config.media.outbound_max_size_bytes,
    )
    try:
        return loader.load_many(attachments)
    except MediaLoadError as exc:
        return f"Error: {exc}"


def load_attachment(
    context: "SharedContext",
    attachment: dict[str, Any],
) -> MessageAttachment | str:
    """Load one attachment argument using the shared media policy."""
    parsed = load_attachments(context, [attachment])
    if isinstance(parsed, str):
        return parsed
    return parsed[0]
