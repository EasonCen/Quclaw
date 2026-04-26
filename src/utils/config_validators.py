"""Reusable configuration validator helpers."""

from typing import Any


def coerce_id_list(value: Any) -> Any:
    """Coerce platform id lists from YAML into string ids."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return value


def coerce_optional_id(value: Any) -> str | None:
    """Coerce an optional platform id from YAML into a string id."""
    if value is None:
        return None
    return str(value)
