from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from re import Pattern
from typing import TYPE_CHECKING

from utils.config import SourceSessionConfig

if TYPE_CHECKING:
    from core.context import SharedContext
    from runtime.events import EventSource


@dataclass
class Binding:
    """A routing binding that matches sources to agents."""

    agent: str
    value: str
    tier: int = field(init=False)
    pattern: Pattern = field(init=False)

    def __post_init__(self) -> None:
        self.agent = self.agent.strip()
        self.value = self.value.strip()

        if not self.agent:
            raise ValueError("routing binding agent must not be empty")
        if not self.value:
            raise ValueError("routing binding value must not be empty")

        self.pattern = re.compile(f"^{self.value}$")
        self.tier = self._compute_tier()

    # tier 0: 精确匹配，最优先
    # tier 1: 具体正则，其次
    # tier 2: 带 .* 的泛匹配，最后
    def _compute_tier(self) -> int:
        """Compute specificity tier."""
        if not any(c in self.value for c in r".*+?[](){}|^$\\"):
            return 0
        if ".*" in self.value:
            return 2
        return 1


@dataclass
class RoutingTable:
    """Routes sources to agents using regex bindings."""

    context: SharedContext
    bindings: list[Binding] | None = field(default=None, init=False)
    _config_signature: tuple[tuple[str, str], ...] | None = field(
        default=None,
        init=False,
    )

    def _load_bindings(self) -> list[Binding]:
        """Load and sort bindings from config. Cached until config changes."""
        raw_bindings = self._get_raw_bindings()
        config_signature = tuple(
            (raw_binding["value"], raw_binding["agent"])
            for raw_binding in raw_bindings
        )

        if (
            self.bindings is not None
            and self._config_signature == config_signature
        ):
            return self.bindings

        indexed_bindings = [
            (
                index,
                Binding(
                    value=raw_binding["value"],
                    agent=raw_binding["agent"],
                ),
            )
            for index, raw_binding in enumerate(raw_bindings)
        ]
        indexed_bindings.sort(
            key=lambda item: (
                item[1].tier,
                -len(item[1].value),
                item[0],
            )
        )

        self.bindings = [binding for _, binding in indexed_bindings]
        self._config_signature = config_signature
        return self.bindings

    def resolve(self, source: str) -> str:
        """Return agent for source, falling back to default_agent if no match."""
        for binding in self._load_bindings():
            if binding.pattern.match(source):
                return binding.agent

        return self.context.config.default_agent

    def persist_binding(self, source_pattern: str, agent_id: str) -> None:
        """Add and persist a routing binding to config.user.json."""
        binding = Binding(
            value=source_pattern.strip(),
            agent=agent_id.strip(),
        )
        raw_bindings = self._get_raw_bindings()
        new_binding = {
            "value": binding.value,
            "agent": binding.agent,
        }

        updated = False
        next_bindings = []
        for raw_binding in raw_bindings:
            if raw_binding["value"] == binding.value:
                next_bindings.append(new_binding)
                updated = True
            else:
                next_bindings.append(raw_binding)

        if not updated:
            next_bindings.append(new_binding)

        routing = dict(self.context.config.routing)
        routing["bindings"] = next_bindings

        self.context.config.set_user("routing", routing)
        self.context.config.routing = routing
        self.bindings = None
        self._config_signature = None

    def reset_matching_source_sessions(
        self,
        source_pattern: str,
        agent_id: str,
    ) -> int:
        """Reset cached source sessions affected by a route binding."""
        binding = Binding(
            value=source_pattern.strip(),
            agent=agent_id.strip(),
        )
        reset_count = 0

        for source, source_session in list(self.context.config.sources.items()):
            if not binding.pattern.match(source):
                continue
            if self.resolve(source) != binding.agent:
                continue

            session_info = self.context.history_store.get_session_info(
                source_session.session_id
            )
            if (
                session_info is not None
                and session_info.agent_id == binding.agent
            ):
                continue

            if self.context.config.remove_runtime_source(source):
                reset_count += 1

        return reset_count

    def get_or_create_session_id(self, source: "EventSource") -> str:
        """Get or create a stable session ID for a source."""
        source_str = str(source)
        source_session = self.context.config.sources.get(source_str)
        if source_session is not None:
            return source_session.session_id

        session_id = uuid.uuid4().hex
        self.config_source_session_cache(source_str, session_id)
        return session_id

    def config_source_session_cache(
        self,
        source_str: str,
        session_id: str | None,
    ) -> None:
        """Config session cache for a source."""
        if session_id is None:
            return
        source_key = source_str.strip()
        if not source_key:
            raise ValueError("source must not be empty")

        self.context.config.set_runtime_source(
            source_key,
            SourceSessionConfig(session_id=session_id),
        )

    def _get_raw_bindings(self) -> list[dict[str, str]]:
        """Return normalized binding dictionaries from config."""
        routing = self.context.config.routing
        if not isinstance(routing, dict):
            raise ValueError("routing must be a mapping")

        raw_bindings = routing.get("bindings", [])
        if raw_bindings is None:
            raw_bindings = []
        if not isinstance(raw_bindings, list):
            raise ValueError("routing.bindings must be a list")

        bindings: list[dict[str, str]] = []
        for index, raw_binding in enumerate(raw_bindings):
            if not isinstance(raw_binding, dict):
                raise ValueError(
                    f"routing.bindings[{index}] must be a mapping"
                )

            value = raw_binding.get("value")
            agent = raw_binding.get("agent")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"routing.bindings[{index}].value must be a "
                    "non-empty string"
                )
            if not isinstance(agent, str) or not agent.strip():
                raise ValueError(
                    f"routing.bindings[{index}].agent must be a "
                    "non-empty string"
                )

            bindings.append(
                {
                    "value": value.strip(),
                    "agent": agent.strip(),
                }
            )

        return bindings
