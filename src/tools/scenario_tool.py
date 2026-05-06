"""Scenario engine tool factory."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

from runtime.events import DispatchEvent, EventSource, OutboundEvent
from scenario.model import NotificationIntent, ScenarioCase, ScenarioResult, ScenarioRole
from scenario.orchestrator import ScenarioOrchestrator
from tools.base import BaseTool, tool

if TYPE_CHECKING:
    from core.agent import AgentSession
    from core.context import SharedContext


SCENARIO_ACTIONS = {
    "register_source",
    "set_pending_intent",
    "get_pending_intent",
    "cancel_pending_intent",
    "init_case",
    "complete_task",
    "get_status",
    "get_audit_log",
    "get_archive_view",
    "scan_timeouts",
}


def create_scenario_tool(context: "SharedContext") -> BaseTool:
    """Create the scenario_engine tool."""
    scenario_config = getattr(context.config, "scenario", None)
    reminder_interval_minutes = getattr(scenario_config, "reminder_interval_minutes", 1)
    orchestrator = ScenarioOrchestrator(
        context.config.workspace,
        reminder_interval_minutes=reminder_interval_minutes,
    )

    @tool(
        name="scenario_engine",
        description=(
            "Call the resignation scenario engine. The tool uses the current "
            "session source as the trusted actor; do not include source or role "
            "in payload."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Scenario action to perform.",
                    "enum": sorted(SCENARIO_ACTIONS),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Business payload for the selected action. init_case requires "
                        "employee_name and employee_id. complete_task hr_confirm/tl_done/"
                        "ops_done require actor_name and actor_id; hr_confirm also "
                        "requires last_working_day in YYYY/MM/DD:HH:MM format, "
                        "tl_done requires tl_summary. "
                        "get_status can include case_id to query a closed case."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    async def scenario_engine(
        session: "AgentSession",
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Call ScenarioOrchestrator with the session source."""
        _auto_register_config_sources(orchestrator, context.config)
        _auto_register_source(orchestrator, session.source)
        result = orchestrator.handle(action, payload or {}, session.source)
        delivery = await _publish_notifications(context, session, orchestrator, result)
        await _dispatch_archive_job(context, session, orchestrator, result)
        return _tool_result_json(result, delivery)

    return scenario_engine


def _tool_result_json(
    result: ScenarioResult,
    delivery: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "scenario": result.model_dump(mode="json"),
            "delivery": delivery or _empty_delivery_report(),
        },
        ensure_ascii=False,
    )


def _empty_delivery_report() -> dict[str, Any]:
    return {
        "published": 0,
        "skipped": [],
        "errors": [],
    }


def _auto_register_source(
    orchestrator: ScenarioOrchestrator,
    source: EventSource | str,
) -> None:
    """Register the current platform role source for future notifications."""
    role = _telegram_role_from_source(source)
    if role is None:
        return

    source_str = str(source)
    registry = orchestrator.store.load_registry()
    changed = _append_source(registry.role_sources, role, source_str)
    if changed:
        orchestrator.store.save_registry(registry)

    case = orchestrator.store.get_active_case()
    if case is None:
        return
    if _append_source(case.role_sources, role, source_str):
        orchestrator.store.save_case(case)


def _telegram_role_from_source(source: EventSource | str) -> ScenarioRole | None:
    source_str = str(source)
    if not source_str.startswith("platform-telegram:"):
        return None
    payload = source_str.split(":", 1)[1]
    if "/" not in payload:
        return None
    bot_key = payload.split("/", 1)[0]
    if bot_key == ScenarioRole.SYSTEM.value:
        return None
    return ScenarioRole(bot_key) if bot_key in ScenarioRole._value2member_map_ else None


def _append_source(
    role_sources: dict[str, list[str]],
    role: ScenarioRole,
    source: str,
) -> bool:
    sources = role_sources.setdefault(role.value, [])
    if source in sources:
        return False
    sources.append(source)
    return True


async def _publish_notifications(
    context: "SharedContext",
    session: "AgentSession",
    orchestrator: ScenarioOrchestrator,
    result: ScenarioResult,
) -> dict[str, Any]:
    """Publish generated notifications as outbound events."""
    report = _empty_delivery_report()
    if not result.ok or not result.notifications:
        return report

    for notification in result.notifications:
        sources = _resolve_notification_sources(orchestrator, notification, context.config)
        if not sources:
            report["skipped"].append(
                {
                    "target_role": notification.target_role.value,
                    "reason": "no registered source",
                }
            )
            continue

        for source in sources:
            await context.eventbus.publish(
                OutboundEvent(
                    session_id=session.session_id,
                    content=notification.content,
                    source=source,
                )
            )
            report["published"] += 1

    return report


