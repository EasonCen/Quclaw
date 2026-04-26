"""Tests for heartbeat background worker behavior."""

import asyncio
import shutil
import sys
import uuid

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.events import (
    DispatchEvent,
    DispatchResultEvent,
    EventSource,
    HeartbeatEventSource,
    OutboundEvent,
)
from core.history import HistoryStore
from core.session_state import SessionState
from server.agent_worker import AgentWorker
from server.heartbeat_worker import (
    HEARTBEAT_OK,
    HeartbeatWorker,
    build_heartbeat_prompt,
    is_heartbeat_ok,
)
from utils.config import Config, HeartbeatConfig


class FakeEventBus:
    def __init__(self) -> None:
        self.subscriptions: list[type[Any]] = []
        self.published: list[Any] = []

    def subscribe(self, event_class: type[Any], handler: Any) -> None:
        del handler
        self.subscriptions.append(event_class)

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class FakeContext:
    def __init__(
        self,
        workspace: Path,
        heartbeat: HeartbeatConfig | None = None,
    ) -> None:
        self.config = SimpleNamespace(
            workspace=workspace,
            default_agent="Qu",
            heartbeat=heartbeat or HeartbeatConfig(),
        )
        self.eventbus = FakeEventBus()


def _write_minimal_config(workspace: Path, heartbeat_block: str = "") -> None:
    content = "\n".join(
        [
            "llm:",
            "  provider: openai",
            "  model: gpt-5",
            "  api_key: sk-test",
            "default_agent: Qu",
            heartbeat_block,
        ]
    )
    (workspace / "config.user.yaml").write_text(content, encoding="utf-8")


def _make_workspace(prefix: str) -> Path:
    workspace = Path(__file__).resolve().parent / f".{prefix}-{uuid.uuid4().hex}"
    workspace.mkdir()
    return workspace


def test_heartbeat_config_defaults_to_disabled() -> None:
    workspace = _make_workspace("heartbeat-config")
    try:
        _write_minimal_config(workspace)

        config = Config.load(workspace)

        assert config.heartbeat.interval_minutes == 0
        assert config.heartbeat.agent is None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_heartbeat_config_can_be_enabled_and_reloaded() -> None:
    workspace = _make_workspace("heartbeat-config")
    try:
        _write_minimal_config(
            workspace,
            "\n".join(["heartbeat:", "  interval_minutes: 30", "  agent: null"]),
        )

        config = Config.load(workspace)

        assert config.heartbeat.interval_minutes == 30
        assert config.heartbeat.agent is None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_heartbeat_event_source_round_trips() -> None:
    source = HeartbeatEventSource(agent_id="Qu")

    parsed = EventSource.from_string(str(source))

    assert isinstance(parsed, HeartbeatEventSource)
    assert parsed.agent_id == "Qu"
    assert parsed.is_heartbeat is True
    assert parsed.is_cron is False
    assert parsed.is_platform is False


def test_heartbeat_prompt_reads_workspace_checklist() -> None:
    workspace = _make_workspace("heartbeat-prompt")
    try:
        (workspace / "HEARTBEAT.md").write_text(
            "# HEARTBEAT\n\n- Check project notes.",
            encoding="utf-8",
        )
        context = FakeContext(workspace)

        prompt = build_heartbeat_prompt(
            context.config,
            datetime(2026, 4, 27, 9, 30),
        )

        assert "HEARTBEAT.md" in prompt
        assert "Check project notes." in prompt
        assert "2026-04-27T09:30:00" in prompt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_heartbeat_prompt_handles_missing_checklist() -> None:
    workspace = _make_workspace("heartbeat-prompt")
    try:
        context = FakeContext(workspace)

        prompt = build_heartbeat_prompt(context.config)

        assert "No HEARTBEAT.md exists" in prompt
        assert HEARTBEAT_OK in prompt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_is_heartbeat_ok_requires_exact_ack() -> None:
    assert is_heartbeat_ok(" HEARTBEAT_OK\n") is True
    assert is_heartbeat_ok("HEARTBEAT_OK - done") is False
    assert is_heartbeat_ok("") is False


