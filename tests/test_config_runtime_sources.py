"""Tests for runtime source session affinity persistence."""

import asyncio
import shutil
import sys
import uuid

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.skill_loader import SkillLoader
from tools.skill_tools import create_skill_tool
from utils.config import Config, SourceSessionConfig


def _write_minimal_config(workspace: Path) -> None:
    (workspace / "config.user.yaml").write_text(
        "\n".join(
            [
                "llm:",
                "  provider: openai",
                "  model: gpt-5",
                "  api_key: sk-test",
                "default_agent: Qu",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_runtime_source_key_is_not_treated_as_dot_path() -> None:
    workspace = Path(__file__).resolve().parent / f".runtime-{uuid.uuid4().hex}"
    try:
        workspace.mkdir()
        _write_minimal_config(workspace)

        config = Config.load(workspace)
        source = "platform-ws:app.v1/chat.2"

        config.set_runtime_source(source, SourceSessionConfig(session_id="session-1"))

        reloaded = Config.load(workspace)
        assert reloaded.sources[source].session_id == "session-1"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _write_cron_ops_skill(workspace: Path) -> None:
    skill_dir = workspace / "skills" / "cron-ops"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: cron-ops",
                "description: Create, list, and delete scheduled cron jobs",
                "---",
                "",
                "Crons live at `{{crons_path}}`.",
                "Use default agent `{{default_agent}}`.",
            ]
        ),
        encoding="utf-8",
    )


def test_skill_loader_renders_config_placeholders() -> None:
    workspace = Path(__file__).resolve().parent / f".skill-{uuid.uuid4().hex}"
    try:
        workspace.mkdir()
        _write_minimal_config(workspace)
        _write_cron_ops_skill(workspace)
        config = Config.load(workspace)

        skill = SkillLoader.from_config(config).load_skill("cron-ops")

        assert "{{crons_path}}" not in skill.content
        assert "{{default_agent}}" not in skill.content
        assert config.crons_path.resolve().as_posix() in skill.content
        assert "Use default agent `Qu`." in skill.content
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_skill_tool_returns_rendered_skill_content() -> None:
    workspace = Path(__file__).resolve().parent / f".skill-{uuid.uuid4().hex}"
    try:
        workspace.mkdir()
        _write_minimal_config(workspace)
        _write_cron_ops_skill(workspace)
        config = Config.load(workspace)
        skill_loader = SkillLoader.from_config(config)

        skill_tool = create_skill_tool(skill_loader)
        assert skill_tool is not None

        content = asyncio.run(
            skill_tool.execute(session=object(), skill_name="cron-ops")
        )

        assert "{{crons_path}}" not in content
        assert config.crons_path.resolve().as_posix() in content
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
