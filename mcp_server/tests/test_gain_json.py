"""Unit tests for the GainJson schema model."""

import json

import pytest

from schemas.gain_json import SpecFlow_JSON_FILENAME, GainJson, GainVersions


class TestGainVersions:
    def test_defaults(self):
        v = GainVersions()
        assert isinstance(v.specflow, str)

    def test_update_specflow(self):
        v = GainVersions()
        v.update_specflow("9.9.9")
        assert v.specflow == "9.9.9"


class TestGainJsonDefaults:
    def test_default_fields(self):
        g = GainJson()
        assert g.description == ""
        assert g.coding_agents == []
        assert g.services_description == {}
        assert isinstance(g.versions, GainVersions)

    def test_coding_agents_instances_are_independent(self):
        a, b = GainJson(), GainJson()
        a.coding_agents.append("x")
        assert b.coding_agents == []

    def test_services_description_instances_are_independent(self):
        a, b = GainJson(), GainJson()
        a.services_description["svc"] = "desc"
        assert b.services_description == {}


class TestGainJsonSerialization:
    def test_to_json_uses_camel_case_alias(self):
        g = GainJson()
        data = json.loads(g.to_json())
        assert "codingAgents" in data
        assert "coding_agents" not in data
        assert "servicesDescription" in data
        assert "services_description" not in data

    def test_to_json_contains_expected_keys(self):
        g = GainJson()
        data = json.loads(g.to_json())
        assert "description" in data
        assert "servicesDescription" in data
        assert "codingAgents" in data
        assert "versions" in data
        assert "specflow" in data["versions"]
        assert "rosetta" not in data["versions"]

    def test_to_json_serializes_services_description(self):
        g = GainJson(services_description={"svc": "does X"})
        data = json.loads(g.to_json())
        assert data["servicesDescription"] == {"svc": "does X"}

    def test_to_json_is_valid_json(self):
        g = GainJson()
        raw = g.to_json()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


class TestGainJsonLoadSave:
    def test_save_creates_file(self, tmp_path):
        GainJson().save(tmp_path)
        assert (tmp_path / SpecFlow_JSON_FILENAME).exists()

    def test_save_round_trips(self, tmp_path):
        g = GainJson(
            description="my project",
            services_description={"svc": "desc"},
            coding_agents=["agent-a"],
            versions=GainVersions(specflow="1.0"),
        )
        g.save(tmp_path)

        loaded = GainJson.load(tmp_path)
        assert loaded.description == "my project"
        assert loaded.services_description == {"svc": "desc"}
        assert "agent-a" in loaded.coding_agents
        assert loaded.versions.specflow == "1.0"

    def test_load_returns_defaults_when_missing(self, tmp_path):
        g = GainJson.load(tmp_path)
        assert g.description == ""
        assert g.coding_agents == []
        assert g.services_description == {}

    def test_load_parses_existing_file(self, tmp_path):
        data = {
            "description": "existing",
            "servicesDescription": {"svc": "does X"},
            "codingAgents": ["x"],
            "versions": {"rosetta": "1", "specflow": "2"},
        }
        (tmp_path / SpecFlow_JSON_FILENAME).write_text(json.dumps(data))

        g = GainJson.load(tmp_path)
        assert g.description == "existing"
        assert g.services_description == {"svc": "does X"}
        assert g.coding_agents == ["x"]
        assert g.versions.specflow == "2"

    def test_construction_accepts_alias(self):
        g = GainJson(servicesDescription={"svc": "desc"})
        assert g.services_description == {"svc": "desc"}


class TestGainJsonUpdateMethods:
    def test_update_description(self):
        g = GainJson()
        g.update_description("hello")
        assert g.description == "hello"

    def test_add_coding_agent(self):
        g = GainJson()
        g.add_coding_agent("agent-a")
        assert "agent-a" in g.coding_agents

    def test_add_coding_agent_deduplicates(self):
        g = GainJson()
        g.add_coding_agent("agent-a")
        g.add_coding_agent("agent-a")
        assert g.coding_agents.count("agent-a") == 1

    def test_remove_coding_agent(self):
        g = GainJson()
        g.add_coding_agent("agent-a")
        g.add_coding_agent("agent-b")
        g.remove_coding_agent("agent-a")
        assert "agent-a" not in g.coding_agents
        assert "agent-b" in g.coding_agents

    def test_remove_coding_agent_noop_when_absent(self):
        g = GainJson()
        g.remove_coding_agent("ghost")
        assert g.coding_agents == []