async def _dispatch_archive_job(
    context: "SharedContext",
    session: "AgentSession",
    orchestrator: ScenarioOrchestrator,
    result: ScenarioResult,
) -> None:
    """Dispatch closed-case archive work to the admin agent."""
    if not result.ok or result.archive_payload is None or not result.case_id:
        return

    admin_sources = _resolve_notification_sources(
        orchestrator,
        NotificationIntent(target_role=ScenarioRole.ADMIN, content=""),
        context.config,
        orchestrator.store.get_case(result.case_id),
    )
    if not admin_sources:
        return

    event = DispatchEvent(
        session_id=f"scenario-archive-{result.case_id}",
        content=_archive_dispatch_prompt(result.case_id),
        source=admin_sources[0],
        target_agent_id="admin",
        parent_session_id=session.session_id,
    )
    await context.eventbus.publish(event)


def _archive_dispatch_prompt(case_id: str) -> str:
    return "\n".join(
        [
            "检测到离职流程已结案，请立即执行归档总结流程。",
            "",
            f"申请单号：{case_id}",
            "",
            "请调用 scenario_engine 的 get_archive_view 读取完整归档视图。",
            "然后生成一份完整未脱敏归档报告，并调用 scenario_notify，target_role=\"admin\"，发给 admin 窗口并持久化归档。",
            "最后生成一份脱敏归档摘要，并分别调用 scenario_notify，target_role=\"employee\" 和 target_role=\"hr\"，发给对应窗口并持久化归档。",
            "脱敏摘要不得包含 platform source、raw audit source、内部 registry 或完整原始 JSON。",
        ]
    )


def _resolve_notification_sources(
    orchestrator: ScenarioOrchestrator,
    notification: NotificationIntent,
    config: Any,
    case: ScenarioCase | None = None,
) -> list[EventSource]:
    source_values: list[str] = []
    case = case or orchestrator.store.get_active_case()
    if case is not None:
        source_values.extend(case.role_sources.get(notification.target_role.value, []))

    registry = orchestrator.store.load_registry()
    for source in registry.role_sources.get(notification.target_role.value, []):
        if source not in source_values:
            source_values.append(source)

    config_sources = _configured_role_sources(config)
    for source in config_sources.get(notification.target_role, []):
        if source not in source_values:
            source_values.append(source)

    sources: list[EventSource] = []
    for source_value in source_values:
        sources.append(EventSource.from_string(source_value))
    return sources


def _auto_register_config_sources(
    orchestrator: ScenarioOrchestrator,
    config: Any,
) -> None:
    """Pre-register configured role sources from Telegram routing settings."""
    configured_sources = _configured_role_sources(config)
    if not configured_sources:
        return

    registry = orchestrator.store.load_registry()
    registry_changed = False
    for role, sources in configured_sources.items():
        for source in sources:
            registry_changed = _append_source(registry.role_sources, role, source) or registry_changed
    if registry_changed:
        orchestrator.store.save_registry(registry)

    case = orchestrator.store.get_active_case()
    if case is None:
        return

    case_changed = False
    for role, sources in configured_sources.items():
        for source in sources:
            case_changed = _append_source(case.role_sources, role, source) or case_changed
    if case_changed:
        orchestrator.store.save_case(case)


def _configured_role_sources(config: Any) -> dict[ScenarioRole, list[str]]:
    """Infer role notification targets from Telegram routing and allowed users."""
    channels = getattr(config, "channels", None)
    telegram = getattr(channels, "telegram", None)
    if telegram is None:
        return {}

    role_sources: dict[ScenarioRole, list[str]] = {}
    for bot_key, bot_config in telegram.normalized_bots.items():
        role = ScenarioRole(bot_key) if bot_key in ScenarioRole._value2member_map_ else None
        if role is None or role == ScenarioRole.SYSTEM:
            continue
        if not bot_config.enabled:
            continue

        for user_id in bot_config.allowed_user_ids:
            source = f"platform-telegram:{role.value}/{user_id}"
            sources = role_sources.setdefault(role, [])
            if source not in sources:
                sources.append(source)
    return role_sources
