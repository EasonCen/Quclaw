"""Private file store for scenario cases and pending registry."""

from __future__ import annotations

import json
import uuid

from pathlib import Path

from scenario.model import CaseStatus, ScenarioCase, ScenarioRegistry


class ScenarioStore:
    """Read and write private scenario state files."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.cases_path = workspace / "cases"
        self.registry_path = workspace / "pending_sources.json"

    def load_registry(self) -> ScenarioRegistry:
        """Load pending/source registry."""
        if not self.registry_path.exists():
            return ScenarioRegistry()
        with self.registry_path.open(encoding="utf-8") as f:
            return ScenarioRegistry.model_validate(json.load(f))

    def save_registry(self, registry: ScenarioRegistry) -> None:
        """Persist pending/source registry."""
        self._write_json(self.registry_path, registry.model_dump(mode="json"))

    def get_active_case(self) -> ScenarioCase | None:
        """Return the single active case, if any."""
        for case_path in sorted(self.cases_path.glob("*.json")):
            case = self.load_case(case_path)
            if case.status == CaseStatus.ACTIVE:
                return case
        return None

    def get_case(self, case_id: str) -> ScenarioCase | None:
        """Return one case by id, if it exists."""
        case_path = self.cases_path / f"{case_id}.json"
        if not case_path.exists():
            return None
        return self.load_case(case_path)

    def load_case(self, path: Path) -> ScenarioCase:
        """Load one case file."""
        with path.open(encoding="utf-8") as f:
            return ScenarioCase.model_validate(json.load(f))

    def save_case(self, case: ScenarioCase) -> None:
        """Persist one case."""
        self.cases_path.mkdir(parents=True, exist_ok=True)
        self._write_json(self.cases_path / f"{case.case_id}.json", case.model_dump(mode="json"))

    def save_archive_document(self, case_id: str, name: str, content: str) -> Path:
        """Persist one archive document under cases/archives."""
        archive_dir = self.cases_path / "archives" / case_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / name
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        temp_path.replace(path)
        return path

    def next_case_id(self) -> str:
        """Return a simple unique case id."""
        return f"RES-{uuid.uuid4().hex[:8].upper()}"

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp_path.replace(path)
