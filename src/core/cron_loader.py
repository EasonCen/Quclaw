"""Cron job definition loader"""

import logging
from datetime import datetime

from croniter import croniter
from pydantic import BaseModel, ValidationError, field_validator
from typing import TYPE_CHECKING, Any

from utils.def_loader import (
    DefNotFoundError,
    InvalidDefError,
    discover_definitions,
    parse_definition,
)

if TYPE_CHECKING:
    from utils.config import Config

logger = logging.getLogger(__name__)

class CronDef(BaseModel):
    """Loaded cron job definition."""

    id: str
    name: str
    description: str
    agent: str
    schedule: str
    prompt: str
    one_off: bool = False

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, value: str)-> str:
        """Validate cron expression and enforce 5-minute minimum granularity."""
        parts = value.split()
        if len(parts) != 5:
            raise ValueError("Cron schedule must use 5 fields: minute hour day month weekday")

        if not croniter.is_valid(value):
            raise ValueError(f"Invalid cron expression: {value}")

        base = datetime(2024, 1, 1, 0, 0)
        iterator = croniter(value, base)

        previous = iterator.get_next(datetime)
        for _ in range(20):
            current = iterator.get_next(datetime)
            gap_minutes = (current - previous).total_seconds() / 60

            if gap_minutes < 5:
                raise ValueError(
                    "Cron schedule must have a minimum granularity of 5 minutes."
                )

            previous = current

        return value
    
class CronLoader:
    """Load cron job definitions from CRON.md files."""

    @staticmethod
    def from_config(config: "Config") -> "CronLoader":
        return CronLoader(config)
    
    def __init__(self, config: "Config"):
        """Initialize CronLoader."""
        self.config = config
        self.config.crons_path.mkdir(parents=True, exist_ok=True)

    def discover_crons(self) -> list[CronDef]:
        """Scan crons directory and load all valid cron definitions."""
        return discover_definitions(
            self.config.crons_path, "CRON.md",
            self._parse_cron_def,
        )
    
    def _parse_cron_def(
            self,
            def_id: str,
            frontmatter: dict[str, Any],
            body: str,
        ) -> CronDef | None:
        """Parse cron definition from frontmatter (callback for parse_definition)."""
        if body.strip() == "":
            logger.warning(f"Cron definition {def_id} has empty prompt body.")
            return None
        try:
            return CronDef(
                id=def_id,
                name=frontmatter["name"],  # type: ignore[misc]
                description=frontmatter["description"],  # type: ignore[misc]
                agent=frontmatter["agent"],  # type: ignore[misc]
                schedule=frontmatter["schedule"],  # type: ignore[misc]
                prompt=body.strip(),
                one_off=frontmatter.get("one_off", False),
            )

        except ValidationError as e:
            logger.warning(f"Invalid cron definition in {def_id}: {e}")
            return None
        except KeyError as e:
            logger.warning(f"Missing required field {e} in cron definition {def_id}: {e}")
            return None
        
    def load(self, cron_id: str) -> CronDef:
        """Load cron by ID."""
        cron_file = self.config.crons_path / cron_id / "CRON.md"
        if not cron_file.exists():
            raise DefNotFoundError("cron", cron_id)
        
        try:
            content = cron_file.read_text(encoding="utf-8")
            cron_def = parse_definition(content, cron_id, self._parse_cron_def)
        except InvalidDefError:
            raise
        except Exception as e:
            raise InvalidDefError("cron", cron_id, str(e))
        
        if cron_def is None:
            raise InvalidDefError("cron", cron_id, "validation failed.")

        return cron_def