def test_heartbeat_worker_dispatches_internal_event() -> None:
    async def scenario() -> None:
        workspace = _make_workspace("heartbeat-worker")
        context = FakeContext(workspace, HeartbeatConfig(interval_minutes=30))
        worker = HeartbeatWorker(context)

        try:
            dispatched = await worker._dispatch_heartbeat(
                datetime(2026, 4, 27, 9, 30),
            )

            assert dispatched is True
            assert context.eventbus.subscriptions == [DispatchResultEvent]
            assert len(context.eventbus.published) == 1
            event = context.eventbus.published[0]
            assert isinstance(event, DispatchEvent)
            assert event.session_id == "heartbeat-Qu"
            assert event.target_agent_id == "Qu"
            assert isinstance(event.source, HeartbeatEventSource)
            assert "silent heartbeat" in event.content
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    asyncio.run(scenario())


def test_heartbeat_worker_skips_when_in_flight() -> None:
    async def scenario() -> None:
        workspace = _make_workspace("heartbeat-worker")
        context = FakeContext(workspace, HeartbeatConfig(interval_minutes=30))
        worker = HeartbeatWorker(context)

        try:
            first = await worker._dispatch_heartbeat()
            second = await worker._dispatch_heartbeat()

            assert first is True
            assert second is False
            assert len(context.eventbus.published) == 1
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    asyncio.run(scenario())


def test_heartbeat_worker_consumes_results_without_outbound() -> None:
    async def scenario() -> None:
        workspace = _make_workspace("heartbeat-worker")
        context = FakeContext(workspace, HeartbeatConfig(interval_minutes=30))
        worker = HeartbeatWorker(context)

        try:
            await worker._dispatch_heartbeat()
            request_id = context.eventbus.published[0].request_id

            await worker.handle_result(
                DispatchResultEvent(
                    session_id="heartbeat-Qu",
                    request_id=request_id,
                    content=HEARTBEAT_OK,
                    source=HeartbeatEventSource(agent_id="Qu"),
                )
            )

            assert worker._pending_request_id is None
            assert not any(
                isinstance(event, OutboundEvent)
                for event in context.eventbus.published
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    asyncio.run(scenario())


def test_heartbeat_worker_ignores_non_heartbeat_results() -> None:
    async def scenario() -> None:
        workspace = _make_workspace("heartbeat-worker")
        context = FakeContext(workspace, HeartbeatConfig(interval_minutes=30))
        worker = HeartbeatWorker(context)

        try:
            await worker._dispatch_heartbeat()

            await worker.handle_result(
                DispatchResultEvent(
                    session_id="other",
                    content=HEARTBEAT_OK,
                    source=SimpleNamespace(is_heartbeat=False),
                )
            )

            assert worker._pending_request_id is not None
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    asyncio.run(scenario())


def test_agent_worker_prunes_heartbeat_ok_from_session_history() -> None:
    async def scenario() -> None:
        workspace = _make_workspace("heartbeat-agent-worker")
        try:
            history_store = HistoryStore(workspace / ".history")
            source = HeartbeatEventSource(agent_id="Qu")
            session_id = "heartbeat-Qu"
            history_store.create_session("Qu", session_id, source)
            context = SimpleNamespace(
                eventbus=FakeEventBus(),
                history_store=history_store,
            )
            state = SessionState(
                session_id=session_id,
                agent=SimpleNamespace(),
                messages=[],
                source=source,
                shared_context=context,
            )
            session = SimpleNamespace(state=state)

            class StubAgentWorker(AgentWorker):
                def _route_agent_def(self, event: DispatchEvent) -> Any:
                    del event
                    return SimpleNamespace(id="Qu")

                def _get_or_create_session(
                    self,
                    event: DispatchEvent,
                    agent_def: Any,
                ) -> Any:
                    del event, agent_def
                    return session

                async def _run_command_or_chat(
                    self,
                    content: str,
                    session: Any,
                ) -> str:
                    session.state.add_message({"role": "user", "content": content})
                    session.state.add_message(
                        {"role": "assistant", "content": HEARTBEAT_OK}
                    )
                    return HEARTBEAT_OK

            worker = StubAgentWorker(context)

            await worker.exec_session(
                DispatchEvent(
                    session_id=session_id,
                    content="Run a silent heartbeat check.",
                    source=source,
                    target_agent_id="Qu",
                )
            )

            session_info = history_store.get_session_info(session_id)
            assert state.messages == []
            assert history_store.get_messages(session_id) == []
            assert session_info is not None
            assert session_info.message_count == 0
            assert session_info.title is None
            assert isinstance(context.eventbus.published[-1], DispatchResultEvent)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    asyncio.run(scenario())
