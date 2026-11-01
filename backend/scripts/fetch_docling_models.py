"""Download the Docling models the parser tests need, at the revisions the worker-parse image bakes in.

    uv run python scripts/fetch_docling_models.py [--target DIR]

The default target is ``backend/.docling-models`` (git-ignored), the folder ``tests/conftest.py`` points
``DOCLING_ARTIFACTS_PATH`` at when it exists. CI passes its own target. A repository whose marker file
(``.fetched``, holding the revision and the file patterns) is already there is skipped, so a second run does
no network call. The revisions here are the single source: ``tests/test_docling_models_ci.py`` keeps the
``docker/worker-parse/Dockerfile`` ARGs equal to them, and the CI cache key hashes this file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final, NamedTuple

from huggingface_hub import snapshot_download

DEFAULT_TARGET: Final = Path(__file__).resolve().parent.parent / ".docling-models"
MARKER: Final = ".fetched"


class ModelRepo(NamedTuple):
    repo_id: str
    revision: str
    patterns: tuple[str, ...]

    @property
    def folder(self) -> str:
        """The folder name Docling looks for under its artifacts path (``/`` becomes ``--``)."""
        return self.repo_id.replace("/", "--")

    @property
    def stamp(self) -> str:
        return "\n".join((self.revision, *self.patterns)) + "\n"


# Only TableFormer v1 FAST is used (D4); V2 and ACCURATE are not fetched.
LAYOUT: Final = ModelRepo(
    "docling-project/docling-layout-heron",
    "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
    ("config.json", "preprocessor_config.json", "model.safetensors"),
)
TABLEFORMER: Final = ModelRepo(
    "docling-project/docling-models",
    "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
    ("model_artifacts/tableformer/fast/*",),
)
MODELS: Final = (LAYOUT, TABLEFORMER)


def fetch(target: Path, models: tuple[ModelRepo, ...] = MODELS) -> list[str]:
    """Download what is missing under ``target``; returns the repo ids it fetched."""

    fetched = []
    for model in models:
        folder = target / model.folder
        marker = folder / MARKER
        if marker.is_file() and marker.read_text(encoding="utf-8") == model.stamp:
            continue
        snapshot_download(
            repo_id=model.repo_id, revision=model.revision, allow_patterns=list(model.patterns), local_dir=folder
        )
        folder.mkdir(parents=True, exist_ok=True)
        marker.write_text(model.stamp, encoding="utf-8")
        fetched.append(model.repo_id)
    return fetched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--target", type=Path, default=DEFAULT_TARGET, help=f"models folder (default: {DEFAULT_TARGET})"
    )
    args = parser.parse_args(argv)
    fetched = fetch(args.target)
    sys.stdout.write(
        f"docling models in {args.target}: {', '.join(fetched) + ' fetched' if fetched else 'already present'}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
