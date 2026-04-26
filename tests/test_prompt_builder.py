"""Tests for layered prompt assembly."""

import shutil
import sys
import uuid

from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.events import CliEventSource, HeartbeatEventSource
from core.prompt_builder import PromptBuilder


class FakeCronLoader:
    def __init__(self, crons=None, error: Exception | None = None) -> None:
        self.crons = crons or []
        self.error = error

    def discover_crons(self):
        if self.error is not None:
            raise self.error
        return self.crons


class FakeConfig:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.agents_path = workspace / "agents"
        self.skills_path = workspace / "skills"
        self.crons_path = workspace / "crons"
        self.history_path = workspace / ".history"
        self.logging_path = workspace / ".logs"
        self.event_path = workspace / ".event"
        self.default_agent = "Qu"

    def template_vars(self) -> dict[str, str]:
        values = {
            "workspace": self.workspace.resolve().as_posix(),
            "agents_path": self.agents_path.resolve().as_posix(),
            "skills_path": self.skills_path.resolve().as_posix(),
            "crons_path": self.crons_path.resolve().as_posix(),
            "history_path": self.history_path.resolve().as_posix(),
            "logging_path": self.logging_path.resolve().as_posix(),
            "event_path": self.event_path.resolve().as_posix(),
            "default_agent": self.default_agent,
        }
        values["memories_path"] = (self.workspace / "memories").resolve().as_posix()
        return values


def _context(tmp_path: Path, cron_loader: FakeCronLoader | None = None):
    return SimpleNamespace(
        config=FakeConfig(tmp_path),
        cron_loader=cron_loader or FakeCronLoader(),
    )


def _state(source):
    agent_def = SimpleNamespace(
        id="Qu",
        agent_md="Identity layer",
        soul_md="Soul layer",
    )
    agent = SimpleNamespace(agent_def=agent_def)
    return SimpleNamespace(agent=agent, source=source)


def _make_workspace(prefix: str) -> Path:
    workspace = Path(__file__).resolve().parent / f".{prefix}-{uuid.uuid4().hex}"
    workspace.mkdir()
    return workspace


def test_prompt_builder_assembles_layers_and_renders_workspace_files():
    workspace = _make_workspace("prompt-builder")
    try:
        (workspace / "BOOTSTRAP.md").write_text(
            "Workspace: `{{workspace}}`\nMemories: `{{memories_path}}`",
            encoding="utf-8",
        )
        (workspace / "AGENTS.md").write_text(
            "Default agent: `{{default_agent}}`",
            encoding="utf-8",
        )
        cron = SimpleNamespace(name="Daily Check", description="Inspect project state")
        builder = PromptBuilder(_context(workspace, FakeCronLoader([cron])))

        prompt = builder.build(_state(CliEventSource()))

        assert "Identity layer" in prompt
        assert "## Personality\n\nSoul layer" in prompt
        assert workspace.resolve().as_posix() in prompt
        assert "{{workspace}}" not in prompt
        assert "{{memories_path}}" not in prompt
        assert "Default agent: `Qu`" in prompt
        assert "## Scheduled Tasks" in prompt
        assert "- **Daily Check**: Inspect project state" in prompt
        assert "## Runtime\n\nAgent: Qu" in prompt
        assert "You are responding via cli." in prompt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_prompt_builder_handles_heartbeat_source():
    workspace = _make_workspace("prompt-builder")
    try:
        builder = PromptBuilder(
            _context(workspace, FakeCronLoader(error=RuntimeError("boom")))
        )

        prompt = builder.build(_state(HeartbeatEventSource(agent_id="Qu")))

        assert "silent heartbeat check" in prompt
        assert "consumed internally" in prompt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
