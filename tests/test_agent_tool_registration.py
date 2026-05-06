"""Tests for shared tool registration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from core.agent import Agent
from runtime.events import CliEventSource, CronEventSource
from channel.telegram_channel import TelegramEventSource


class AgentLoaderStub:
    def discover_agents(self):
        return []


class AgentToolRegistrationTest(TestCase):
    def make_agent(self) -> Agent:
        agent = Agent.__new__(Agent)
        agent.agent_def = SimpleNamespace(id="test-agent", allow_skills=False)
        agent.context = SimpleNamespace(
            config=SimpleNamespace(
                workspace=Path("unused"),
                websearch=None,
                webread=None,
                channels=SimpleNamespace(enabled=False),
            ),
            skill_loader=None,
            agent_loader=AgentLoaderStub(),
        )
        return agent

    def tool_names_for(self, source) -> set[str]:
        return {tool.name for tool in self.make_agent()._build_tools(source).list_all()}

    def test_scenario_role_sources_get_scenario_tool(self):
        for bot_key in ("employee", "hr", "tl", "ops", "admin"):
            source = TelegramEventSource(chat_id="1", bot_key=bot_key)

            self.assertIn("scenario_engine", self.tool_names_for(source))

    def test_only_admin_source_gets_scenario_notify_tool(self):
        admin_source = TelegramEventSource(chat_id="1", bot_key="admin")
        hr_source = TelegramEventSource(chat_id="1", bot_key="hr")

        self.assertIn("scenario_notify", self.tool_names_for(admin_source))
        self.assertNotIn("scenario_notify", self.tool_names_for(hr_source))

    def test_cron_source_gets_scenario_tool_for_timeout_scan(self):
        source = CronEventSource("resignation-monitor")

        self.assertIn("scenario_engine", self.tool_names_for(source))

    def test_default_telegram_source_gets_scenario_tool(self):
        source = TelegramEventSource(chat_id="1")

        self.assertIn("scenario_engine", self.tool_names_for(source))

    def test_cli_source_gets_scenario_tool(self):
        source = CliEventSource()

        self.assertIn("scenario_engine", self.tool_names_for(source))
