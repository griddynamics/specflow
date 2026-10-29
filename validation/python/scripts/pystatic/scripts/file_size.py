"""No tracked or untracked-but-not-ignored file under the watched paths exceeds the size or
page ceiling. Reads `[file-size]` (`paths`, default `tests/fixtures`; `max-bytes`, default
10 MiB; `suffixes`; `page-paths`; `max-pages`). Doc: /README.md#file-size"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import target_root
from pystatic.harness import Finding, StepResult
from pystatic.harness.gitutil import glob_tracked

STEP = "file-size"
DOC = "/README.md#file-size"
DEFAULT_PATHS = ("tests/fixtures",)
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _positive(table: dict[str, object], key: str, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        msg = f"`{key}` must be a positive integer"
        raise config.ConfigError(msg)
    return value


def page_count(path: Path) -> int | None:
    """The pages of a PDF or the slides of a presentation; None for another format.

    Needs the `pages` extra (`pypdf`, `python-pptx`)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    if suffix == ".pptx":
        from pptx import Presentation

        return len(Presentation(str(path)).slides)
    return None


def _size_finding(name: str, size: int, ceiling: int) -> Finding:
    return Finding(
        name,
        1,
        1,
        "file-size-over-ceiling",
        f"{size} bytes exceeds the {ceiling}-byte ceiling",
        fix="shrink the file (an excerpt, a smaller sample) or split it",
        doc=DOC,
    )


def _page_finding(name: str, path: Path, max_pages: int) -> Finding | None:
    pages = page_count(path)
    if pages is None:
        return Finding(
            name,
            1,
            1,
            "file-size-pages-not-countable",
            "a document under `page-paths` whose pages cannot be counted",
            fix="use a PDF or PPTX, or move the file out of `page-paths`",
            doc=DOC,
        )
    if pages <= max_pages:
        return None
    return Finding(
        name,
        1,
        1,
        "file-size-over-page-budget",
        f"{pages} pages exceeds the {max_pages}-page budget",
        fix="cut the document to an excerpt within the page budget",
        doc=DOC,
    )


def check() -> list[Finding]:
    table = config.section(STEP)
    ceiling = _positive(table, "max-bytes", DEFAULT_MAX_BYTES)
    max_pages = _positive(table, "max-pages", 2)
    root = target_root()
    suffixes = {s.lower() for s in config.string_list(table, "suffixes", [])}
    page_specs = config.string_list(table, "page-paths", [])
    specs = config.string_list(table, "paths", list(DEFAULT_PATHS))
    names = sorted({name for spec in specs for name in glob_tracked(spec, cwd=root)})
    page_names = {name for spec in page_specs for name in glob_tracked(spec, cwd=root)}
    found: list[Finding] = []
    for name in sorted(set(names) | page_names):
        path = root / name
        if not path.is_file():
            continue
        if suffixes and PurePosixPath(name).suffix.lower() not in suffixes:
            continue
        size = path.stat().st_size
        if name in names and size > ceiling:
            found.append(_size_finding(name, size, ceiling))
        if name in page_names and (page := _page_finding(name, path, max_pages)):
            found.append(page)
    return found


def run() -> StepResult:
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
