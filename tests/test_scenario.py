"""Tests for the resignation scenario state machine."""

from __future__ import annotations

from unittest import TestCase

from scenario.model import CasePhase, CaseStatus, PendingIntent, ScenarioCase, ScenarioRegistry
from scenario.orchestrator import ScenarioOrchestrator


EMPLOYEE = "platform-telegram:employee/1"
HR = "platform-telegram:hr/1"
TL = "platform-telegram:tl/1"
OPS = "platform-telegram:ops/1"
ADMIN = "platform-telegram:admin/1"
SYSTEM = "cron:resignation-monitor"


class InMemoryScenarioStore:
    def __init__(self) -> None:
        self.registry = ScenarioRegistry()
        self.cases: dict[str, ScenarioCase] = {}
        self.next_id = 1

    def load_registry(self) -> ScenarioRegistry:
        return self.registry

    def save_registry(self, registry: ScenarioRegistry) -> None:
        self.registry = registry

    def get_active_case(self) -> ScenarioCase | None:
        for case in self.cases.values():
            if case.status == CaseStatus.ACTIVE:
                return case
        return None

    def get_case(self, case_id: str) -> ScenarioCase | None:
        return self.cases.get(case_id)

    def save_case(self, case: ScenarioCase) -> None:
        self.cases[case.case_id] = case

    def next_case_id(self) -> str:
        case_id = f"RES-TEST-{self.next_id}"
        self.next_id += 1
        return case_id


