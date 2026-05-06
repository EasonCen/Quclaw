"""State mutation effects for the resignation scenario."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scenario.model import (
    AuditEntry,
    CasePhase,
    CaseStatus,
    CaseSteps,
    NotificationIntent,
    ScenarioCase,
    ScenarioRegistry,
    ScenarioRole,
    Step1,
    Step2,
    Step3,
    Step4,
    StepStatus,
)


SCENARIO_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def now() -> datetime:
    """Return the scenario business time in China Standard Time."""
    return datetime.now(SCENARIO_TZ)


def timestamp(dt: datetime) -> int:
    """Serialize datetime as a Unix timestamp in seconds."""
    return int(dt.timestamp())


def format_timestamp(ts: int) -> str:
    """Format a Unix timestamp for China business users."""
    return datetime.fromtimestamp(ts, SCENARIO_TZ).strftime("%Y/%m/%d:%H:%M:%S")


def format_business_minute(ts: int) -> str:
    """Format a business datetime field without seconds."""
    return datetime.fromtimestamp(ts, SCENARIO_TZ).strftime("%Y/%m/%d:%H:%M")


def create_case(
    *,
    case_id: str,
    employee_source: str,
    employee_person: dict[str, str],
    registry: ScenarioRegistry,
    now: datetime,
) -> ScenarioCase:
    """Create a new case in the initial state-machine phase."""
    now_ts = timestamp(now)
    case = ScenarioCase(
        case_id=case_id,
        phase=CasePhase.AWAITING_HR_CONFIRM,
        status=CaseStatus.ACTIVE,
        created_at=now_ts,
        employee={**employee_person, "source": employee_source},
        role_sources={role: list(sources) for role, sources in registry.role_sources.items()},
        steps=CaseSteps(
            step_1=Step1(completed_at=now_ts),
            step_2=Step2(deadline=timestamp(now + timedelta(hours=24))),
            step_3=Step3(),
            step_4=Step4(),
        ),
    )
    append_audit(
        case,
        actor_role=ScenarioRole.EMPLOYEE,
        source=employee_source,
        event="init_case",
        data={"employee": employee_person},
        now=now,
    )
    return case


def append_audit(
    case: ScenarioCase,
    *,
    actor_role: ScenarioRole,
    source: str,
    event: str,
    data: dict[str, Any],
    now: datetime,
) -> None:
    """Append a business audit entry."""
    case.audit_log.append(
        AuditEntry(
            ts=timestamp(now),
            actor_role=actor_role,
            source=source,
            event=event,
            data=data,
        )
    )


def notify(target_role: ScenarioRole, content: str) -> NotificationIntent:
    """Build a role-targeted notification intent."""
    return NotificationIntent(target_role=target_role, content=content)


def case_initialized_notifications(case: ScenarioCase) -> list[NotificationIntent]:
    """Build notifications emitted when an employee case is created."""
    return [
        notify(
            ScenarioRole.HR,
            "\n".join(
                [
                    "收到新的试用期主动离职申请。",
                    "",
                    f"申请单号：{case.case_id}",
                    f"员工：{_employee_label(case)}",
                    f"创建时间：{format_timestamp(case.created_at)}",
                    "",
                    "请在 24 小时内确认申请并填写最后工作时间。",
                ]
            ),
        ),
        notify(
            ScenarioRole.ADMIN,
            _admin_status_notification(
                case,
                title="后台记录：新的离职申请已创建。",
                next_action="等待 HR 确认申请与最后工作时间。",
            ),
        ),
    ]


def on_hr_confirm(
    case: ScenarioCase,
    *,
    last_working_day: str,
    last_working_day_deadline: datetime,
    actor_person: dict[str, str],
    actor_role: ScenarioRole,
    source: str,
    now: datetime,
) -> list[NotificationIntent]:
    """Apply HR confirmation effects."""
    case.steps.step_2.status = StepStatus.DONE
    case.steps.step_2.hr_confirmed = True
    case.steps.step_2.last_working_day = last_working_day
    case.steps.step_2.completed_at = timestamp(now)
    case.steps.step_3.status = StepStatus.ACTIVE
    case.steps.step_3.deadline = timestamp(last_working_day_deadline)
    _record_responsible(case, actor_role, actor_person, source)
    append_audit(
        case,
        actor_role=actor_role,
        source=source,
        event="hr_confirm",
        data={
            "last_working_day": last_working_day,
            "deadline": case.steps.step_3.deadline,
            "actor": actor_person,
        },
        now=now,
    )
    return [
        notify(
            ScenarioRole.TL,
            _handover_notification(
                case,
                title="HR 已确认试用期主动离职申请，请完成交接摘要。",
                task="请在规定时间内提交交接摘要、接管人和未完成事项。",
            ),
        ),
        notify(
            ScenarioRole.OPS,
            _handover_notification(
                case,
                title="HR 已确认试用期主动离职申请，请完成权限与资产回收。",
                task="请在规定时间内完成账号、权限、设备和资产回收确认。",
            ),
        ),
        notify(
            ScenarioRole.ADMIN,
            _admin_status_notification(
                case,
                title="后台记录：HR 已确认离职申请。",
                next_action="等待 TL 完成交接摘要，等待 Ops 完成权限与资产回收。",
            ),
        ),
        notify(
            ScenarioRole.EMPLOYEE,
            _employee_status_notification(
                case,
                title="你的离职申请已由 HR 确认。",
                status="流程已进入内部交接与权限/资产回收阶段。",
                next_action="请等待 TL 与 Ops 完成内部处理。",
            ),
        ),
    ]


def on_tl_done(
    case: ScenarioCase,
    *,
    tl_summary: str,
    actor_person: dict[str, str],
    actor_role: ScenarioRole,
    source: str,
    now: datetime,
    advance: bool,
) -> list[NotificationIntent]:
    """Apply TL handover completion effects."""
    case.steps.step_3.tl_done = True
    case.steps.step_3.tl_summary = tl_summary
    case.steps.step_3.tl_completed_at = timestamp(now)
    _record_responsible(case, actor_role, actor_person, source)
    append_audit(
        case,
        actor_role=actor_role,
        source=source,
        event="tl_done",
        data={"tl_summary": tl_summary, "actor": actor_person},
        now=now,
    )
    if not advance:
        return [
            notify(
                ScenarioRole.EMPLOYEE,
                _employee_status_notification(
                    case,
                    title="TL 已完成交接确认。",
                    status="当前仍在等待 Ops 完成权限与资产回收。",
                    next_action="请继续等待内部处理完成。",
                ),
            )
        ]
    return _advance_to_hr_signoff(case, now)


def on_ops_done(
    case: ScenarioCase,
    *,
    recovery_data: dict[str, Any],
    actor_person: dict[str, str],
    actor_role: ScenarioRole,
    source: str,
    now: datetime,
    advance: bool,
) -> list[NotificationIntent]:
    """Apply Ops recovery completion effects."""
    case.steps.step_3.ops_done = True
    case.steps.step_3.ops_recovery_data = recovery_data
    case.steps.step_3.ops_completed_at = timestamp(now)
    _record_responsible(case, actor_role, actor_person, source)
    append_audit(
        case,
        actor_role=actor_role,
        source=source,
        event="ops_done",
        data={"recovery_data": recovery_data, "actor": actor_person},
        now=now,
    )
    if not advance:
        return [
            notify(
                ScenarioRole.EMPLOYEE,
                _employee_status_notification(
                    case,
                    title="Ops 已完成权限与资产回收确认。",
                    status="当前仍在等待 TL 完成交接确认。",
                    next_action="请继续等待内部处理完成。",
                ),
            )
        ]
    return _advance_to_hr_signoff(case, now)


def _advance_to_hr_signoff(case: ScenarioCase, now: datetime) -> list[NotificationIntent]:
    """Set step 3 done and unlock HR signoff."""
    case.steps.step_3.status = StepStatus.DONE
    case.steps.step_3.completed_at = timestamp(now)
    case.steps.step_4.status = StepStatus.ACTIVE
    return [
        notify(ScenarioRole.HR, "交接与回收均已完成，请进行最终结案签核。"),
        notify(
            ScenarioRole.ADMIN,
            _admin_status_notification(
                case,
                title="后台记录：TL 交接与 Ops 回收均已完成。",
                next_action="等待 HR 最终结案签核。",
            ),
        ),
        notify(
            ScenarioRole.EMPLOYEE,
            _employee_status_notification(
                case,
                title="内部交接与回收已完成。",
                status="流程已进入 HR 最终结案签核阶段。",
                next_action="请等待 HR 完成最终结案。",
            ),
        ),
    ]


def on_hr_sign(
    case: ScenarioCase,
    *,
    actor_role: ScenarioRole,
    source: str,
    now: datetime,
) -> dict[str, Any]:
    """Apply HR signoff and close the case."""
    case.steps.step_4.status = StepStatus.DONE
    case.steps.step_4.hr_signed = True
    case.steps.step_4.completed_at = timestamp(now)
    case.status = CaseStatus.CLOSED
    append_audit(
        case,
        actor_role=actor_role,
        source=source,
        event="hr_sign",
        data={},
        now=now,
    )
    return build_archive_payload(case)


def mark_timeout(
    case: ScenarioCase,
    *,
    target: ScenarioRole,
    source: str,
    now: datetime,
    overdue: bool,
) -> list[NotificationIntent]:
    """Apply TL/Ops routine reminder or overdue escalation effects."""
    step_3 = case.steps.step_3
    notifications: list[NotificationIntent] = []
    if target == ScenarioRole.TL:
        step_3.tl_reminder_count += 1
        step_3.tl_last_reminded_at = timestamp(now)
        count = step_3.tl_reminder_count
        event = "tl_timeout" if overdue else "tl_reminder"
        notifications.append(notify(ScenarioRole.TL, "请尽快补充交接摘要。"))
        if overdue or count >= 3:
            step_3.tl_escalated = True
            _record_escalation(case, "tl_timeout")
            notifications.extend(
                _escalation_notifications(
                    "TL 交接信息超时未完成。"
                    if overdue
                    else "TL 交接信息已提醒 3 次以上仍未完成。"
                )
            )
    else:
        step_3.ops_reminder_count += 1
        step_3.ops_last_reminded_at = timestamp(now)
        count = step_3.ops_reminder_count
        event = "ops_timeout" if overdue else "ops_reminder"
        notifications.append(notify(ScenarioRole.OPS, "请尽快完成权限与资产回收。"))
        if overdue or count >= 3:
            step_3.ops_escalated = True
            _record_escalation(case, "ops_timeout")
            notifications.extend(
                _escalation_notifications(
                    "运维权限与资产回收超时未完成。"
                    if overdue
                    else "运维权限与资产回收已提醒 3 次以上仍未完成。"
                )
            )

    append_audit(
        case,
        actor_role=ScenarioRole.SYSTEM,
        source=source,
        event=event,
        data={"reminder_count": count, "overdue": overdue},
        now=now,
    )
    return notifications


def _record_escalation(case: ScenarioCase, escalation: str) -> None:
    case.escalated = True
    if escalation not in case.escalations:
        case.escalations.append(escalation)


def _record_responsible(
    case: ScenarioCase,
    actor_role: ScenarioRole,
    actor_person: dict[str, str],
    source: str,
) -> None:
    case.responsible[actor_role.value] = {**actor_person, "source": source}


def _escalation_notifications(content: str) -> list[NotificationIntent]:
    return [
        notify(ScenarioRole.HR, f"需要人工跟进：{content}"),
    ]


def build_archive_payload(case: ScenarioCase) -> dict[str, Any]:
    """Build archive payload for the archive agent."""
    return case.model_dump(mode="json")


def case_closed_notifications(case: ScenarioCase) -> list[NotificationIntent]:
    """Build notifications emitted when HR closes the case."""
    return [
        notify(
            ScenarioRole.EMPLOYEE,
            _employee_status_notification(
                case,
                title="你的离职流程已结案。",
                status="HR 已完成最终签核，流程状态已关闭。",
                next_action="",
            ),
        ),
        notify(
            ScenarioRole.ADMIN,
            _admin_status_notification(
                case,
                title="后台记录：离职流程已结案。",
                next_action="流程已关闭，可查看审计日志或归档记录。",
            ),
        ),
    ]


def _employee_status_notification(
    case: ScenarioCase,
    *,
    title: str,
    status: str,
    next_action: str | None,
) -> str:
    lines = [
        title,
        "",
        f"申请单号：{case.case_id}",
        f"员工：{_employee_label(case)}",
        f"创建时间：{format_timestamp(case.created_at)}",
        f"最后工作时间：{case.steps.step_2.last_working_day or '未确认'}",
        f"当前阶段：{case.phase.value}",
        "",
        status,
    ]
    if next_action:
        lines.append(f"下一步：{next_action}")
    return "\n".join(lines)


def _admin_status_notification(
    case: ScenarioCase,
    *,
    title: str,
    next_action: str,
) -> str:
    return "\n".join(
        [
            title,
            "",
            f"申请单号：{case.case_id}",
            f"当前阶段：{case.phase.value}",
            f"员工：{_employee_label(case)}",
            f"创建时间：{format_timestamp(case.created_at)}",
            f"最后工作时间：{case.steps.step_2.last_working_day or '未确认'}",
            f"截止时间：{_optional_timestamp(case.steps.step_3.deadline)}",
            f"HR 负责：{_responsible_label(case, ScenarioRole.HR)}",
            f"TL 负责：{_responsible_label(case, ScenarioRole.TL)}",
            f"Ops 负责：{_responsible_label(case, ScenarioRole.OPS)}",
            f"TL 交接：{'已完成' if case.steps.step_3.tl_done else '未完成'}",
            f"Ops 回收：{'已完成' if case.steps.step_3.ops_done else '未完成'}",
            "",
            f"下一步：{next_action}",
        ]
    )


def _handover_notification(case: ScenarioCase, *, title: str, task: str) -> str:
    return "\n".join(
        [
            title,
            "",
            f"申请单号：{case.case_id}",
            f"员工：{_employee_label(case)}",
            f"创建时间：{format_timestamp(case.created_at)}",
            f"最后工作时间：{case.steps.step_2.last_working_day}",
            f"HR 确认时间：{_optional_timestamp(case.steps.step_2.completed_at)}",
            "",
            task,
        ]
    )


def _optional_timestamp(ts: int | None) -> str:
    if ts is None:
        return "未记录"
    return format_timestamp(ts)


def _employee_label(case: ScenarioCase) -> str:
    return str(case.employee.get("label") or case.employee.get("source") or "未记录")


def _responsible_label(case: ScenarioCase, role: ScenarioRole) -> str:
    responsible = case.responsible.get(role.value, {})
    return str(responsible.get("label") or "未记录")
