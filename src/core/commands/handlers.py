"""Built-in slash command handlers."""
from typing import TYPE_CHECKING

import re
from core.commands.base import Command
from utils.def_loader import DefNotFoundError, InvalidDefError

if TYPE_CHECKING:
    from core.agent_loader import AgentDef
    from core.cron_loader import CronDef
    from core.agent import AgentSession


class SessionCommand(Command):
    """Show current session details"""

    name = "session"
    description = "Show current session details."

    async def execute(self, args: str, session: "AgentSession") -> str:
        info = session.agent.history_store.get_session_info(session.session_id)

        # 处理在 index 中找不到的 session
        created_str = info.created_at if info else "Unknown"

        lines = [
            f"**Session ID:** `{session.session_id}`",
            f"**Agent:** {session.agent.agent_def.name} (`{session.agent.agent_def.id}`)",
            f"**Created:** {created_str}",
            f"**Messages:** {len(session.state.messages)}",
        ]
        return "\n".join(lines)


class HelpCommand(Command):
    """Show available commands."""

    name = "help"
    description = "Show available slash commands."

    async def execute(self, args: str, session: "AgentSession") -> str:
        return session.agent.context.command_registry.render_help()


class CompactCommand(Command):
    """Trigger manual context compaction."""

    name = "compact"
    description = "Compact conversation context manually"

    async def execute(self, args: str, session: "AgentSession") -> str:
        # Force compaction regardless of threshold
        context_guard = getattr(session, "context_guard")
        await context_guard._compact_messages(session.state, session.agent.llm)
        msg_count = len(session.state.messages)
        return f"✓ Context compacted. {msg_count} messages retained."


class ContextCommand(Command):
    """Show session context information."""

    name = "context"
    description = "Show session context information"

    async def execute(self, args: str, session: "AgentSession") -> str:
        token_count = session.context_guard.estimate_tokens(session.state)
        threshold = session.context_guard.token_threshold
        usage_pct = (token_count / threshold) * 100 if threshold > 0 else 0

        lines = [
            f"**Messages:** {len(session.state.messages)}",
            f"**Tokens:** {token_count:,} ({usage_pct:.1f}% of {threshold:,} threshold)",
        ]
        return "\n".join(lines)


class SkillsCommand(Command):
    """List all skills or show skill details."""

    name = "skills"
    description = "List all skills or show one skill's content."

    async def execute(self, args: str, session: "AgentSession") -> str:
        if not session.agent.agent_def.allow_skills:
            return "Skills are disabled for this agent."

        skill_id = args.strip()

        try:
            if skill_id:
                skill = session.agent.skill_loader.load_skill(skill_id)
                return "\n".join(
                    [
                        f"**Skill:** {skill.name} (`{skill.id}`)",
                        f"**Description:** {skill.description}",
                        "",
                        skill.content,
                    ]
                )

            skills = sorted(
                session.agent.skill_loader.discover_skills(),
                key=lambda skill: skill.name.lower(),
            )
        except DefNotFoundError as exc:
            if exc.def_type == "skill":
                return f"Skill not found: `{skill_id}`"
            return "Skills directory not found."

        if not skills:
            return "No skills available."

        lines = ["Available skills:"]
        for skill in skills:
            lines.append(f"- `{skill.id}`: {skill.name} - {skill.description}")
        return "\n".join(lines)



class RouteCommand(Command):
    """Create a routing binding."""

    name = "route"
    description = "Create a routing binding (persists to config)"

    async def execute(self, args: str, session: "AgentSession") -> str:
        parts = args.strip().split(None, 1)
        if len(parts) != 2:
            return "**Usage:** `/route <source_pattern> <agent_id>`\n\nExample: `/route platform-telegram:.* Qu`"

        pattern, agent_id = parts

        # Validate regex pattern
        try:
            re.compile(f"^{pattern}$")
        except re.error as e:
            return f"✗ Invalid regex pattern: {e}"

        # Verify agent exists
        try:
            session.shared_context.agent_loader.load(agent_id)
        except DefNotFoundError as exc:
            if exc.def_type != "agent":
                raise
            return f"✗ Agent `{agent_id}` not found."

        # Create and persist binding, then reset affected source cache entries.
        routing_table = session.shared_context.routing_table
        routing_table.persist_binding(pattern, agent_id)
        reset_count = routing_table.reset_matching_source_sessions(
            pattern,
            agent_id,
        )

        lines = [f"✓ Route bound: `{pattern}` → `{agent_id}`"]
        if reset_count:
            lines.append(
                f"Reset {reset_count} cached source session(s). "
                "Matching sources will create new sessions on next message."
            )
        else:
            lines.append("No cached source sessions needed reset.")
        return "\n".join(lines)


