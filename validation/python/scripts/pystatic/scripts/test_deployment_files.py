"""Self-test for `deployment_files`."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import deployment_files as rule

POLICY = rule.Policy(
    allowed_keys=frozenset({"SEED_CATALOG_PATH"}),
    forbidden_trees=(PurePosixPath("tests"), PurePosixPath("scripts/demo-data")),
)


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_the_input_rule_refuses_test_and_seed_inputs_only() -> None:
    for key in ("APP_FIXTURE_DIR", "SEED_MODE", "HTTP_CASSETTE_DIR", "RECORDING_PROFILE"):
        assert POLICY.names_test_input(key), key
    for key in ("MAX_RECORDING_SECONDS", "COMMAND_REPLAY_LIMIT", "SEED_CATALOG_PATH", "DB_URL"):
        assert not POLICY.names_test_input(key), key


def test_the_compose_rule_refuses_every_test_or_seed_input_and_mount() -> None:
    composition: dict[str, Any] = {
        "services": {
            "api": {
                "environment": {"APP_FIXTURE_DIR": "/fixtures", "DB_URL": "postgres://db"},
                "volumes": [
                    "./tests/fixtures:/fixtures:ro",
                    "api_data:/data",
                    {"type": "bind", "source": "./scripts/demo-data/data", "target": "/seed"},
                ],
                "env_file": "./tests/runtime.env",
            },
            "worker": {
                "environment": ["SEED_MODE=demo", "MAX_RECORDING_SECONDS=60"],
                "env_file": [".env", {"path": "./scripts/demo-data/seed.env", "required": False}],
            },
        },
        "secrets": {"admin": {"file": "./scripts/demo-data/admin.txt"}},
        "configs": {"index": {"file": "${HOME:?}/.config/app.json"}},
    }
    assert rule.compose_problems(composition, POLICY) == [
        ("deployment-test-input", "service api sets APP_FIXTURE_DIR"),
        ("deployment-test-input", "service worker sets SEED_MODE"),
        ("deployment-test-mount", "mounts ./tests/fixtures"),
        ("deployment-test-mount", "mounts ./scripts/demo-data/data"),
        ("deployment-test-mount", "mounts ./tests/runtime.env"),
        ("deployment-test-mount", "mounts ./scripts/demo-data/seed.env"),
        ("deployment-test-mount", "mounts ./scripts/demo-data/admin.txt"),
    ]


def test_the_mount_rule_matches_whole_trees_only() -> None:
    assert POLICY.under_forbidden_tree("./scripts/demo-data/data/catalog.json")
    assert not POLICY.under_forbidden_tree("./scripts/demo-dataset")
    assert not POLICY.under_forbidden_tree("/var/cache/api")
    assert not POLICY.under_forbidden_tree("api_data")


def test_the_workflow_rule_refuses_a_full_history_checkout_with_blobs() -> None:
    def checkout(**options: object) -> dict[str, Any]:
        return {"jobs": {"build": {"steps": [{"uses": "actions/checkout@v6", "with": options}]}}}

    find = rule.full_history_checkouts_without_blob_filter
    assert find(checkout(**{"fetch-depth": 0})) == ["build"]
    assert find(checkout(**{"fetch-depth": "0"})) == ["build"]
    assert find(checkout(**{"fetch-depth": 0, "filter": "blob:none"})) == []
    assert find(checkout()) == []


def test_check_reads_compose_and_workflows_from_the_root(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    environment:\n      APP_FIXTURE_DIR: /f\n"
        "    volumes: !override\n      - ./tests/data:/data\n"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v6\n"
        "        with:\n          fetch-depth: 0\n"
    )
    files.configure(tmp_path)
    assert [(f.path, f.rule) for f in rule.check()] == [
        (".github/workflows/ci.yml", "ci-full-history-blobs"),
        ("compose.yaml", "deployment-test-input"),
        ("compose.yaml", "deployment-test-mount"),
    ]


def test_configured_compose_list_and_allowed_keys(tmp_path: Path) -> None:
    (tmp_path / "deploy.yml").write_text("services:\n  api:\n    environment:\n      - SEED_X=1\n")
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text('[deployment-files]\ncompose = ["deploy.yml"]\nallowed-keys = ["SEED_X"]\n')
    files.configure(tmp_path)
    config.configure(cfg)
    assert rule.check() == []
