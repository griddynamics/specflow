#!/usr/bin/env python3
"""Licence gate: per-image dependency sets against a licence allowlist, without building images.

    licence_gate.py --config backend/licence-gate.toml [--env ROLE=PYTHON ...] [--workdir DIR]

For each role (image) of the config it installs the locked runtime set the Dockerfile installs
(`uv sync --frozen --no-dev --no-install-project --extra ...`) into a scratch environment, lists the
installed licences with pip-licenses and fails when a forbidden package is present or a licence is not on
the allowlist. It also checks the lockfile for forbidden names and that NOTICE names the components the
config (and the direct dependencies of the notice extra) require. `--env ROLE=PYTHON` checks an existing
environment instead of syncing one (a dry run against a modified set). Python 3.11+, stdlib only.

Exit codes: 0 clean, 1 findings, 2 tool or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_FINDINGS, EXIT_TOOL_ERROR = 0, 1, 2
_OPERATORS = re.compile(r"(\(|\)|\s+AND\s+|\s+OR\s+|\s+WITH\s+|;|,)")


class ToolError(Exception):
    """A configuration or tool failure, as opposed to a finding."""


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    exceptions: frozenset[str]
    aliases: dict[str, str]
    overrides: dict[str, str]
    override_versions: dict[str, str]
    forbidden: frozenset[str]

    def expand(self, expression: str) -> str:
        """Replace the alias names (some hold parentheses) by their SPDX ids, longest name first, in one pass."""
        if not self.aliases:
            return expression
        names = "|".join(re.escape(k) for k in sorted(self.aliases, key=len, reverse=True))
        return re.sub(rf"(?<![\w-])(?:{names})(?![\w-])", lambda m: self.aliases[m.group(0).lower()], expression, flags=re.IGNORECASE)

    def operand(self, text: str) -> str | None:
        """The SPDX id of one licence name, or None when it is not on the allowlist."""
        text = text.strip()
        spdx = self.aliases.get(text.lower(), text)
        return spdx if spdx in self.allowed else None


class _Parser:
    """SPDX-style expression: AND binds tighter than OR; `X WITH Y` needs both X and Y allowed."""

    def __init__(self, policy: Policy, expression: str) -> None:
        parts = (p.strip() for p in _OPERATORS.split(policy.expand(expression)))
        self.tokens = [("AND" if p == ";" or p == "," else p) for p in parts if p]
        self.pos = 0
        self.policy = policy
        self.bad: list[str] = []

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> tuple[bool, list[str]]:
        ok = self._or()
        if self._peek() is not None:
            raise ToolError(f"cannot parse licence expression {' '.join(self.tokens)!r}")
        return ok, [] if ok else sorted(set(self.bad))

    def _or(self) -> bool:
        results = [self._and()]
        while self._peek() == "OR":
            self._next()
            results.append(self._and())
        return any(results)

    def _and(self) -> bool:
        results = [self._atom()]
        while self._peek() == "AND":
            self._next()
            results.append(self._atom())
        return all(results)

    def _atom(self) -> bool:
        token = self._peek()
        if token is None or token in {"AND", "OR", "WITH", ")"}:
            raise ToolError(f"cannot parse licence expression {' '.join(self.tokens)!r}")
        self._next()
        if token == "(":
            ok = self._or()
            if self._peek() != ")":
                raise ToolError(f"unbalanced parentheses in {' '.join(self.tokens)!r}")
            self._next()
            return ok
        ok = self.policy.operand(token) is not None
        if self._peek() == "WITH":
            self._next()
            exception = self._next() if self._peek() is not None else ""
            if exception not in self.policy.exceptions:
                self.bad.append(f"{token} WITH {exception}")
                return False
        if not ok:
            self.bad.append(token)
        return ok


def check_licence(policy: Policy, name: str, licence: str) -> list[str]:
    """The licence names of one package that are not allowed (empty when it passes)."""
    expression = policy.overrides.get(normalise(name), licence)
    try:
        ok, bad = _Parser(policy, expression).parse()
    except ToolError:
        return [expression]  # a licence text no parser can read is not on the allowlist either
    return [] if ok else bad


def override_findings(policy: Policy, listings: dict[str, list[dict[str, str]]]) -> list[str]:
    """An override is pinned to one version and must match an installed package."""
    installed = {normalise(r["Name"]): r["Version"] for rows in listings.values() for r in rows}
    findings = []
    for name, pinned in policy.override_versions.items():
        if name not in installed:
            findings.append(f"override {name} matches no installed package: drop it from the config")
        elif installed[name] != pinned:
            findings.append(f"override {name} is pinned to {pinned} but {installed[name]} is installed: re-check its licence")
    return findings


def check_listing(policy: Policy, role: str, listing: list[dict[str, str]]) -> list[str]:
    findings = []
    for row in listing:
        name, version, licence = row["Name"], row["Version"], row["License"]
        if normalise(name) in policy.forbidden:
            findings.append(f"{role}: {name} {version} is forbidden (AGPL PDF stack); remove it from the dependency set")
        bad = check_licence(policy, name, licence)
        if bad:
            findings.append(f"{role}: {name} {version}: licence {licence!r} is not on the allowlist (not allowed: {', '.join(bad)})")
    return findings


def packages_with_notice(listing: list[dict[str, str]]) -> set[str]:
    """Names of the packages whose distribution ships a NOTICE file (Apache-2.0 section 4(d))."""
    return {r["Name"] for r in listing if r.get("NoticeText", "").strip() not in ("", "UNKNOWN")}


def lockfile_names(lockfile: Path) -> set[str]:
    data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    return {normalise(p["name"]) for p in data.get("package", [])}


def requirement_names(requirements: list[str]) -> list[str]:
    return [m.group(0) for r in requirements if (m := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r))]


def in_notice(notice: str, word: str) -> bool:
    """Whether NOTICE names ``word`` (case and ``-_.`` insensitive, on word boundaries)."""
    pattern = r"(?<![a-z0-9])" + re.escape(normalise(word)) + r"(?![a-z0-9])"
    return re.search(pattern, normalise(notice)) is not None


def notice_findings(notice: str, tree: set[str], packages: list[str], text: list[str]) -> list[str]:
    """Names the config requires in NOTICE (and in the tree) that are missing."""
    findings = [f"NOTICE does not name {name} (a dependency of the parse set)" for name in packages if not in_notice(notice, name)]
    findings += [f"NOTICE does not mention {word!r}" for word in text if not in_notice(notice, word)]
    findings += [f"notice package {name} is not in the worker-parse tree: drop it from the config" for name in packages if normalise(name) not in tree]
    return findings


def shipped_notice_findings(notice: str, names: set[str]) -> list[str]:
    return [f"NOTICE does not name {name} (its distribution ships a NOTICE file)" for name in sorted(names) if not in_notice(notice, name)]


@dataclass
class Config:
    path: Path
    data: dict[str, Any]
    policy: Policy = field(init=False)

    def __post_init__(self) -> None:
        d = self.data
        try:
            self.policy = Policy(
                allowed=frozenset(d["allowed"]),
                exceptions=frozenset(d.get("allowed-exceptions", [])),
                aliases={k.lower(): v for k, v in d.get("aliases", {}).items()},
                overrides={normalise(k): v["licence"] for k, v in d.get("overrides", {}).items()},
                override_versions={normalise(k): v["version"] for k, v in d.get("overrides", {}).items()},
                forbidden=frozenset(normalise(n) for n in d["forbidden"]),
            )
        except KeyError as exc:
            raise ToolError(f"{self.path}: missing key {exc}") from exc

    def resolve(self, key: str) -> Path:
        return (self.path.parent / self.data[key]).resolve()


def run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    except OSError as exc:
        raise ToolError(f"cannot start {cmd[0]}: {exc}") from exc
    if done.returncode != 0:
        raise ToolError(f"{' '.join(cmd)} exited {done.returncode}: {done.stderr.strip()[-400:]}")
    return done.stdout


def sync_env(project: Path, extras: list[str], target: Path) -> Path:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | {"UV_PROJECT_ENVIRONMENT": str(target)}
    cmd = ["uv", "sync", "--frozen", "--no-dev", "--no-install-project", "--project", str(project)]
    run(cmd + [arg for extra in extras for arg in ("--extra", extra)], env)
    return target / "bin" / "python"


def list_licences(pip_licenses: str, python: Path) -> list[dict[str, str]]:
    """Name, Version, License and NoticeText of every package (``--no-license-path``: no machine paths in the output)."""
    cmd = ["uvx", "--from", pip_licenses, "pip-licenses", "--python", str(python), "--from=mixed", "--format=json"]
    cmd += ["--with-license-file", "--no-license-path", "--with-notice-file"]
    return json.loads(run(cmd))  # type: ignore[no-any-return]


def check(config: Config, existing: dict[str, Path], workdir: Path) -> list[str]:
    findings: list[str] = []
    project = config.resolve("project")
    names = lockfile_names(project / config.data["lockfile"])
    findings += [f"lockfile: {n} is forbidden and is locked" for n in sorted(names & config.policy.forbidden)]
    synced: dict[tuple[str, ...], Path] = {}
    trees: dict[str, set[str]] = {}
    shipped_notices: set[str] = set()
    listings: dict[str, list[dict[str, str]]] = {}
    for role in config.data["roles"]:
        python = existing.get(role["name"])
        if python is None:
            key = tuple(role["extras"])
            if key not in synced:
                synced[key] = sync_env(project, role["extras"], workdir / "-".join(key))
            python = synced[key]
        listing = list_licences(config.data["pip-licenses"], python)
        trees[role["name"]] = {normalise(r["Name"]) for r in listing}
        shipped_notices |= packages_with_notice(listing)
        listings[role["name"]] = listing
        findings += check_listing(config.policy, role["name"], listing)
        print(f"licence-gate: {role['name']}: {len(listing)} packages checked")
    findings += override_findings(config.policy, listings)
    notice = config.data["notice"]
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    direct = requirement_names(pyproject["project"]["optional-dependencies"][notice["extra"]])
    findings += notice_findings(
        config.resolve("notice-file").read_text(encoding="utf-8"),
        trees[notice["role"]],
        sorted({*notice["packages"], *direct}),
        notice.get("text", []),
    )
    findings += shipped_notice_findings(config.resolve("notice-file").read_text(encoding="utf-8"), shipped_notices)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env", action="append", default=[], metavar="ROLE=PYTHON", help="check this python instead of syncing the role")
    parser.add_argument("--workdir", type=Path, help="where to create the scratch environments (default: a temporary directory)")
    args = parser.parse_args(argv)
    try:
        config = Config(args.config.resolve(), tomllib.loads(args.config.read_text(encoding="utf-8")))
        existing = {role: Path(python) for role, _, python in (item.partition("=") for item in args.env)}
        unknown = set(existing) - {r["name"] for r in config.data["roles"]}
        if unknown:
            raise ToolError(f"--env names unknown roles: {', '.join(sorted(unknown))}")
        with tempfile.TemporaryDirectory(prefix="licence-gate-") as tmp:
            findings = check(config, existing, args.workdir or Path(tmp))
    except (ToolError, OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        print(f"licence-gate: error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    for finding in findings:
        print(f"licence-gate: {finding}", file=sys.stderr)
    print(f"licence-gate: {'FAILED, ' + str(len(findings)) + ' finding(s)' if findings else 'clean'}")
    return EXIT_FINDINGS if findings else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
