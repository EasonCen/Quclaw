"""Slash command registry for registration, resolution, and dispatch."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.commands.base import Command

if TYPE_CHECKING:
    from core.agent import AgentSession


@dataclass(slots=True, frozen=True)
class ResolvedCommand:
    """Parsed slash command input."""

    name: str
    args: str
    command: Command | None


class CommandRegistry:
    """Registry for slash commands."""

    def __init__(self) -> None:
        """Initialize an empty command registry."""
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        """Register a command and its aliases."""
        name = self._normalize(command.name)
        aliases = [self._normalize(alias) for alias in command.aliases]

        existing = self._commands.get(name)
        if existing is not None and existing is not command:
            raise ValueError(f"Command already registered: {command.name}")

        for alias in aliases:
            if alias == name:
                continue

            existing_command = self._commands.get(alias)
            if existing_command is not None and existing_command is not command:
                raise ValueError(f"Alias conflicts with command name: {alias}")

            existing_alias = self._aliases.get(alias)
            if existing_alias is not None and existing_alias is not command:
                raise ValueError(f"Alias already registered: {alias}")

        self._commands[name] = command
        for alias in aliases:
            if alias != name:
                self._aliases[alias] = command

    def get(self, name: str) -> Command | None:
        """Get a command by name or alias."""
        key = self._normalize(name)
        return self._commands.get(key) or self._aliases.get(key)

    def list_all(self) -> list[Command]:
        """List commands in registration order."""
        return list(self._commands.values())

    def render_help(self) -> str:
        """Render a help message for all registered commands."""
        lines = ["Available commands:"]
        for command in self.list_all():
            alias_text = ""
            if command.aliases:
                alias_text = " (" + ", ".join(f"`/{alias}`" for alias in command.aliases) + ")"
            lines.append(f"- `/{command.name}`{alias_text}: {command.description}")
        return "\n".join(lines)

    def resolve(self, text: str) -> ResolvedCommand | None:
        """Resolve slash command input into a structured result."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        body = stripped[1:].strip()
        if not body:
            return ResolvedCommand(name="", args="", command=None)

        parts = body.split(maxsplit=1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        return ResolvedCommand(name=name, args=args, command=self.get(name))

    async def dispatch(self, text: str, session: "AgentSession") -> str | None:
        """Dispatch a slash command and return its response."""
        resolved = self.resolve(text)
        if resolved is None:
            return None

        if not resolved.name:
            return "Unknown command: `/`. Use `/help` to see available commands."

        if resolved.command is None:
            return f"Unknown command: `/{resolved.name}`. Use `/help` to see available commands."

        return await resolved.command.execute(resolved.args, session)

    @classmethod
    def with_builtins(cls) -> "CommandRegistry":
        """Create a command registry with builtin slash commands."""

        from core.commands.handlers import (
            AgentCommand,
            CompactCommand,
            ContextCommand,
            CronsCommand,
            HelpCommand,
            SessionCommand,
            SkillsCommand,
            RouteCommand,
            BindingsCommand,
        )

        registry = cls()
        registry.register(SessionCommand())
        registry.register(HelpCommand())
        registry.register(ContextCommand())
        registry.register(CompactCommand())
        registry.register(SkillsCommand())
        registry.register(AgentCommand())
        registry.register(RouteCommand())
        registry.register(BindingsCommand())
        registry.register(CronsCommand())
        return registry

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize command names and aliases for fast lookups."""
        return name.strip().lstrip("/").lower()
