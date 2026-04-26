"""Skill loader for discovering and loading skills."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

from utils.def_loader import DefNotFoundError, discover_definitions

if TYPE_CHECKING:
    from utils.config import Config

logger = logging.getLogger(__name__)


class SkillDef(BaseModel):
    """Loaded skill definition."""
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    content: str

class SkillLoader:
    """Load and manage skill definitions from filesystem"""

    @staticmethod
    def from_config(config: "Config") -> "SkillLoader":
        """Create SkillLoader from config"""
        return SkillLoader(config)
    
    def __init__(self, config: "Config"):
        self.config = config

    def discover_skills(self) -> list[SkillDef]:
        """Scan skills directory and return list of valid SkillDef."""
        return discover_definitions(
            self.config.skills_path,"SKILL.md", self._parse_skill_def
        )
    
    def _parse_skill_def(
            self, def_id: str, frontmatter: dict[str,Any], body: str
    ) -> SkillDef | None:
        """Parse skill definition from frontmatter (callback for discover_definitions)."""
        try:
            return SkillDef(
                id=def_id,
                name=frontmatter["name"],  # type: ignore[misc]
                description=frontmatter["description"],  # type: ignore[misc]
                content=self._render_content(body.strip()),
            )
        except ValidationError as e:
            logger.warning(f"Invalid skill '{def_id}': {e}")
            return None
        except KeyError as e:
            logger.warning(f"Missing required field in skill '{def_id}': {e}")
            return None

    def _render_content(self, content: str) -> str:
        """Render config-backed placeholders inside skill instructions."""
        rendered = content
        for key, value in self._template_vars().items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        return rendered

    def _template_vars(self) -> dict[str, str]:
        """Return workspace paths available to SKILL.md files."""
        field_names = (
            "workspace",
            "agents_path",
            "skills_path",
            "crons_path",
            "history_path",
            "logging_path",
            "event_path",
            "default_agent",
        )
        return {
            field_name: self._format_template_value(getattr(self.config, field_name))
            for field_name in field_names
            if hasattr(self.config, field_name)
        }

    @staticmethod
    def _format_template_value(value: Any) -> str:
        """Format template values for use in markdown and tool arguments."""
        if isinstance(value, Path):
            return value.resolve().as_posix()
        return str(value)

    def load_skill(self, skill_id: str) -> SkillDef:
        """Load full skill definition by ID."""
        skills = self.discover_skills()
        for skill in skills:
            if skill.id == skill_id:
                return skill

        raise DefNotFoundError("skill", skill_id)
