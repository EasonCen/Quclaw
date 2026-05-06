"""Controlled scenario notification tool for admin archive delivery."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

from runtime.events import EventSource, OutboundEvent
from scenario import rules
from scenario.model import CaseStatus, NotificationIntent, ScenarioRole
from scenario.orchestrator import ScenarioOrchestrator
from tools.base import BaseTool, tool
from tools.scenario_tool import (
    _auto_register_config_sources,
    _auto_register_source,
    _empty_delivery_report,
    _resolve_notification_sources,
)

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.context import SharedContext


SCENARIO_NOTIFY_TARGETS = {
    ScenarioRole.ADMIN,
    ScenarioRole.EMPLOYEE,
    ScenarioRole.HR,
}


def create_scenario_notify_tool(
    context: "SharedContext",
    source: EventSource | None = None,
) -> BaseTool | None:
    """Create the scenario_notify tool."""
    if source is not None and not _is_admin_source(source):
        return None

    scenario_config = getattr(context.config, "scenario", None)
    reminder_interval_minutes = getattr(scenario_config, "reminder_interval_minutes", 1)
    orchestrator = ScenarioOrchestrator(
        context.config.workspace,
        reminder_interval_minutes=reminder_interval_minutes,
    )

    @tool(
        name="scenario_notify",
        description=(
            "Admin-only controlled notification tool for resignation scenario "
            "archive delivery. It can send a full archive report to admin, or "
            "sanitized summaries to employee or HR for a specific case_id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_role": {
                    "type": "string",
                    "description": "Notification target role.",
                    "enum": sorted(role.value for role in SCENARIO_NOTIFY_TARGETS),
                },
                "case_id": {
                    "type": "string",
                    "description": "Closed scenario case id.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Archive content to send. Use a full report only for admin; "
                        "use sanitized summaries for employee or HR."
                    ),
                },
            },
            "required": ["target_role", "case_id", "content"],
            "additionalProperties": False,
        },
    )
    async def scenario_notify(
        session: "AgentSession",
        target_role: str,
        case_id: str,
        content: str,
    ) -> str:
        """Publish a controlled archive notification from admin."""
        try:
            _auto_register_config_sources(orchestrator, context.config)
            _auto_register_source(orchestrator, session.source)
            actor_role = rules.role_from_source(session.source)
            rules.require_role(actor_role, {ScenarioRole.ADMIN}, "scenario_notify")
            target = _require_target_role(target_role)
            case_id_value = _require_text(case_id, "case_id")
            message = _require_text(content, "content")
            case = orchestrator.store.get_case(case_id_value)
            if case is None:
                return _tool_result_json(
                    ok=False,
                    code="case_not_found",
                    message=f"Case not found: {case_id_value}",
                )
            if case.status != CaseStatus.CLOSED:
                return _tool_result_json(
                    ok=False,
                    code="invalid_phase",
                    message=f"Case is not closed: {case_id_value}",
                )

            archive_path = _save_archive_document(orchestrator, case_id_value, target, message)
            notification = NotificationIntent(target_role=target, content=message)
            delivery = await _publish_notification(context, session, orchestrator, notification, case_id_value, case)
            return _tool_result_json(
                ok=True,
                code="notification_sent",
                message="Notification delivery attempted.",
                case_id=case_id_value,
                archive_path=archive_path,
                delivery=delivery,
            )
        except rules.ScenarioError as exc:
            return _tool_result_json(ok=False, code=exc.code, message=exc.message)
        except ValueError as exc:
            return _tool_result_json(ok=False, code="invalid_payload", message=str(exc))
        except Exception as exc:
            return _tool_result_json(ok=False, code="scenario_notify_error", message=str(exc))

    return scenario_notify


def _is_admin_source(source: EventSource) -> bool:
    try:
        return rules.role_from_source(source) == ScenarioRole.ADMIN
    except rules.ScenarioError:
        return False


def _require_target_role(value: str) -> ScenarioRole:
    try:
        role = ScenarioRole(str(value))
    except ValueError as exc:
        raise ValueError(f"Unsupported target_role: {value}") from exc
    if role not in SCENARIO_NOTIFY_TARGETS:
        raise ValueError(f"Unsupported target_role: {value}")
    return role


def _require_text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required.")
    return value.strip()


def _save_archive_document(
    orchestrator: ScenarioOrchestrator,
    case_id: str,
    target: ScenarioRole,
    content: str,
) -> str | None:
    name_by_role = {
        ScenarioRole.ADMIN: "admin_full_report.md",
        ScenarioRole.EMPLOYEE: "employee_summary.md",
        ScenarioRole.HR: "hr_summary.md",
    }
    name = name_by_role[target]
    save = getattr(orchestrator.store, "save_archive_document", None)
    if save is None:
        return None
    return str(save(case_id, name, content))


async def _publish_notification(
    context: "SharedContext",
    session: "AgentSession",
    orchestrator: ScenarioOrchestrator,
    notification: NotificationIntent,
    case_id: str,
    case: Any,
) -> dict[str, Any]:
    report = _empty_delivery_report()
    if not hasattr(context, "eventbus"):
        report["errors"].append(
            {
                "target_role": notification.target_role.value,
                "source": None,
                "error": "eventbus is not available",
            }
        )
        return report

    sources = _resolve_notification_sources(orchestrator, notification, context.config, case)
    if not sources:
        report["skipped"].append(
            {
                "target_role": notification.target_role.value,
                "case_id": case_id,
                "reason": "no registered source",
            }
        )
        return report

    for source in sources:
        try:
            await context.eventbus.publish(
                OutboundEvent(
                    session_id=session.session_id,
                    content=notification.content,
                    source=source,
                )
            )
            report["published"] += 1
        except Exception as exc:
            report["errors"].append(
                {
                    "target_role": notification.target_role.value,
                    "source": str(source),
                    "error": str(exc),
                }
            )
    return report


def _tool_result_json(
    *,
    ok: bool,
    code: str,
    message: str,
    case_id: str | None = None,
    archive_path: str | None = None,
    delivery: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "code": code,
            "message": message,
            "case_id": case_id,
            "archive_path": archive_path,
            "delivery": delivery or _empty_delivery_report(),
        },
        ensure_ascii=False,
    )
