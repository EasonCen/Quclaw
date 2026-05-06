"""Pure business rules for the resignation scenario."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from runtime.events import EventSource
from scenario.model import (
    CasePhase,
    CaseStatus,
    ScenarioCase,
    ScenarioRole,
    TaskType,
)
from scenario.effects import SCENARIO_TZ, timestamp


class ScenarioError(ValueError):
    """Business-level scenario error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def role_from_source(source: EventSource | str) -> ScenarioRole:
    """Resolve trusted scenario role from an event source."""
    source_str = str(source)
    if source_str.startswith("cron:"):
        return ScenarioRole.SYSTEM

    if not source_str.startswith("platform-telegram:"):
        raise ScenarioError("unsupported_source", f"Unsupported source: {source_str}")

    payload = source_str.split(":", 1)[1]
    parts = payload.split("/")
    if len(parts) < 2:
        raise ScenarioError("unsupported_source", "Default Telegram bot is not a scenario role.")

    try:
        return ScenarioRole(parts[0])
    except ValueError as exc:
        raise ScenarioError("unknown_role", f"Unknown scenario role: {parts[0]}") from exc


def require_role(actual: ScenarioRole, allowed: set[ScenarioRole], action: str) -> None:
    """Raise when a role may not perform an action."""
    if actual not in allowed:
        allowed_values = ", ".join(sorted(role.value for role in allowed))
        raise ScenarioError(
            "permission_denied",
            f"Role {actual.value} cannot perform {action}. Allowed: {allowed_values}.",
        )


def task_type_from_payload(payload: dict[str, Any]) -> TaskType:
    """Parse task_type from a complete_task payload."""
    value = payload.get("task_type")
    try:
        return TaskType(str(value))
    except ValueError as exc:
        raise ScenarioError("invalid_payload", f"Unsupported task_type: {value}") from exc


def require_active_case(case: ScenarioCase | None) -> ScenarioCase:
    """Return active case or raise."""
    if case is None:
        raise ScenarioError("case_not_found", "No active case.")
    if case.status != CaseStatus.ACTIVE:
        raise ScenarioError("case_closed", f"Case is not active: {case.case_id}")
    return case


def require_no_active_case(case: ScenarioCase | None) -> None:
    """Raise if a case is already active."""
    if case is not None and case.status == CaseStatus.ACTIVE:
        raise ScenarioError("active_case_exists", f"Active case already exists: {case.case_id}")


def require_payload_text(payload: dict[str, Any], key: str) -> str:
    """Require a non-empty text payload field."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError("invalid_payload", f"{key} is required.")
    return value.strip()


def optional_payload_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Return an optional dict payload field."""
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ScenarioError("invalid_payload", f"{key} must be an object.")
    return value


def require_person(payload: dict[str, Any], *, name_key: str, id_key: str) -> dict[str, str]:
    """Require a business person identity from payload."""
    name = require_payload_text(payload, name_key)
    person_id = require_payload_text(payload, id_key)
    return {
        "name": name,
        "id": person_id,
        "label": format_person_label(name, person_id),
    }


def format_person_label(name: str, person_id: str) -> str:
    """Format a business identity for notifications and views."""
    return f"{name}(ID: {person_id})"


def parse_business_datetime(value: str) -> datetime:
    """Parse the scenario business datetime format."""
    try:
        parsed = datetime.strptime(value, "%Y/%m/%d:%H:%M")
    except ValueError as exc:
        raise ScenarioError(
            "invalid_payload",
            f"last_working_day must use YYYY/MM/DD:HH:MM, got: {value}",
        ) from exc
    return parsed.replace(tzinfo=SCENARIO_TZ)


def require_close_conditions(case: ScenarioCase) -> None:
    """Validate all close conditions before HR signoff."""
    step_2 = case.steps.step_2
    step_3 = case.steps.step_3
    if not step_2.hr_confirmed:
        raise ScenarioError("close_condition_missing", "HR confirmation is missing.")
    if not step_3.tl_done:
        raise ScenarioError("close_condition_missing", "TL handover is missing.")
    if not step_3.ops_done:
        raise ScenarioError("close_condition_missing", "Ops recovery is missing.")
    if case.phase != CasePhase.AWAITING_HR_SIGNOFF:
        raise ScenarioError("invalid_phase", "Case is not awaiting HR signoff.")


def deadline_exceeded(deadline: int | None, now: datetime) -> bool:
    """Return whether a Unix timestamp deadline is exceeded."""
    if deadline is None:
        return False
    return deadline <= int(now.timestamp())


def reminder_due(
    last_reminded_at: int | None,
    now: datetime,
    interval_minutes: int,
) -> bool:
    """Return whether a routine reminder should be sent."""
    if last_reminded_at is None:
        return True
    next_due = last_reminded_at + int(timedelta(minutes=interval_minutes).total_seconds())
    return next_due <= timestamp(now)
