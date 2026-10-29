"""`python -m pystatic.docs.check [--root DIR] [--config TOML]`: checks frontmatter, folder
types, numbered records, headings, identifiers, ephemeral markers, internal links and
generated indexes of the bundle. Doc: /README.md#docs-bundle"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pystatic.cli import main as cli_main
from pystatic.docs import indexes, settings
from pystatic.docs.frontmatter import (
    FrontmatterError,
    blank_frontmatter,
    frontmatter_fields_lenient,
    frontmatter_from_text,
)
from pystatic.docs.markdown import (
    Link,
    Table,
    find_fences,
    find_links,
    find_tables,
    heading_anchor,
    is_heading_numbered,
    links_under_heading,
    parse_headings,
)
from pystatic.files import target_root
from pystatic.harness import Finding, StepResult

STEP = "docs-bundle"
DOC = "/README.md#docs-bundle"
_FIX_HINTS = {
    "docs-frontmatter": "correct the frontmatter block",
    "docs-numbered-record": "match the record's file name and its opening heading",
    "docs-heading": "rename the heading (unnumbered, unique, plain ASCII)",
    "docs-duplicate-id": "keep every identifier unique across tables",
    "docs-id-placement": "move the row to a document that may hold it",
    "docs-ephemeral": "remove the work-in-progress marker",
    "docs-link": "fix the link target or anchor",
    "docs-index-stale": "run python -m pystatic.docs.indexes",
}
_READING_ORDER_LEVEL = 2


@dataclass
class _Bundle:
    """The bundle's files, read once."""

    texts: dict[str, str]
    lines: dict[str, list[str]]
    findings: list[Finding] = field(default_factory=list)

    def add(self, rel: str, line: int, rule: str, message: str) -> None:
        fix = _FIX_HINTS.get(rule, "fix the finding")
        self.findings.append(Finding(rel, line, 1, rule, message, fix=fix, doc=DOC))


def _read(docs_dir: Path) -> _Bundle:
    texts: dict[str, str] = {}
    lines: dict[str, list[str]] = {}
    for path in sorted(docs_dir.rglob("*.md")):
        rel = path.relative_to(docs_dir).as_posix()
        text = texts[rel] = path.read_text(encoding="utf-8")
        lines[rel] = blank_frontmatter(text).split("\n")
    return _Bundle(texts=texts, lines=lines)


def _folder(rel: str) -> str:
    parent = PurePosixPath(rel).parent
    return "" if parent == PurePosixPath() else parent.as_posix()


def _internal_target(href: str) -> tuple[str | None, str | None] | None:
    """`(absolute /path.md or None, #anchor or None)`; `None` for an external or odd link."""
    u = urlsplit(href)
    if (
        u.scheme
        or u.netloc
        or u.query
        or (u.path and not (u.path.startswith("/") and u.path.endswith(".md")))
    ):
        return None
    path, anchor = u.path or None, (f"#{u.fragment}" if u.fragment else None)
    return None if not path and not anchor else (path, anchor)


def _tag_documents(cfg: settings.Bundle, bundle: _Bundle) -> tuple[set[str], set[str]]:
    """(valid tags, files linked from a tag document's reading order)."""
    if not cfg.tag_folder:
        return set(), set()
    tag_docs = [
        rel
        for rel in bundle.texts
        if _folder(rel) == cfg.tag_folder and PurePosixPath(rel).name != cfg.index_name
    ]
    linked: set[str] = set()
    for rel in tag_docs if cfg.reading_order_heading else []:
        lines = bundle.lines[rel]
        head = next(
            (
                h
                for h in parse_headings(lines)
                if h.level == _READING_ORDER_LEVEL
                and h.title.lower() == cfg.reading_order_heading.lower()
            ),
            None,
        )
        for link in links_under_heading(lines, head) if head else []:
            target = link.target.split("#", 1)[0]
            if target.startswith("/") and target.endswith(".md"):
                linked.add(target.lstrip("/"))
    return {PurePosixPath(rel).stem for rel in tag_docs} | set(cfg.extra_tags), linked


