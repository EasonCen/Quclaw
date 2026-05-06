"""Tests for the scenario_engine tool."""

from __future__ import annotations

import json

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

import tools.scenario_tool as scenario_tool
import tools.scenario_notify_tool as scenario_notify_tool

from scenario.model import CasePhase, CaseStatus, ScenarioCase, ScenarioRegistry, ScenarioResult
from scenario.orchestrator import ScenarioOrchestrator
from runtime.events import DispatchEvent, OutboundEvent


EMPLOYEE = "platform-telegram:employee/1"
HR = "platform-telegram:hr/1"
TL = "platform-telegram:tl/1"
OPS = "platform-telegram:ops/1"
ADMIN = "platform-telegram:admin/1"
DEFAULT_TELEGRAM = "platform-telegram:1"


class InMemoryScenarioStore:
    def __init__(self) -> None:
        self.registry = ScenarioRegistry()
        self.cases: dict[str, ScenarioCase] = {}
        self.archive_docs: dict[tuple[str, str], str] = {}
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

    def save_archive_document(self, case_id: str, name: str, content: str) -> str:
        self.archive_docs[(case_id, name)] = content
        return f"cases/archives/{case_id}/{name}"

    def next_case_id(self) -> str:
        case_id = f"RES-TEST-{self.next_id}"
        self.next_id += 1
        return case_id


class SessionStub:
    def __init__(self, source: str) -> None:
        self.source = source
        self.session_id = "session-1"


class EventBusStub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[OutboundEvent | DispatchEvent] = []

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("publish failed")
        self.events.append(event)

    def outbound_events(self) -> list[OutboundEvent]:
        return [event for event in self.events if isinstance(event, OutboundEvent)]

    def dispatch_events(self) -> list[DispatchEvent]:
        return [event for event in self.events if isinstance(event, DispatchEvent)]


