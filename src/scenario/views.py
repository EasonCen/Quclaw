"""Role-specific scenario views."""

from __future__ import annotations

from typing import Any

from scenario.model import ScenarioCase, ScenarioRole


def build_archive_view(case: ScenarioCase) -> dict[str, Any]:
    """Build the full closed-case archive view for admin."""
    view = case.model_dump(mode="json")
    view["responsible_summary"] = _responsible_summary(case)
    view["handover_summary"] = {
        "tl_done": case.steps.step_3.tl_done,
        "tl_summary": case.steps.step_3.tl_summary,
        "tl_completed_at": case.steps.step_3.tl_completed_at,
        "ops_done": case.steps.step_3.ops_done,
        "ops_recovery_data": case.steps.step_3.ops_recovery_data,
        "ops_completed_at": case.steps.step_3.ops_completed_at,
    }
    return view


def build_status_view(case: ScenarioCase, role: ScenarioRole) -> dict[str, Any]:
    """Build a role-specific status view."""
    if role == ScenarioRole.ADMIN:
        view = case.model_dump(mode="json")
        view["responsible_summary"] = _responsible_summary(case)
        return view

    step_2 = case.steps.step_2
    step_3 = case.steps.step_3
    base: dict[str, Any] = {
        "case_id": case.case_id,
        "status": case.status.value,
        "phase": case.phase.value,
        "employee": case.employee.get("label"),
    }
    if role == ScenarioRole.EMPLOYEE:
        return {
            **base,
            "current_step": case.phase.value,
            "last_working_day": step_2.last_working_day,
            "next_action": _employee_next_action(case),
        }
    if role == ScenarioRole.HR:
        return {
            **base,
            "hr_confirmed": step_2.hr_confirmed,
            "last_working_day": step_2.last_working_day,
            "hr_responsible": _responsible_label(case, ScenarioRole.HR),
            "tl_responsible": _responsible_label(case, ScenarioRole.TL),
            "ops_responsible": _responsible_label(case, ScenarioRole.OPS),
            "tl_done": step_3.tl_done,
            "tl_summary": step_3.tl_summary,
            "ops_done": step_3.ops_done,
            "needs_manual_followup": list(case.escalations),
        }
    if role == ScenarioRole.TL:
        return {
            **base,
            "task": "填写交接摘要",
            "last_working_day": step_2.last_working_day,
            "tl_responsible": _responsible_label(case, ScenarioRole.TL),
            "tl_done": step_3.tl_done,
            "tl_summary": step_3.tl_summary,
        }
    if role == ScenarioRole.OPS:
        return {
            **base,
            "task": "权限与资产回收",
            "last_working_day": step_2.last_working_day,
            "ops_responsible": _responsible_label(case, ScenarioRole.OPS),
            "ops_done": step_3.ops_done,
            "ops_recovery_data": step_3.ops_recovery_data,
        }
    return base


def build_audit_view(case: ScenarioCase) -> dict[str, Any]:
    """Return audit log for admin only."""
    return {
        "case_id": case.case_id,
        "audit_log": [entry.model_dump(mode="json") for entry in case.audit_log],
    }


def _employee_next_action(case: ScenarioCase) -> str:
    if case.status.value == "closed":
        return "流程已关闭"
    if case.phase.value == "awaiting_hr_confirm":
        return "等待 HR 确认申请与最后工作时间"
    if case.phase.value == "handover_and_recovery":
        return "等待内部交接和权限回收"
    if case.phase.value == "awaiting_hr_signoff":
        return "等待 HR 最终结案"
    return "暂无"


def _responsible_summary(case: ScenarioCase) -> dict[str, str]:
    return {
        "hr": _responsible_label(case, ScenarioRole.HR),
        "tl": _responsible_label(case, ScenarioRole.TL),
        "ops": _responsible_label(case, ScenarioRole.OPS),
    }


def _responsible_label(case: ScenarioCase, role: ScenarioRole) -> str:
    responsible = case.responsible.get(role.value, {})
    return str(responsible.get("label") or "未记录")
