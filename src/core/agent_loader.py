"""Agent definition loader."""

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from utils.config import Config, LLMConfig
from utils.def_loader import(
    DefNotFoundError,
    InvalidDefError,
    discover_definitions,
    parse_definition,
)

class AgentDef(BaseModel):
    """Loaded agent definition with merged settings."""

    id: str
    name: str
    description: str = ""
    agent_md: str
    soul_md: str | None = None
    llm: LLMConfig
    allow_skills: bool = False
    max_concurrency: int = Field(default=1, ge=1)



class AgentLoader:
    """Loads agent definitions from AGENT.md files."""

    @staticmethod
    def from_config(config: Config) -> "AgentLoader":
        return AgentLoader(config)

    
    def __init__(self, config: Config):
        """Initialize AgentLoader."""
        self.config = config

    def load(self, agent_id: str) -> AgentDef:
        """Load agent by ID."""
        agent_file = self.config.agents_path / agent_id / "AGENT.md"
        if not agent_file.exists():
            raise DefNotFoundError("agent", agent_id)

        try:
            content = agent_file.read_text(encoding="utf-8")
            agent_def = parse_definition(content, agent_id, self._parse_agent_def)
        except InvalidDefError:
            raise
        except Exception as e:
            raise InvalidDefError("agent", agent_id, str(e))

        return self._with_soul_md(agent_def)
    
    def discover_agents(self) -> list[AgentDef]:
        """Scan agents directory and load all valid agent definitions."""
        agents = discover_definitions(
            self.config.agents_path, "AGENT.md",
            self._parse_agent_def,
        )
        return [self._with_soul_md(agent) for agent in agents]


    def _parse_agent_def(
        self, def_id: str, frontmatter: dict[str, Any], body: str
    ) -> AgentDef:
        """Parse agent definition from frontmatter (callback for parse_definition)."""
        llm_overrides = frontmatter.get("llm")
        merged_llm = self._merge_llm_config(llm_overrides)

        try:
            return AgentDef(
                id=def_id,
                name=frontmatter["name"],  # type: ignore[misc]
                description=frontmatter.get("description", ""),
                agent_md=body.strip(),
                llm=merged_llm,
                allow_skills=frontmatter.get("allow_skills", False),
                max_concurrency=frontmatter.get("max_concurrency", 1),
            )
        except ValidationError as e:
            raise InvalidDefError("agent", def_id, str(e))

    def _merge_llm_config(self, agent_llm: dict[str, Any] | None) -> LLMConfig:
        """Deep merge agent's llm config with global defaults."""
        base = self.config.llm.model_dump()
        if agent_llm:
            base = {**base, **agent_llm}
        return LLMConfig(**base)

    def _with_soul_md(self, agent_def: AgentDef) -> AgentDef:
        """Attach optional SOUL.md content to an agent definition."""
        soul_file = self.config.agents_path / agent_def.id / "SOUL.md"
        if not soul_file.exists():
            return agent_def

        return agent_def.model_copy(
            update={"soul_md": soul_file.read_text(encoding="utf-8").strip()}
        )