class ScenarioToolTest(IsolatedAsyncioTestCase):
    def make_orchestrator(self) -> ScenarioOrchestrator:
        orchestrator = ScenarioOrchestrator.__new__(ScenarioOrchestrator)
        orchestrator.store = InMemoryScenarioStore()
        orchestrator.reminder_interval_minutes = 1
        return orchestrator

    def patch_orchestrator(self, orchestrator: ScenarioOrchestrator) -> None:
        original = scenario_tool.ScenarioOrchestrator
        scenario_tool.ScenarioOrchestrator = lambda workspace, **kwargs: orchestrator
        self.addCleanup(setattr, scenario_tool, "ScenarioOrchestrator", original)

    def patch_notify_orchestrator(self, orchestrator: ScenarioOrchestrator) -> None:
        original = scenario_notify_tool.ScenarioOrchestrator
        scenario_notify_tool.ScenarioOrchestrator = lambda workspace, **kwargs: orchestrator
        self.addCleanup(setattr, scenario_notify_tool, "ScenarioOrchestrator", original)

    def make_tool(
        self,
        orchestrator: ScenarioOrchestrator,
        eventbus: EventBusStub | None = None,
        config=None,
    ):
        self.patch_orchestrator(orchestrator)
        config = config or SimpleNamespace(workspace="unused")
        context = SimpleNamespace(
            config=config,
            eventbus=eventbus or EventBusStub(),
        )
        return scenario_tool.create_scenario_tool(context)

    def make_config_with_role_bots(self):
        return SimpleNamespace(
            workspace="unused",
            channels=SimpleNamespace(
                telegram=SimpleNamespace(
                    normalized_bots={
                        "employee": SimpleNamespace(enabled=True, allowed_user_ids=["1"]),
                        "hr": SimpleNamespace(enabled=True, allowed_user_ids=["1"]),
                        "tl": SimpleNamespace(enabled=True, allowed_user_ids=["1"]),
                        "ops": SimpleNamespace(enabled=True, allowed_user_ids=["1"]),
                        "admin": SimpleNamespace(enabled=True, allowed_user_ids=["1"]),
                    }
                )
            ),
            routing={
                "bindings": [
                    {"value": "platform-telegram:employee/.*", "agent": "employee"},
                    {"value": "platform-telegram:hr/.*", "agent": "hr"},
                    {"value": "platform-telegram:tl/.*", "agent": "tl"},
                    {"value": "platform-telegram:ops/.*", "agent": "ops"},
                    {"value": "platform-telegram:admin/.*", "agent": "admin"},
                ]
            },
        )

    async def call(self, tool, source: str, action: str, payload: dict | None = None) -> dict:
        raw = await tool.execute(SessionStub(source), action=action, payload=payload)
        return json.loads(raw)

    async def call_notify(self, tool, source: str, target_role: str, case_id: str, content: str) -> dict:
        raw = await tool.execute(
            SessionStub(source),
            target_role=target_role,
            case_id=case_id,
            content=content,
        )
        return json.loads(raw)

    async def init_case(self, tool) -> dict:
        await self.call(tool, EMPLOYEE, "set_pending_intent")
        return await self.call(
            tool,
            EMPLOYEE,
            "init_case",
            {"employee_name": "Alice", "employee_id": "E001"},
        )

    async def close_case(self, tool) -> str:
        await self.init_case(tool)
        await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )
        await self.call(tool, TL, "complete_task", {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"})
        result = await self.call(tool, OPS, "complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"})
        self.assertTrue(result["scenario"]["ok"], result)
        close_result = await self.call(tool, HR, "complete_task", {"task_type": "hr_sign"})
        self.assertTrue(close_result["scenario"]["ok"], close_result)
        return close_result["scenario"]["case_id"]

    async def test_tool_uses_session_source_not_payload_source(self):
        captured = {}

        class FakeOrchestrator:
            def __init__(self) -> None:
                self.store = InMemoryScenarioStore()

            def handle(self, action, payload, source):
                captured["action"] = action
                captured["payload"] = payload
                captured["source"] = source
                return ScenarioResult(ok=True, code="ok", message="ok")

        self.patch_orchestrator(FakeOrchestrator())
        context = SimpleNamespace(
            config=SimpleNamespace(workspace="unused"),
            eventbus=EventBusStub(),
        )
        tool = scenario_tool.create_scenario_tool(context)

        result = await self.call(
            tool,
            EMPLOYEE,
            "get_status",
            {"source": HR, "role": "hr"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(captured["source"], EMPLOYEE)
        self.assertEqual(captured["payload"]["source"], HR)

    async def test_employee_cannot_hr_confirm(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)
        await self.init_case(tool)

        result = await self.call(
            tool,
            EMPLOYEE,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertFalse(result["scenario"]["ok"])
        self.assertEqual(result["scenario"]["code"], "permission_denied")

    async def test_hr_confirm_moves_to_handover_and_recovery(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)
        await self.init_case(tool)

        result = await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.HANDOVER_AND_RECOVERY)

    async def test_tl_ops_can_complete_in_either_order(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)
        await self.init_case(tool)
        await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        await self.call(tool, OPS, "complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"})
        result = await self.call(
            tool,
            TL,
            "complete_task",
            {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.AWAITING_HR_SIGNOFF)

    async def test_admin_audit_ok_but_complete_task_denied(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)
        await self.init_case(tool)

        audit = await self.call(tool, ADMIN, "get_audit_log")
        denied = await self.call(
            tool,
            ADMIN,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertTrue(audit["scenario"]["ok"], audit)
        self.assertIn("audit_log", audit["scenario"]["view"])
        self.assertFalse(denied["scenario"]["ok"])
        self.assertEqual(denied["scenario"]["code"], "permission_denied")
        self.assertEqual(denied["delivery"]["published"], 0)

    async def test_default_telegram_source_is_rejected(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        result = await self.call(tool, DEFAULT_TELEGRAM, "get_status")

        self.assertFalse(result["scenario"]["ok"])
        self.assertEqual(result["scenario"]["code"], "unsupported_source")

    async def test_tool_result_is_json_with_required_fields(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        result = await self.init_case(tool)

        self.assertTrue({"scenario", "delivery"}.issubset(result))
        self.assertTrue({"ok", "code", "message"}.issubset(result["scenario"]))
        self.assertTrue({"published", "skipped", "errors"}.issubset(result["delivery"]))

    async def test_hr_confirm_publishes_to_registered_tl_and_ops(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(orchestrator, eventbus)

        await self.call(tool, TL, "get_status")
        await self.call(tool, OPS, "get_status")
        await self.call(tool, ADMIN, "get_status")
        await self.init_case(tool)
        eventbus.events.clear()
        result = await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 4)
        self.assertEqual(result["delivery"]["skipped"], [])
        self.assertEqual([str(event.source) for event in eventbus.events], [TL, OPS, ADMIN, EMPLOYEE])
        for event in eventbus.events:
            self.assertIn("申请单号：RES-TEST-1", event.content)
            self.assertIn("员工：Alice(ID: E001)", event.content)
            self.assertIn("创建时间：", event.content)
            self.assertIn("最后工作时间：2026/05/20:18:00", event.content)

    async def test_init_case_hr_notification_contains_case_details(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(orchestrator, eventbus)

        await self.call(tool, HR, "register_source")
        result = await self.init_case(tool)

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 1)
        event = eventbus.events[0]
        self.assertEqual(str(event.source), HR)
        self.assertIn("收到新的试用期主动离职申请。", event.content)
        self.assertIn("申请单号：RES-TEST-1", event.content)
        self.assertIn("员工：Alice(ID: E001)", event.content)
        self.assertIn("创建时间：", event.content)
        self.assertIn("请在 24 小时内确认申请并填写最后工作时间。", event.content)

    async def test_hr_confirm_skips_unregistered_tl_and_ops(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        await self.init_case(tool)
        result = await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 1)
        self.assertEqual(
            [item["target_role"] for item in result["delivery"]["skipped"]],
            ["tl", "ops", "admin"],
        )

    async def test_configured_allowed_users_are_pre_registered(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(
            orchestrator,
            eventbus,
            config=self.make_config_with_role_bots(),
        )

        init_result = await self.init_case(tool)

        self.assertTrue(init_result["scenario"]["ok"], init_result)
        self.assertEqual(init_result["delivery"]["published"], 2)
        self.assertEqual([str(event.source) for event in eventbus.events], [HR, ADMIN])
        registry = orchestrator.store.load_registry()
        self.assertEqual(registry.role_sources["admin"], [ADMIN])

        eventbus.events.clear()
        result = await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 4)
        self.assertEqual(result["delivery"]["skipped"], [])
        self.assertEqual([str(event.source) for event in eventbus.events], [TL, OPS, ADMIN, EMPLOYEE])

    async def test_tool_call_auto_registers_current_source(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        await self.call(tool, HR, "get_status")

        registry = orchestrator.store.load_registry()
        self.assertEqual(registry.role_sources["hr"], [HR])

    async def test_auto_register_syncs_active_case_role_sources(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        await self.init_case(tool)
        await self.call(tool, TL, "get_status")

        case = orchestrator.store.get_active_case()
        self.assertEqual(case.role_sources["tl"], [TL])

    async def test_tl_ops_completion_publishes_hr_notification(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(orchestrator, eventbus)

        await self.call(tool, HR, "get_status")
        await self.init_case(tool)
        await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )
        await self.call(tool, OPS, "complete_task", {"task_type": "ops_done", "actor_name": "Alice", "actor_id": "OPS001"})
        result = await self.call(
            tool,
            TL,
            "complete_task",
            {"task_type": "tl_done", "tl_summary": "handover ok", "actor_name": "Alice", "actor_id": "TL001"},
        )

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 2)
        self.assertEqual([str(event.source) for event in eventbus.events[-2:]], [HR, EMPLOYEE])

    async def test_publish_failure_happens_after_case_update(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator, EventBusStub(fail=True))

        await self.call(tool, TL, "get_status")
        await self.call(tool, OPS, "get_status")
        await self.init_case(tool)

        with self.assertRaises(RuntimeError):
            await self.call(
                tool,
                HR,
                "complete_task",
                {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
            )

        self.assertEqual(orchestrator.store.get_active_case().phase, CasePhase.HANDOVER_AND_RECOVERY)

    async def test_cron_timeout_publishes_to_registered_roles(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(orchestrator, eventbus)

        await self.call(tool, TL, "get_status")
        await self.call(tool, OPS, "get_status")
        await self.call(tool, HR, "get_status")
        await self.call(tool, ADMIN, "get_status")
        await self.init_case(tool)
        await self.call(
            tool,
            HR,
            "complete_task",
            {"task_type": "hr_confirm", "last_working_day": "2026/05/20:18:00", "actor_name": "Alice", "actor_id": "HR001"},
        )
        eventbus.events.clear()
        case = orchestrator.store.get_active_case()
        case.steps.step_3.deadline = 0
        orchestrator.store.save_case(case)

        result = await self.call(tool, "cron:resignation-monitor", "scan_timeouts")

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["delivery"]["published"], 4)
        self.assertEqual([str(event.source) for event in eventbus.events], [TL, HR, OPS, HR])

    async def test_get_archive_view_tool_for_closed_case(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        case_id = await self.close_case(tool)
        result = await self.call(tool, ADMIN, "get_archive_view", {"case_id": case_id})

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["scenario"]["code"], "archive_view")
        self.assertEqual(result["scenario"]["view"]["case_id"], case_id)
        self.assertEqual(result["scenario"]["view"]["handover_summary"]["tl_summary"], "handover ok")

    async def test_get_status_tool_returns_closed_case_by_case_id(self):
        orchestrator = self.make_orchestrator()
        tool = self.make_tool(orchestrator)

        case_id = await self.close_case(tool)
        result = await self.call(tool, EMPLOYEE, "get_status", {"case_id": case_id})

        self.assertTrue(result["scenario"]["ok"], result)
        self.assertEqual(result["scenario"]["view"]["status"], "closed")
        self.assertEqual(result["scenario"]["view"]["phase"], "closed")

    async def test_hr_sign_dispatches_archive_job_to_admin(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        tool = self.make_tool(orchestrator, eventbus)

        await self.call(tool, ADMIN, "register_source")
        case_id = await self.close_case(tool)

        dispatches = eventbus.dispatch_events()
        self.assertEqual(len(dispatches), 1)
        dispatch = dispatches[0]
        self.assertEqual(dispatch.target_agent_id, "admin")
        self.assertEqual(str(dispatch.source), ADMIN)
        self.assertEqual(dispatch.parent_session_id, "session-1")
        self.assertIn(case_id, dispatch.content)
        self.assertIn("get_archive_view", dispatch.content)
        self.assertIn("scenario_notify", dispatch.content)
        self.assertIn("target_role=\"admin\"", dispatch.content)
        self.assertIn("完整未脱敏归档报告", dispatch.content)

    async def test_scenario_notify_admin_sends_to_admin_employee_and_hr(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        scenario_engine = self.make_tool(orchestrator, eventbus)
        await self.call(scenario_engine, ADMIN, "register_source")
        await self.call(scenario_engine, EMPLOYEE, "register_source")
        await self.call(scenario_engine, HR, "register_source")
        case_id = await self.close_case(scenario_engine)
        self.patch_notify_orchestrator(orchestrator)
        notify_tool = scenario_notify_tool.create_scenario_notify_tool(
            SimpleNamespace(config=SimpleNamespace(workspace="unused"), eventbus=eventbus)
        )
        eventbus.events.clear()

        admin_result = await self.call_notify(notify_tool, ADMIN, "admin", case_id, "完整未脱敏报告")
        employee_result = await self.call_notify(notify_tool, ADMIN, "employee", case_id, "脱敏摘要")
        hr_result = await self.call_notify(notify_tool, ADMIN, "hr", case_id, "脱敏摘要")

        self.assertTrue(admin_result["ok"], admin_result)
        self.assertTrue(employee_result["ok"], employee_result)
        self.assertTrue(hr_result["ok"], hr_result)
        self.assertEqual(admin_result["delivery"]["published"], 1)
        self.assertEqual(employee_result["delivery"]["published"], 1)
        self.assertEqual(hr_result["delivery"]["published"], 1)
        self.assertEqual(admin_result["archive_path"], f"cases/archives/{case_id}/admin_full_report.md")
        self.assertEqual(
            orchestrator.store.archive_docs[(case_id, "admin_full_report.md")],
            "完整未脱敏报告",
        )
        self.assertEqual(
            orchestrator.store.archive_docs[(case_id, "employee_summary.md")],
            "脱敏摘要",
        )
        self.assertEqual(
            orchestrator.store.archive_docs[(case_id, "hr_summary.md")],
            "脱敏摘要",
        )
        outbound_events = eventbus.outbound_events()
        self.assertEqual([str(event.source) for event in outbound_events], [ADMIN, EMPLOYEE, HR])
        self.assertEqual([event.content for event in outbound_events], ["完整未脱敏报告", "脱敏摘要", "脱敏摘要"])

    async def test_scenario_notify_rejects_active_case_archive(self):
        orchestrator = self.make_orchestrator()
        scenario_engine = self.make_tool(orchestrator)
        await self.call(scenario_engine, EMPLOYEE, "set_pending_intent")
        init_result = await self.call(
            scenario_engine,
            EMPLOYEE,
            "init_case",
            {"employee_name": "Alice", "employee_id": "E001"},
        )
        self.patch_notify_orchestrator(orchestrator)
        notify_tool = scenario_notify_tool.create_scenario_notify_tool(
            SimpleNamespace(config=SimpleNamespace(workspace="unused"), eventbus=EventBusStub())
        )

        result = await self.call_notify(
            notify_tool,
            ADMIN,
            "admin",
            init_result["scenario"]["case_id"],
            "完整未脱敏报告",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_phase")

    async def test_scenario_notify_rejects_non_admin(self):
        orchestrator = self.make_orchestrator()
        scenario_engine = self.make_tool(orchestrator)
        case_id = await self.close_case(scenario_engine)
        self.patch_notify_orchestrator(orchestrator)
        notify_tool = scenario_notify_tool.create_scenario_notify_tool(
            SimpleNamespace(config=SimpleNamespace(workspace="unused"), eventbus=EventBusStub())
        )

        result = await self.call_notify(notify_tool, HR, "employee", case_id, "脱敏摘要")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "permission_denied")

    async def test_scenario_notify_rejects_invalid_target(self):
        orchestrator = self.make_orchestrator()
        scenario_engine = self.make_tool(orchestrator)
        case_id = await self.close_case(scenario_engine)
        self.patch_notify_orchestrator(orchestrator)
        notify_tool = scenario_notify_tool.create_scenario_notify_tool(
            SimpleNamespace(config=SimpleNamespace(workspace="unused"), eventbus=EventBusStub())
        )

        result = await self.call_notify(notify_tool, ADMIN, "ops", case_id, "脱敏摘要")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_payload")

    async def test_scenario_notify_skips_unregistered_target(self):
        orchestrator = self.make_orchestrator()
        eventbus = EventBusStub()
        scenario_engine = self.make_tool(orchestrator, eventbus)
        case_id = await self.close_case(scenario_engine)
        case = orchestrator.store.get_case(case_id)
        case.role_sources.pop("employee", None)
        orchestrator.store.save_case(case)
        registry = orchestrator.store.load_registry()
        registry.role_sources.pop("employee", None)
        orchestrator.store.save_registry(registry)
        self.patch_notify_orchestrator(orchestrator)
        notify_tool = scenario_notify_tool.create_scenario_notify_tool(
            SimpleNamespace(config=SimpleNamespace(workspace="unused"), eventbus=eventbus)
        )
        eventbus.events.clear()

        result = await self.call_notify(notify_tool, ADMIN, "employee", case_id, "脱敏摘要")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"]["published"], 0)
        self.assertEqual(result["delivery"]["skipped"][0]["target_role"], "employee")
