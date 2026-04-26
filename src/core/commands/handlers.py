"""Built-in slash command handlers."""
from typing import TYPE_CHECKING

from core.commands.base import Command
from utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
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
