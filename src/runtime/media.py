"""Media attachment model and local file loading helpers."""

import mimetypes

from dataclasses import dataclass
from pathlib import Path
from typing import Any


ATTACHMENT_KINDS = {"file", "image", "video", "audio"}


class MediaLoadError(ValueError):
    """Raised when a media attachment cannot be loaded safely."""


@dataclass(frozen=True)
class MessageAttachment:
    """File attachment metadata carried by outbound events."""

    path: str
    filename: str | None = None
    media_type: str | None = None
    kind: str = "file"

    def __post_init__(self) -> None:
        if self.kind not in ATTACHMENT_KINDS:
            raise ValueError(f"Unsupported attachment kind: {self.kind}")

    @property
    def display_name(self) -> str:
        """Return the filename to expose to the target platform."""
        return self.filename or Path(self.path).name

    def to_dict(self) -> dict[str, str | None]:
        """Serialize attachment metadata to JSON-safe data."""
        return {
            "path": self.path,
            "filename": self.filename,
            "media_type": self.media_type,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageAttachment":
        """Deserialize attachment metadata from JSON data."""
        return cls(
            path=str(data["path"]),
            filename=_optional_str(data.get("filename")),
            media_type=_optional_str(data.get("media_type")),
            kind=str(data.get("kind") or "file"),
        )

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        filename: str | None = None,
        media_type: str | None = None,
        kind: str | None = None,
    ) -> "MessageAttachment":
        """Build attachment metadata from a local path."""
        path_str = str(path)
        detected_media_type = media_type or mimetypes.guess_type(path_str)[0]
        return cls(
            path=path_str,
            filename=filename,
            media_type=detected_media_type,
            kind=kind or _kind_from_media_type(detected_media_type),
        )


class MediaLoader:
    """Loads outbound media from local files using workspace policy."""

    def __init__(
        self,
        *,
        workspace: Path,
        local_roots: list[Path],
        max_size_bytes: int,
    ) -> None:
        self.workspace = workspace.resolve()
        roots = local_roots or [self.workspace]
        self.local_roots = tuple(root.resolve() for root in roots)
        self.max_size_bytes = max_size_bytes

    def load_many(
        self,
        attachments: list[dict[str, Any]] | None,
    ) -> list[MessageAttachment]:
        """Validate and load multiple attachment arguments."""
        if attachments is None:
            return []
        if not isinstance(attachments, list):
            raise MediaLoadError("attachments must be a list.")

        loaded: list[MessageAttachment] = []
        for index, item in enumerate(attachments, start=1):
            if not isinstance(item, dict):
                raise MediaLoadError(f"attachment #{index} must be an object.")
            loaded.append(self.load_one(item, index=index))
        return loaded

    def load_one(
        self,
        attachment: dict[str, Any],
        *,
        index: int | None = None,
    ) -> MessageAttachment:
        """Validate and load one attachment argument."""
        label = f"attachment #{index}" if index is not None else "attachment"
        raw_path = attachment.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise MediaLoadError(f"{label} path is required.")

        path = self._resolve_path(raw_path)
        self._check_allowed_root(path, label)
        self._check_file(path, label)
        self._check_size(path, label)

        filename = attachment.get("filename")
        media_type = attachment.get("media_type")
        kind = attachment.get("kind")
        try:
            return MessageAttachment.from_path(
                path,
                filename=filename if isinstance(filename, str) else None,
                media_type=media_type if isinstance(media_type, str) else None,
                kind=kind if isinstance(kind, str) else None,
            )
        except ValueError as exc:
            raise MediaLoadError(f"{label} is invalid: {exc}") from exc

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _check_allowed_root(self, path: Path, label: str) -> None:
        if any(_is_relative_to(path, root) for root in self.local_roots):
            return

        roots = ", ".join(str(root) for root in self.local_roots)
        raise MediaLoadError(f"{label} is outside allowed media roots: {roots}")

    @staticmethod
    def _check_file(path: Path, label: str) -> None:
        if not path.is_file():
            raise MediaLoadError(f"{label} does not exist: {path}")

    def _check_size(self, path: Path, label: str) -> None:
        size = path.stat().st_size
        if size <= self.max_size_bytes:
            return

        limit_mb = self.max_size_bytes / 1024 / 1024
        raise MediaLoadError(f"{label} exceeds max media size: {limit_mb:.0f} MB")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _kind_from_media_type(media_type: str | None) -> str:
    if media_type is None:
        return "file"

    top_level = media_type.split("/", 1)[0]
    if top_level in ATTACHMENT_KINDS:
        return top_level
    return "file"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
