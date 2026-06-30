import importlib.metadata
from pathlib import Path
from typing import Dict

from pydantic import BaseModel, ConfigDict, Field

SpecFlow_JSON_FILENAME = "gain.json"


def _specflow_version() -> str:
    try:
        return importlib.metadata.version("specflow-mcp-server")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class GainVersions(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    specflow: str = Field(default_factory=_specflow_version)

    def update_specflow(self, version: str) -> None:
        self.specflow = version


class GainJson(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    description: str = ""
    services_description: Dict[str, str] = Field(default_factory=dict, alias="servicesDescription")
    coding_agents: list[str] = Field(default_factory=list, alias="codingAgents")
    versions: GainVersions = Field(default_factory=GainVersions)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, by_alias=True)

    @classmethod
    def load(cls, project_root: Path) -> "GainJson":
        path = project_root / SpecFlow_JSON_FILENAME
        if path.exists():
            return cls.model_validate_json(path.read_text())
        return cls()

    def save(self, project_root: Path) -> None:
        path = project_root / SpecFlow_JSON_FILENAME
        path.write_text(self.to_json())

    def update_description(self, description: str) -> None:
        self.description = description

    def add_coding_agent(self, agent: str) -> None:
        if agent not in self.coding_agents:
            self.coding_agents.append(agent)

    def remove_coding_agent(self, agent: str) -> None:
        self.coding_agents = [a for a in self.coding_agents if a != agent]
