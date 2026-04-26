"""Shared utilities for loading definition files (agents, skills, crons)."""

import logging
from pathlib import Path
from typing import Any, TypeVar, Callable
import yaml

T = TypeVar("T")
logger = logging.getLogger(__name__)

class DefNotFoundError(Exception):
    """Definition folder or file doesn't exist."""

    def __init__(self, def_type: str, def_id: str | Path) -> None:
        self.def_type = def_type
        self.def_id = def_id
        super().__init__(f"{def_type} not found: {def_id}")


class InvalidDefError(Exception):
    """Definition file is malformed."""

def parse_definition(
    content: str,
    def_id: str,
    parse_fn: Callable[[str, dict[str, Any], str], T],
) -> T:
    """Parse YAML frontmatter + markdown body with type conversion."""
    normalized = content.replace("\r\n", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized.removeprefix("\ufeff")

    if not normalized.startswith("---\n"):
        raise InvalidDefError(f"Missing YAML frontmatter in '{def_id}'")

    remainder = normalized[4:]
    frontmatter_text, separator, body = remainder.partition("\n---\n")
    if not separator:
        if remainder.endswith("\n---"):
            frontmatter_text = remainder[:-4]
            body = ""
        else:
            raise InvalidDefError(f"Unterminated YAML frontmatter in '{def_id}'")

    try:
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise InvalidDefError(f"Invalid YAML frontmatter in '{def_id}': {exc}") from exc

    if loaded is None:
        frontmatter: dict[str, Any] = {}
    elif isinstance(loaded, dict):
        frontmatter = loaded
    else:
        raise InvalidDefError(f"Frontmatter in '{def_id}' must be a mapping")

    try:
        return parse_fn(def_id, frontmatter, body)
    except InvalidDefError:
        raise
    except Exception as exc:
        raise InvalidDefError(f"Failed to parse definition '{def_id}': {exc}") from exc


def discover_definitions(
    path: Path,
    filename: str,
    parse_fn: Callable[[str, dict[str, Any], str], T | None],
) -> list[T]:
    """Scan directory for definition files."""
    if not path.exists() or not path.is_dir():
        raise DefNotFoundError("definition path", path)

    definitions: list[T] = []
    for entry in path.iterdir():
        if not entry.is_dir():
            continue

        definition_file = entry / filename
        if not definition_file.is_file():
            continue

        try:
            parsed = parse_definition(
                definition_file.read_text(encoding="utf-8"),
                entry.name,
                parse_fn,
            )
        except (OSError, InvalidDefError) as exc:
            logger.warning("Skipping invalid definition '%s': %s", definition_file, exc)
            continue

        if parsed is not None:
            definitions.append(parsed)

    return definitions

def write_definition(
    def_id: str,
    frontmatter: dict[str, Any],
    body: str,
    base_path: Path,
    filename: str,
) -> Path:
    """Write a definition file with YAML frontmatter and markdown body."""
    definition_dir = base_path / def_id
    definition_dir.mkdir(parents=True, exist_ok=True)

    definition_file = definition_dir / filename
    frontmatter_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    body_text = body.strip()

    if body_text:
        content = f"---\n{frontmatter_text}\n---\n\n{body_text}\n"
    else:
        content = f"---\n{frontmatter_text}\n---\n"

    definition_file.write_text(content, encoding="utf-8")
    return definition_file