class ScenarioOrchestratorTest(TestCase):
    def make_orchestrator(self):
        orchestrator = ScenarioOrchestrator.__new__(ScenarioOrchestrator)
        orchestrator.store = InMemoryScenarioStore()
        orchestrator.reminder_interval_minutes = 1
        return orchestrator

    def init_case(self, orchestrator: ScenarioOrchestrator):
        pending_result = orchestrator.handle("set_pending_intent", {}, EMPLOYEE)
        self.assertTrue(pending_result.ok, pending_result)
        result = orchestrator.handle("init_case", {"employee_name": "Alice", "employee_id": "E001"}, EMPLOYEE)
        self.assertTrue(result.ok, result)
        return orchestrator.store.get_active_case()

    def confirm_hr(self, orchestrator: ScenarioOrchestrator):
        return orchestrator.handle(
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/15:18:00", "actor_name": "Alice", "actor_id": "HR001"},
            HR,
        )

    def test_init_case_starts_awaiting_hr_confirm(self):
        orchestrator = self.make_orchestrator()
        case = self.init_case(orchestrator)
        self.assertEqual(case.phase, CasePhase.AWAITING_HR_CONFIRM)
        self.assertEqual(case.employee["label"], "Alice(ID: E001)")

    def test_hr_confirm_moves_to_handover_and_recovery(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        result = self.confirm_hr(orchestrator)
        self.assertTrue(result.ok, result)
        case = orchestrator.store.get_active_case()
        self.assertEqual(case.phase, CasePhase.HANDOVER_AND_RECOVERY)
        self.assertEqual(case.steps.step_2.last_working_day, "2026/05/15:18:00")

    def test_tl_then_ops_moves_to_hr_signoff(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)

        tl_result = orchestrator.handle(
            "complete_task",
            {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"},
            TL,
        )
        self.assertTrue(tl_result.ok, tl_result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.HANDOVER_AND_RECOVERY)

        ops_result = orchestrator.handle(
            "complete_task",
            {"task_type": "ops_done", "recovery_data": {"github": "done"}, "actor_name": "Alice", "actor_id": "OPS001"},
            OPS,
        )
        self.assertTrue(ops_result.ok, ops_result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.AWAITING_HR_SIGNOFF)

    def test_ops_then_tl_moves_to_hr_signoff(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)

        ops_result = orchestrator.handle("complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"}, OPS)
        self.assertTrue(ops_result.ok, ops_result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.HANDOVER_AND_RECOVERY)

        tl_result = orchestrator.handle(
            "complete_task",
            {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"},
            TL,
        )
        self.assertTrue(tl_result.ok, tl_result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.AWAITING_HR_SIGNOFF)

    def test_ops_done_rejects_non_object_recovery_data(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)

        result = orchestrator.handle(
            "complete_task",
            {
                "task_type": "ops_done",
                "recovery_data": "done",
                "actor_name": "Alice",
                "actor_id": "OPS001",
            },
            OPS,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_payload")

    def test_hr_sign_closes_case(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        orchestrator.handle("complete_task", {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"}, TL)
        orchestrator.handle("complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"}, OPS)

        result = orchestrator.handle("complete_task", {"task_type": "hr_sign"}, HR)
        self.assertTrue(result.ok, result)
        case = next(iter(orchestrator.store.cases.values()))
        self.assertEqual(case.phase, CasePhase.CLOSED)
        self.assertEqual(case.status, CaseStatus.CLOSED)
        self.assertIsNotNone(result.archive_payload)
        self.assertEqual(
            [notification.target_role.value for notification in result.notifications],
            ["employee", "admin"],
        )
        self.assertNotIn("下一步：", result.notifications[0].content)
        self.assertIn("下一步：", result.notifications[1].content)

    def test_all_roles_can_get_closed_case_status_by_case_id(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        orchestrator.handle("complete_task", {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"}, TL)
        orchestrator.handle("complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"}, OPS)
        close_result = orchestrator.handle("complete_task", {"task_type": "hr_sign"}, HR)

        for source in (EMPLOYEE, HR, TL, OPS, ADMIN):
            result = orchestrator.handle("get_status", {"case_id": close_result.case_id}, source)

            self.assertTrue(result.ok, result)
            self.assertEqual(result.view["status"], "closed")
            self.assertEqual(result.view["phase"], "closed")

    def test_admin_get_archive_view_for_closed_case(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        orchestrator.handle("complete_task", {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"}, TL)
        orchestrator.handle("complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"}, OPS)
        close_result = orchestrator.handle("complete_task", {"task_type": "hr_sign"}, HR)

        result = orchestrator.handle("get_archive_view", {"case_id": close_result.case_id}, ADMIN)

        self.assertTrue(result.ok, result)
        self.assertEqual(result.code, "archive_view")
        self.assertEqual(result.view["case_id"], close_result.case_id)
        self.assertIn("audit_log", result.view)
        self.assertEqual(result.view["responsible_summary"]["tl"], "Alice(ID: TL001)")
        self.assertEqual(result.view["handover_summary"]["tl_summary"], "handover ok")

    def test_get_archive_view_rejects_active_case(self):
        orchestrator = self.make_orchestrator()
        case = self.init_case(orchestrator)

        result = orchestrator.handle("get_archive_view", {"case_id": case.case_id}, ADMIN)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_phase")

    def test_get_archive_view_is_admin_only(self):
        orchestrator = self.make_orchestrator()
        case = self.init_case(orchestrator)

        result = orchestrator.handle("get_archive_view", {"case_id": case.case_id}, HR)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "permission_denied")

    def test_get_archive_view_missing_case(self):
        orchestrator = self.make_orchestrator()

        result = orchestrator.handle("get_archive_view", {"case_id": "RES-MISSING"}, ADMIN)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "case_not_found")

    def test_timeout_marks_escalation_without_phase_change(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        case = orchestrator.store.get_active_case()
        case.steps.step_3.deadline = 0
        orchestrator.store.save_case(case)

        result = orchestrator.handle("scan_timeouts", {}, SYSTEM)
        self.assertTrue(result.ok, result)
        case = orchestrator.store.get_active_case()
        self.assertEqual(case.phase, CasePhase.HANDOVER_AND_RECOVERY)
        self.assertEqual(case.steps.step_3.tl_reminder_count, 1)

    def test_scan_sends_reminder_before_deadline(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)

        result = orchestrator.handle("scan_timeouts", {}, SYSTEM)

        self.assertTrue(result.ok, result)
        self.assertEqual(len(result.notifications), 2)
        case = orchestrator.store.get_active_case()
        self.assertEqual(case.steps.step_3.tl_reminder_count, 1)
        self.assertEqual(case.steps.step_3.ops_reminder_count, 1)

    def test_third_reminder_before_deadline_notifies_hr(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        case = orchestrator.store.get_active_case()
        case.steps.step_3.tl_reminder_count = 2
        case.steps.step_3.ops_done = True
        case.steps.step_3.tl_last_reminded_at = 0
        orchestrator.store.save_case(case)

        result = orchestrator.handle("scan_timeouts", {}, SYSTEM)

        self.assertTrue(result.ok, result)
        self.assertEqual(
            [notification.target_role.value for notification in result.notifications],
            ["tl", "hr"],
        )
        self.assertIn("提醒 3 次", result.notifications[1].content)

    def test_overdue_first_reminder_notifies_hr(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        case = orchestrator.store.get_active_case()
        case.steps.step_3.deadline = 0
        case.steps.step_3.ops_done = True
        orchestrator.store.save_case(case)

        result = orchestrator.handle("scan_timeouts", {}, SYSTEM)

        self.assertTrue(result.ok, result)
        self.assertEqual(
            [notification.target_role.value for notification in result.notifications],
            ["tl", "hr"],
        )
        self.assertIn("超时未完成", result.notifications[1].content)

    def test_employee_cannot_hr_confirm(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        result = orchestrator.handle(
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/15:18:00", "actor_name": "Alice", "actor_id": "HR001"},
            EMPLOYEE,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "permission_denied")

    def test_hr_confirm_rejects_seconds_format(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)

        result = orchestrator.handle(
            "complete_task",
            {
                "task_type": "hr_confirm",
                "last_working_day": "2026/05/15:18:00:00",
                "actor_name": "Alice",
                "actor_id": "HR001",
            },
            HR,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_payload")
        self.assertIn("YYYY/MM/DD:HH:MM", result.message)

    def test_admin_cannot_complete_task(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        result = orchestrator.handle(
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/15:18:00", "actor_name": "Alice", "actor_id": "HR001"},
            ADMIN,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "permission_denied")

    def test_admin_view_has_audit_and_hr_view_does_not(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)
        self.confirm_hr(orchestrator)
        admin_view = orchestrator.handle("get_status", {}, ADMIN)
        hr_view = orchestrator.handle("get_status", {}, HR)
        self.assertIn("audit_log", admin_view.view)
        self.assertNotIn("audit_log", hr_view.view)
        self.assertEqual(admin_view.view["responsible_summary"]["hr"], "Alice(ID: HR001)")
        self.assertEqual(hr_view.view["hr_responsible"], "Alice(ID: HR001)")

    def test_init_case_requires_pending_intent(self):
        orchestrator = self.make_orchestrator()

        result = orchestrator.handle("init_case", {"employee_name": "Alice", "employee_id": "E001"}, EMPLOYEE)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "pending_intent_required")

    def test_init_case_rejects_expired_pending_intent(self):
        orchestrator = self.make_orchestrator()
        registry = orchestrator.store.load_registry()
        registry.pending_intents[EMPLOYEE] = PendingIntent(detected_at=0, expires_at=0)
        orchestrator.store.save_registry(registry)

        result = orchestrator.handle("init_case", {"employee_name": "Alice", "employee_id": "E001"}, EMPLOYEE)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "pending_intent_expired")
        self.assertNotIn(EMPLOYEE, orchestrator.store.load_registry().pending_intents)

    def test_init_case_requires_employee_identity_after_pending(self):
        orchestrator = self.make_orchestrator()
        orchestrator.handle("set_pending_intent", {}, EMPLOYEE)

        result = orchestrator.handle("init_case", {}, EMPLOYEE)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_payload")

    def test_get_pending_intent_clears_expired_intent(self):
        orchestrator = self.make_orchestrator()
        registry = orchestrator.store.load_registry()
        registry.pending_intents[EMPLOYEE] = PendingIntent(detected_at=0, expires_at=0)
        orchestrator.store.save_registry(registry)

        result = orchestrator.handle("get_pending_intent", {}, EMPLOYEE)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "pending_intent_expired")
        self.assertIsNone(result.view["pending"])
        self.assertNotIn(EMPLOYEE, orchestrator.store.load_registry().pending_intents)

    def test_hr_confirm_requires_actor_identity(self):
        orchestrator = self.make_orchestrator()
        self.init_case(orchestrator)

        result = orchestrator.handle(
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/15:18:00"},
            HR,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_payload")

