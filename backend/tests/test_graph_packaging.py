"""Package data of the graph library (plan 5.T5; tech-specs 10 "Dependencies"): the shipped ontology
``graph/core/ontology/spec-ontology.yaml`` and every judge prompt it references,
``graph/core/ontology/judge_prompts/<prompt>.md``.

1. The installed package: the files are resolved through ``importlib.resources`` from the ``graph``
   package the interpreter imports, which must be the one the installed distribution
   (``specflow``) provides. In this repo that is uv's editable install, whose
   ``.pth`` points at ``backend/src``, so here the installed files are the source tree's; in a
   regular install they are the copies in ``site-packages``. ``load_ontology()`` loads exactly them.
2. The wheel: ``uv build --offline`` (it builds from uv's cache, no network; well under a second)
   builds the wheel into a temp dir; it must hold the same files byte for byte, and the package
   unpacked from it, imported ahead of the editable install, must load the ontology from itself.
   Skipped only when the ``uv`` CLI is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from graph.core import load_ontology

DISTRIBUTION = "specflow"
PACKAGE = "graph"
ONTOLOGY_DIR = ("core", "ontology")
ONTOLOGY_FILE = "spec-ontology.yaml"
JUDGE_PROMPT_DIR = "judge_prompts"
BACKEND = Path(__file__).resolve().parents[1]


def ontology_dir():
    directory = files(PACKAGE)
    for part in ONTOLOGY_DIR:
        directory = directory / part
    return directory


def referenced_prompts() -> list[str]:
    """The judge prompt names the installed ontology file references."""

    data = yaml.safe_load((ontology_dir() / ONTOLOGY_FILE).read_text(encoding="utf-8"))
    return sorted({rule["prompt"] for rule in data["inference"]["rules"]})


def package_data() -> dict[str, bytes]:
    """Every package data file of the installed package, by its path inside it (``graph/...``)."""

    directory = ontology_dir()
    base = "/".join((PACKAGE, *ONTOLOGY_DIR))
    found = {f"{base}/{ONTOLOGY_FILE}": (directory / ONTOLOGY_FILE).read_bytes()}
    for name in referenced_prompts():
        found[f"{base}/{JUDGE_PROMPT_DIR}/{name}.md"] = (directory / JUDGE_PROMPT_DIR / f"{name}.md").read_bytes()
    return found


def installed_roots() -> list[Path]:
    """Where the installed distribution puts its packages: the paths of an editable install's
    ``.pth`` files, else the distribution's own location (``site-packages``)."""

    dist = distribution(DISTRIBUTION)
    roots = []
    for item in dist.files or ():
        if item.suffix == ".pth":
            text = Path(dist.locate_file(item)).read_text(encoding="utf-8")
            roots += [
                Path(line.strip()) for line in text.splitlines() if line.strip() and not line.startswith("import ")
            ]
    return roots or [Path(dist.locate_file(""))]


def test_the_imported_package_is_the_installed_distribution() -> None:
    package = Path(str(files(PACKAGE))).resolve()
    roots = [root.resolve() for root in installed_roots()]
    assert any(package == root / PACKAGE for root in roots), (package, roots)
    direct_url = distribution(DISTRIBUTION).read_text("direct_url.json")
    editable = bool(direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"))
    if editable:  # uv's editable install of this repo: the installed package is backend/src/graph
        assert package == (BACKEND / "src" / PACKAGE).resolve()


def test_the_ontology_and_its_judge_prompts_are_package_resources() -> None:
    directory = ontology_dir()
    assert (directory / ONTOLOGY_FILE).is_file()
    prompts = referenced_prompts()
    assert prompts, "the shipped ontology references no judge prompt"
    for name in prompts:
        prompt = directory / JUDGE_PROMPT_DIR / f"{name}.md"
        assert prompt.is_file(), name
        assert prompt.read_text(encoding="utf-8").strip(), name
    # Every shipped prompt file is referenced (no stale package data).
    shipped = sorted(
        item.name.removesuffix(".md")
        for item in (directory / JUDGE_PROMPT_DIR).iterdir()
        if item.is_file() and item.name.endswith(".md")
    )
    assert shipped == prompts


def test_load_ontology_reads_the_package_resources() -> None:
    ontology = load_ontology(submodule_files=[])
    data = yaml.safe_load((ontology_dir() / ONTOLOGY_FILE).read_text(encoding="utf-8"))
    assert (ontology.name, ontology.version) == (data["name"], str(data["version"]))
    assert sorted(ontology.judge_prompts) == referenced_prompts()
    for name, text in ontology.judge_prompts.items():
        assert text == (ontology_dir() / JUDGE_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


LOAD_FROM_UNPACKED = """
import json
from importlib.resources import files
import graph
from graph.core import load_ontology
ontology = load_ontology(submodule_files=[])
print(json.dumps({"package": graph.__file__, "resources": str(files("graph")),
                  "name": ontology.name, "prompts": sorted(ontology.judge_prompts)}))
"""


requires_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the uv CLI is needed to build the wheel")


@requires_uv
def test_the_built_wheel_holds_the_package_data_and_loads_it(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    uv = str(shutil.which("uv"))
    subprocess.run(
        [uv, "build", "--offline", "--wheel", "--out-dir", str(out), "--quiet"],
        cwd=BACKEND,
        check=True,
        timeout=120,
    )
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        expected = package_data()
        assert sorted(set(expected) - names) == []
        for name, content in expected.items():
            assert wheel.read(name) == content, name
        assert {"graph/__init__.py", "graph/core/ontology/loader.py"} <= names
        wheel.extractall(unpacked)
    # The unpacked wheel first on the path: graph and its data come from it, not the editable install
    # (the dependencies still come from this environment).
    done = subprocess.run(
        [sys.executable, "-c", LOAD_FROM_UNPACKED],
        cwd=tmp_path,
        env={"PYTHONPATH": str(unpacked), "PATH": "", "LOGS_STDOUT_LEVEL": "ERROR"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert Path(loaded["package"]).resolve().is_relative_to(unpacked.resolve()), loaded
    assert Path(loaded["resources"]).resolve() == (unpacked / PACKAGE).resolve()
    assert loaded["prompts"] == referenced_prompts() and loaded["name"] == load_ontology(submodule_files=[]).name