def _check_tags(
    cfg: settings.Bundle,
    bundle: _Bundle,
    rel: str,
    raw_tags: object,
    tags: tuple[set[str], set[str]],
) -> None:
    valid, linked = tags
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        bundle.add(rel, 1, "docs-frontmatter", "tags must be a list of strings")
        return
    for tag in raw_tags:
        if tag not in valid:
            bundle.add(rel, 1, "docs-frontmatter", f"unknown tag {tag!r}")
    needs_tags = rel in linked or _folder(rel) == cfg.tag_folder
    if needs_tags and not raw_tags:
        bundle.add(rel, 1, "docs-frontmatter", "tags must not be empty for a tagged document")


def _check_frontmatter(
    cfg: settings.Bundle, bundle: _Bundle, rel: str, tags: tuple[set[str], set[str]]
) -> None:
    text = bundle.texts[rel]
    if PurePosixPath(rel).name == cfg.index_name:
        if rel != cfg.index_name and text.startswith("---"):
            bundle.add(rel, 1, "docs-frontmatter", f"{cfg.index_name} must not carry frontmatter")
        return
    fields = frontmatter_fields_lenient(text)
    if fields is None:
        bundle.add(rel, 1, "docs-frontmatter", "missing frontmatter")
        return
    parsed = None
    try:
        parsed = frontmatter_from_text(text)
    except FrontmatterError as exc:
        bundle.add(rel, 1, "docs-frontmatter", f"frontmatter is not valid YAML: {exc}")
    for key in cfg.fields:
        if key not in fields:
            bundle.add(rel, 1, "docs-frontmatter", f"frontmatter missing {key}")
    for key in sorted(set(fields) - set(cfg.fields) - cfg.optional_fields):
        bundle.add(rel, 1, "docs-frontmatter", f"frontmatter extra field {key}")
    doc_type = fields.get("type")
    if "type" in cfg.fields and cfg.types and doc_type not in cfg.types:
        bundle.add(rel, 1, "docs-frontmatter", f"unknown type {doc_type}")
    allowed = cfg.folder_types().get(_folder(rel))
    if allowed and doc_type not in allowed:
        bundle.add(rel, 1, "docs-frontmatter", f"type {doc_type} not allowed in {_folder(rel)}")
    if "status" in cfg.fields and cfg.statuses and fields.get("status") not in cfg.statuses:
        bundle.add(rel, 1, "docs-frontmatter", f"bad status {fields.get('status')}")
    if "tags" in cfg.fields and cfg.tag_folder:
        raw_tags = parsed.get("tags") if isinstance(parsed, dict) else None
        _check_tags(cfg, bundle, rel, raw_tags, tags)


def _check_numbered_record(cfg: settings.Bundle, bundle: _Bundle, rel: str) -> None:
    name = PurePosixPath(rel).name
    for record in cfg.numbered_records:
        if _folder(rel) != record.folder or name == cfg.index_name:
            continue
        if not record.filename.match(name):
            bundle.add(rel, 1, "docs-numbered-record", f"file name {name} breaks the convention")
        h1 = next((h for h in parse_headings(bundle.lines[rel]) if h.level == 1), None)
        match = record.heading.match(h1.title) if h1 else None
        if match is None:
            bundle.add(rel, 1, "docs-numbered-record", "the opening heading breaks the convention")
        elif heading_anchor(" ".join(match.groups())) != PurePosixPath(rel).stem:
            message = "the opening heading does not match the file name"
            bundle.add(rel, 1, "docs-numbered-record", message)


def _check_headings(cfg: settings.Bundle, bundle: _Bundle, rel: str) -> set[str]:
    slugs: collections.Counter[str] = collections.Counter()
    for h in parse_headings(bundle.lines[rel]):
        if cfg.forbid_numbered_headings and is_heading_numbered(h.title):
            bundle.add(rel, h.line, "docs-heading", f"numbered heading: {h.title}")
        if cfg.ascii_headings and not h.title.isascii():
            bundle.add(rel, h.line, "docs-heading", f"non-ASCII heading: {h.title}")
        for char in cfg.forbidden_heading_chars:
            if char in h.title:
                bundle.add(rel, h.line, "docs-heading", f"{char!r} in heading: {h.title}")
        slugs[heading_anchor(h.title)] += 1
    for slug, count in slugs.items():
        if count > 1:
            bundle.add(rel, 1, "docs-heading", f"duplicate heading slug #{slug} x{count}")
    return set(slugs)