class BindingsCommand(Command):
    """Show all routing bindings."""

    name = "bindings"
    description = "Show all routing bindings"

    async def execute(self, args: str, session: "AgentSession") -> str:
        bindings = session.shared_context.config.routing.get("bindings", [])

        if not bindings:
            return "No routing bindings configured."

        lines = ["**Routing Bindings:**"]
        for binding in bindings:
            lines.append(f"- `{binding['value']}` → `{binding['agent']}`")

        return "\n".join(lines)


class CronsCommand(Command):
    """List all cron jobs or show cron details."""

    name = "crons"
    description = "List all cron jobs or show one cron's content."

    async def execute(self, args: str, session: "AgentSession") -> str:
        cron_id = args.strip()

        try:
            if cron_id:
                cron = session.shared_context.cron_loader.load(cron_id)
                return self._render_cron_detail(cron)

            crons = sorted(
                session.shared_context.cron_loader.discover_crons(),
                key=lambda cron: cron.name.lower(),
            )
        except DefNotFoundError as exc:
            if exc.def_type == "cron":
                return f"✗ Cron job `{cron_id}` not found."
            return "Crons directory not found."
        except InvalidDefError as exc:
            return f"Invalid cron definition: {exc}"

        if not crons:
            return "No cron jobs configured."

        lines = ["**Cron Jobs:**"]
        for cron in crons:
            one_off = " one-off" if cron.one_off else ""
            lines.append(
                f"- `{cron.id}`: {cron.name} - `{cron.schedule}` → `{cron.agent}`{one_off}"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_cron_detail(cron: "CronDef") -> str:
        """Render details for a single cron job."""
        lines = [
            f"**Cron:** `{cron.id}`",
            f"**Name:** {cron.name}",
            f"**Description:** {cron.description}",
            f"**Agent:** `{cron.agent}`",
            f"**Schedule:** `{cron.schedule}`",
            f"**One-off:** {cron.one_off}",
            "",
            "---",
            "",
            "**Prompt:**",
            "```",
            cron.prompt,
            "```",
        ]
        return "\n".join(lines)


class AgentCommand(Command):
    """List agents or show agent details."""

    name = "agent"
    aliases = ["agents"]
    description = "List agents or show agent details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        context = session.shared_context
        agent_id = args.strip()

        try:
            if not agent_id:
                agents = context.agent_loader.discover_agents()
                return self._render_agent_list(agents, session)

            agent_def = context.agent_loader.load(agent_id)
        except DefNotFoundError as exc:
            if exc.def_type == "agent":
                return f"✗ Agent `{agent_id}` not found."
            return "Agents directory not found."
        except InvalidDefError as exc:
            return f"Invalid agent definition: {exc}"

        return self._render_agent_detail(agent_def)

    @staticmethod
    def _render_agent_list(
        agents: list["AgentDef"],
        session: "AgentSession",
    ) -> str:
        """Render all available agents."""
        if not agents:
            return "No agents available."

        current_agent_id = session.agent.agent_def.id
        lines = ["**Agents:**"]
        for agent in agents:
            marker = " (current)" if agent.id == current_agent_id else ""
            description = agent.description or agent.name
            lines.append(f"- `{agent.id}`: {description}{marker}")

        return "\n".join(lines)

    @staticmethod
    def _render_agent_detail(agent_def: "AgentDef") -> str:
        """Render details for a single agent."""
        lines = [
            f"**Agent:** `{agent_def.id}`",
            f"**Name:** {agent_def.name}",
            f"**Description:** {agent_def.description}",
            f"**LLM:** {agent_def.llm.model}",
            "",
            "---",
            "",
            "**AGENT.md:**",
            "```",
            agent_def.agent_md,
            "```",
        ]

        if agent_def.soul_md:
            lines.extend(
                [
                    "",
                    "**SOUL.md:**",
                    "```",
                    agent_def.soul_md,
                    "```",
                ]
            )

        return "\n".join(lines)
