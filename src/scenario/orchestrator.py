"""Scenario orchestration entrypoint.

This module intentionally coordinates storage, rules, state-machine events,
and effects. Business decisions live in the imported rules/effects/state
modules.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from statemachine.exceptions import TransitionNotAllowed

from runtime.events import EventSource
from scenario import effects, rules
from scenario.model import (
    CasePhase,
    CaseStatus,
    PendingIntent,
    ScenarioResult,
    ScenarioRole,
    TaskType,
)
from scenario.state import ResignationCaseMachine
from scenario.store import ScenarioStore
from scenario.views import build_archive_view, build_audit_view, build_status_view


class ScenarioOrchestrator:
    """Coordinate scenario actions without embedding business rules."""

    def __init__(self, workspace: Path, *, reminder_interval_minutes: int = 1) -> None:
        self.store = ScenarioStore(workspace)
        self.reminder_interval_minutes = reminder_interval_minutes

    def handle(
        self,
        action: str,
        payload: dict[str, Any] | None,
        source: EventSource | str,
    ) -> ScenarioResult:
        """Handle one scenario action."""
        payload = payload or {}
        source_str = str(source)
        try:
            role = rules.role_from_source(source)
            if action == "register_source":
                return self._register_source(role, source_str)
            if action == "set_pending_intent":
                return self._set_pending_intent(role, source_str)
            if action == "get_pending_intent":
                return self._get_pending_intent(role, source_str)
            if action == "cancel_pending_intent":
                return self._cancel_pending_intent(role, source_str)
            if action == "init_case":
                return self._init_case(role, source_str, payload)
            if action == "complete_task":
                return self._complete_task(role, source_str, payload)
            if action == "get_status":
                return self._get_status(role, source_str, payload)
            if action == "get_audit_log":
                return self._get_audit_log(role)
            if action == "get_archive_view":
                return self._get_archive_view(role, payload)
            if action == "scan_timeouts":
                return self._scan_timeouts(role, source_str)
            raise rules.ScenarioError("unknown_action", f"Unknown action: {action}")
        except rules.ScenarioError as exc:
            return ScenarioResult(
                ok=False,
                code=exc.code,
                message=exc.message,
                errors=[exc.message],
            )
        except TransitionNotAllowed as exc:
            return ScenarioResult(
                ok=False,
                code="invalid_transition",
                message=str(exc),
                errors=[str(exc)],
            )

    def _register_source(self, role: ScenarioRole, source: str) -> ScenarioResult:
        rules.require_role(
            role,
            {
                ScenarioRole.EMPLOYEE,
                ScenarioRole.HR,
                ScenarioRole.TL,
                ScenarioRole.OPS,
                ScenarioRole.ADMIN,
            },
            "register_source",
        )
        registry = self.store.load_registry()
        sources = registry.role_sources.setdefault(role.value, [])
        if source not in sources:
            sources.append(source)
        self.store.save_registry(registry)
        return ScenarioResult(ok=True, code="source_registered", message="Source registered.")

    def _set_pending_intent(self, role: ScenarioRole, source: str) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.EMPLOYEE}, "set_pending_intent")
        now = effects.now()
        registry = self.store.load_registry()
        registry.pending_intents[source] = PendingIntent(
            detected_at=effects.timestamp(now),
            expires_at=effects.timestamp(now + timedelta(minutes=5)),
        )
        self.store.save_registry(registry)
        return ScenarioResult(ok=True, code="pending_intent_set", message="Pending intent set.")

    def _get_pending_intent(self, role: ScenarioRole, source: str) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.EMPLOYEE}, "get_pending_intent")
        registry = self.store.load_registry()
        pending = registry.pending_intents.get(source)
        if pending and self._pending_expired(pending):
            registry.pending_intents.pop(source, None)
            self.store.save_registry(registry)
            return ScenarioResult(
                ok=True,
                code="pending_intent_expired",
                message="Pending intent expired.",
                view={"pending": None},
            )
        return ScenarioResult(
            ok=True,
            code="pending_intent",
            message="Pending intent found." if pending else "No pending intent.",
            view={"pending": pending.model_dump(mode="json") if pending else None},
        )

    def _cancel_pending_intent(self, role: ScenarioRole, source: str) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.EMPLOYEE}, "cancel_pending_intent")
        registry = self.store.load_registry()
        registry.pending_intents.pop(source, None)
        self.store.save_registry(registry)
        return ScenarioResult(ok=True, code="pending_intent_cancelled", message="Pending intent cancelled.")

    def _init_case(self, role: ScenarioRole, source: str, payload: dict[str, Any]) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.EMPLOYEE}, "init_case")
        rules.require_no_active_case(self.store.get_active_case())
        registry = self.store.load_registry()
        pending = registry.pending_intents.get(source)
        if pending is None:
            raise rules.ScenarioError(
                "pending_intent_required",
                "Pending resignation intent confirmation is required before init_case.",
            )
        if self._pending_expired(pending):
            registry.pending_intents.pop(source, None)
            self.store.save_registry(registry)
            raise rules.ScenarioError(
                "pending_intent_expired",
                "Pending resignation intent confirmation expired.",
            )
        employee_person = rules.require_person(
            payload,
            name_key="employee_name",
            id_key="employee_id",
        )
        now = effects.now()
        case = effects.create_case(
            case_id=self.store.next_case_id(),
            employee_source=source,
            employee_person=employee_person,
            registry=registry,
            now=now,
        )
        registry.pending_intents.pop(source, None)
        self.store.save_registry(registry)
        self.store.save_case(case)
        return ScenarioResult(
            ok=True,
            code="case_initialized",
            message="Case initialized.",
            case_id=case.case_id,
            notifications=effects.case_initialized_notifications(case),
        )

    @staticmethod
    def _pending_expired(pending: PendingIntent) -> bool:
        return pending.expires_at <= effects.timestamp(effects.now())

    def _complete_task(
        self,
        role: ScenarioRole,
        source: str,
        payload: dict[str, Any],
    ) -> ScenarioResult:
        case = rules.require_active_case(self.store.get_active_case())
        task_type = rules.task_type_from_payload(payload)
        machine = ResignationCaseMachine(case, state_field="phase")
        now = effects.now()
        notifications = []
        archive_payload = None

        if task_type == TaskType.HR_CONFIRM:
            rules.require_role(role, {ScenarioRole.HR}, task_type.value)
            last_working_day = rules.require_payload_text(payload, "last_working_day")
            actor_person = rules.require_person(payload, name_key="actor_name", id_key="actor_id")
            last_working_day_dt = rules.parse_business_datetime(last_working_day)
            normalized_last_working_day = effects.format_business_minute(
                effects.timestamp(last_working_day_dt)
            )
            machine.hr_confirm()
            notifications = effects.on_hr_confirm(
                case,
                last_working_day=normalized_last_working_day,
                last_working_day_deadline=last_working_day_dt,
                actor_person=actor_person,
                actor_role=role,
                source=source,
                now=now,
            )
        elif task_type == TaskType.TL_DONE:
            rules.require_role(role, {ScenarioRole.TL}, task_type.value)
            tl_summary = rules.require_payload_text(payload, "tl_summary")
            actor_person = rules.require_person(payload, name_key="actor_name", id_key="actor_id")
            advance = case.steps.step_3.ops_done
            machine.tl_done_ready() if advance else machine.tl_done_wait()
            notifications = effects.on_tl_done(
                case,
                tl_summary=tl_summary,
                actor_person=actor_person,
                actor_role=role,
                source=source,
                now=now,
                advance=advance,
            )
        elif task_type == TaskType.OPS_DONE:
            rules.require_role(role, {ScenarioRole.OPS}, task_type.value)
            recovery_data = rules.optional_payload_dict(payload, "recovery_data")
            actor_person = rules.require_person(payload, name_key="actor_name", id_key="actor_id")
            advance = case.steps.step_3.tl_done
            machine.ops_done_ready() if advance else machine.ops_done_wait()
            notifications = effects.on_ops_done(
                case,
                recovery_data=recovery_data,
                actor_person=actor_person,
                actor_role=role,
                source=source,
                now=now,
                advance=advance,
            )
        elif task_type == TaskType.HR_SIGN:
            rules.require_role(role, {ScenarioRole.HR}, task_type.value)
            rules.require_close_conditions(case)
            machine.hr_sign()
            archive_payload = effects.on_hr_sign(
                case,
                actor_role=role,
                source=source,
                now=now,
            )
            notifications = effects.case_closed_notifications(case)

        self.store.save_case(case)
        return ScenarioResult(
            ok=True,
            code=f"{task_type.value}_completed",
            message=f"{task_type.value} completed.",
            case_id=case.case_id,
            notifications=notifications,
            archive_payload=archive_payload,
        )

    def _get_status(self, role: ScenarioRole, source: str, payload: dict[str, Any]) -> ScenarioResult:
        rules.require_role(
            role,
            {ScenarioRole.EMPLOYEE, ScenarioRole.HR, ScenarioRole.TL, ScenarioRole.OPS, ScenarioRole.ADMIN},
            "get_status",
        )
        if payload.get("case_id"):
            case_id = rules.require_payload_text(payload, "case_id")
            case = self.store.get_case(case_id)
            if case is None:
                raise rules.ScenarioError("case_not_found", f"Case not found: {case_id}")
            if role == ScenarioRole.EMPLOYEE and case.employee.get("source") != source:
                raise rules.ScenarioError("permission_denied", f"Role {role.value} cannot view case {case_id}.")
        else:
            case = rules.require_active_case(self.store.get_active_case())
        return ScenarioResult(
            ok=True,
            code="status",
            message="Status loaded.",
            case_id=case.case_id,
            view=build_status_view(case, role),
        )

    def _get_audit_log(self, role: ScenarioRole) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.ADMIN}, "get_audit_log")
        case = rules.require_active_case(self.store.get_active_case())
        return ScenarioResult(
            ok=True,
            code="audit_log",
            message="Audit log loaded.",
            case_id=case.case_id,
            view=build_audit_view(case),
        )

    def _get_archive_view(self, role: ScenarioRole, payload: dict[str, Any]) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.ADMIN}, "get_archive_view")
        case_id = rules.require_payload_text(payload, "case_id")
        case = self.store.get_case(case_id)
        if case is None:
            raise rules.ScenarioError("case_not_found", f"Case not found: {case_id}")
        if case.status != CaseStatus.CLOSED:
            raise rules.ScenarioError("invalid_phase", f"Case is not closed: {case_id}")
        return ScenarioResult(
            ok=True,
            code="archive_view",
            message="Archive view loaded.",
            case_id=case.case_id,
            view=build_archive_view(case),
        )

    def _scan_timeouts(self, role: ScenarioRole, source: str) -> ScenarioResult:
        rules.require_role(role, {ScenarioRole.SYSTEM}, "scan_timeouts")
        case = rules.require_active_case(self.store.get_active_case())
        if case.phase != CasePhase.HANDOVER_AND_RECOVERY:
            return ScenarioResult(ok=True, code="no_timeouts", message="No timeout scan needed.", case_id=case.case_id)

        machine = ResignationCaseMachine(case, state_field="phase")
        now = effects.now()
        notifications = []
        reminder_interval = self.reminder_interval_minutes
        overdue = rules.deadline_exceeded(case.steps.step_3.deadline, now)
        if (
            not case.steps.step_3.tl_done
            and rules.reminder_due(case.steps.step_3.tl_last_reminded_at, now, reminder_interval)
        ):
            machine.mark_tl_timeout()
            notifications.extend(
                effects.mark_timeout(
                    case,
                    target=ScenarioRole.TL,
                    source=source,
                    now=now,
                    overdue=overdue,
                )
            )
        if (
            not case.steps.step_3.ops_done
            and rules.reminder_due(case.steps.step_3.ops_last_reminded_at, now, reminder_interval)
        ):
            machine.mark_ops_timeout()
            notifications.extend(
                effects.mark_timeout(
                    case,
                    target=ScenarioRole.OPS,
                    source=source,
                    now=now,
                    overdue=overdue,
                )
            )
        self.store.save_case(case)
        return ScenarioResult(
            ok=True,
            code="timeouts_scanned",
            message="Timeout scan completed.",
            case_id=case.case_id,
            notifications=notifications,
        )