def _check_ids(
    cfg: settings.Bundle,
    bundle: _Bundle,
    rel: str,
    tables: list[Table],
    declared: dict[str, tuple[str, int]],
) -> None:
    rows = [
        (row[0].strip(), ln)
        for table in tables
        for row, ln in zip(table.rows, table.row_lines, strict=True)
        if row
    ]
    if cfg.id_pattern is not None:
        for cell, ln in rows:
            m = cfg.id_pattern.match(cell)
            if not m:
                continue
            ident = m.group(1) if m.groups() else m.group(0)
            if ident in declared:
                prev_rel, prev_ln = declared[ident]
                where = f"{prev_rel}:{prev_ln}"
                message = f"duplicate identifier {ident} (previously declared at {where})"
                bundle.add(rel, ln, "docs-duplicate-id", message)
            else:
                declared[ident] = (rel, ln)
    folder = _folder(rel)
    for placement in cfg.id_placement:
        in_forbidden = any(
            folder == f or folder.startswith(f"{f}/") for f in placement.forbidden_folders
        )
        if not in_forbidden or rel in placement.allowed_files:
            continue
        hit = next((ln for cell, ln in rows if placement.pattern.match(cell)), None)
        if hit is not None:
            message = f"a row matching {placement.pattern.pattern!r} is not allowed in {folder}"
            bundle.add(rel, hit, "docs-id-placement", message)


def _check_ephemeral(cfg: settings.Bundle, bundle: _Bundle, rel: str) -> None:
    lines = bundle.lines[rel]
    fences = [(f.start, f.end) for f in find_fences(lines)]
    text = "\n".join(
        "" if any(s <= i <= e for s, e in fences) else line for i, line in enumerate(lines, start=1)
    )
    for exempt in cfg.ephemeral_exempt:
        if exempt.file == rel:
            text = exempt.after.split(text, maxsplit=1)[0]
    for name, pattern in cfg.ephemeral:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            snippet = text[max(0, match.start() - 30) : match.end() + 30]
            bundle.add(rel, line, "docs-ephemeral", f"ephemeral ({name}): {snippet!r}")


def _check_links(
    bundle: _Bundle, links: dict[str, list[Link]], headings: dict[str, set[str]]
) -> None:
    for rel, found in links.items():
        for link in found:
            parsed = _internal_target(link.target)
            if parsed is None:
                continue
            link_path, anchor = parsed
            target = link_path.lstrip("/") if link_path else rel
            if link_path and target not in bundle.texts:
                bundle.add(rel, link.line, "docs-link", f"broken file link {link_path}")
            elif anchor and anchor[1:].lower() not in headings.get(target, set()):
                bundle.add(rel, link.line, "docs-link", f"broken anchor {link_path or ''}{anchor}")


def check_bundle(cfg: settings.Bundle, docs_dir: Path) -> list[Finding]:
    bundle = _read(docs_dir)
    tags = _tag_documents(cfg, bundle)
    declared: dict[str, tuple[str, int]] = {}
    headings: dict[str, set[str]] = {}
    links: dict[str, list[Link]] = {}
    for rel in bundle.texts:
        _check_frontmatter(cfg, bundle, rel, tags)
        _check_numbered_record(cfg, bundle, rel)
        headings[rel] = _check_headings(cfg, bundle, rel)
        links[rel] = find_links(bundle.lines[rel])
        _check_ids(cfg, bundle, rel, find_tables(bundle.lines[rel]), declared)
        _check_ephemeral(cfg, bundle, rel)
    _check_links(bundle, links, headings)
    if cfg.folders:
        generated, _ = indexes.generated(cfg, docs_dir, texts=bundle.texts)
        for rel, content in generated.items():
            if bundle.texts.get(rel) != content:
                bundle.add(rel, 1, "docs-index-stale", "stale index")
    return sorted(bundle.findings, key=lambda f: (f.path, f.line, f.rule, f.message))


def run() -> StepResult:
    cfg = settings.load()
    docs_dir = cfg.docs_dir(target_root())
    if not docs_dir.is_dir():
        return StepResult.ok(STEP, detail=f"skipped: no documentation bundle at {cfg.root}/")
    return StepResult.from_findings(STEP, check_bundle(cfg, docs_dir))


def main() -> int:
    return cli_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
