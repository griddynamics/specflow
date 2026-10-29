"""Production Compose files set and mount no test or seed input, and CI checkouts of the full
history filter out historical blobs. Reads `[deployment-files]` (all keys optional).
Doc: /README.md#deployment-files"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "deployment-files"
DOC = "/README.md#deployment-files"
DEFAULT_COMPOSE = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
#: Words that name a test or seed input wherever they occur in a key.
DEFAULT_INPUT_WORDS = ("FIXTURE", "FIXTURES", "CASSETTE", "CASSETTES", "SEED")
#: Words that name a test input only where the key also locates or selects one.
DEFAULT_REPLAY_WORDS = ("RECORDING", "RECORDINGS", "REPLAY", "REPLAYS")
DEFAULT_LOCATOR_WORDS = ("DIR", "PATH", "FILE", "PROFILE", "MODE", "URL", "BUNDLE")
DEFAULT_FORBIDDEN_TREES = ("tests",)
_CHECKOUT = re.compile(r"^actions/checkout@")


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's merge tags (`!reset`, `!override`) as the plain values they carry."""


def _tagged(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    raise yaml.constructor.ConstructorError(
        None, None, f"unexpected {type(node).__name__} under {node.tag}", node.start_mark
    )


for _tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(_tag, _tagged)


_INPUT = frozenset(DEFAULT_INPUT_WORDS)
_REPLAY = frozenset(DEFAULT_REPLAY_WORDS)
_LOCATOR = frozenset(DEFAULT_LOCATOR_WORDS)
_FORBIDDEN = tuple(PurePosixPath(tree) for tree in DEFAULT_FORBIDDEN_TREES)


@dataclass(frozen=True)
class Policy:
    input_words: frozenset[str] = _INPUT
    replay_words: frozenset[str] = _REPLAY
    locator_words: frozenset[str] = _LOCATOR
    allowed_keys: frozenset[str] = frozenset()
    forbidden_trees: tuple[PurePosixPath, ...] = _FORBIDDEN

    def names_test_input(self, key: str) -> bool:
        """Whether a configuration key names a fixture, recording, replay or seed input."""
        if key in self.allowed_keys:
            return False
        words = set(key.upper().split("_"))
        return bool(
            words & self.input_words or (words & self.replay_words and words & self.locator_words)
        )

    def under_forbidden_tree(self, source: str) -> bool:
        relative = PurePosixPath(source.removeprefix("./"))
        if relative.is_absolute() or source.startswith(("$", "~")):
            return False
        return any(relative == tree or tree in relative.parents for tree in self.forbidden_trees)


def load_compose(path: Path) -> dict[str, Any]:
    loader = ComposeLoader(path.read_text(encoding="utf-8"))
    try:
        loaded = loader.get_single_data()
    finally:
        loader.dispose()
    return loaded if isinstance(loaded, dict) else {}


def _environment_keys(service: dict[str, Any]) -> list[str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return [str(item).split("=", 1)[0] for item in environment]
    return [str(key) for key in environment]


def _env_files(env_file: object) -> list[str]:
    """The files of an `env_file` entry: one path, or a list of paths or `{path: ...}` maps."""
    if env_file is None:
        return []
    entries = env_file if isinstance(env_file, list) else [env_file]
    return [str(entry["path"]) if isinstance(entry, dict) else str(entry) for entry in entries]


def mount_sources(composition: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for service in (composition.get("services") or {}).values():
        for volume in service.get("volumes") or []:
            if isinstance(volume, dict):
                sources.append(str(volume.get("source", "")))
            elif ":" in str(volume):
                sources.append(str(volume).split(":", 1)[0])
        sources.extend(_env_files(service.get("env_file")))
    for section in ("secrets", "configs"):
        sources.extend(
            str(entry["file"])
            for entry in (composition.get(section) or {}).values()
            if isinstance(entry, dict) and "file" in entry
        )
    return sources


def compose_problems(composition: dict[str, Any], policy: Policy) -> list[tuple[str, str]]:
    """`(rule, message)` for each test input a composition sets and each test file it mounts."""
    found = [
        ("deployment-test-input", f"service {name} sets {key}")
        for name, service in (composition.get("services") or {}).items()
        for key in _environment_keys(service)
        if policy.names_test_input(key)
    ]
    found += [
        ("deployment-test-mount", f"mounts {source}")
        for source in mount_sources(composition)
        if policy.under_forbidden_tree(source)
    ]
    return found


def full_history_checkouts_without_blob_filter(workflow: dict[str, Any]) -> list[str]:
    """The jobs whose `actions/checkout` fetches the full history with its historical blobs."""
    found = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not _CHECKOUT.match(str(step.get("uses", ""))):
                continue
            options = step.get("with") or {}
            if str(options.get("fetch-depth", "1")) == "0" and options.get("filter") != "blob:none":
                found.append(str(job_name))
    return found


def _policy(table: dict[str, Any]) -> Policy:
    def words(key: str, default: tuple[str, ...]) -> frozenset[str]:
        return frozenset(w.upper() for w in config.string_list(table, key, list(default)))

    trees = config.string_list(table, "forbidden-trees", list(DEFAULT_FORBIDDEN_TREES))
    return Policy(
        input_words=words("input-words", DEFAULT_INPUT_WORDS),
        replay_words=words("replay-words", DEFAULT_REPLAY_WORDS),
        locator_words=words("locator-words", DEFAULT_LOCATOR_WORDS),
        allowed_keys=frozenset(config.string_list(table, "allowed-keys", [])),
        forbidden_trees=tuple(PurePosixPath(t) for t in trees),
    )


def _finding(path: Path, rule: str, message: str, fix: str) -> Finding:
    return Finding(repo_rel(path), 1, 1, rule, message, fix=fix, doc=DOC)


def check() -> list[Finding]:
    table = config.section(STEP)
    root = target_root()
    policy = _policy(table)
    compose = config.string_list(
        table, "compose", [name for name in DEFAULT_COMPOSE if (root / name).is_file()]
    )
    findings: list[Finding] = []
    for name in compose:
        for rule, message in compose_problems(load_compose(root / name), policy):
            findings.append(
                _finding(root / name, rule, message, "keep test and seed inputs out of production")
            )
    workflows_dir = root / str(table.get("workflows", ".github/workflows"))
    workflows = sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])
    for path in workflows:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in full_history_checkouts_without_blob_filter(workflow or {}):
            message = f"job {job} checks out the full history with every historical blob"
            findings.append(
                _finding(path, "ci-full-history-blobs", message, "add `filter: blob:none`")
            )
    return sorted(findings, key=lambda f: (f.path, f.rule, f.message))


def run() -> StepResult:
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
