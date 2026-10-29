"""`python -m pystatic.docs.indexes [--root DIR] [--config TOML] [--check]`: writes the root
and per-folder `index.md` files from frontmatter; `--check` exits 1 when one is stale.
Doc: /README.md#docs-bundle"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TypeAliasType

from pystatic.cli import apply_target
from pystatic.docs import settings
from pystatic.docs.frontmatter import (
    FrontmatterError,
    frontmatter_fields_lenient,
    frontmatter_from_text,
)
from pystatic.files import target_root

DocumentEntries = TypeAliasType("DocumentEntries", list[tuple[str, dict[str, str]]])


def _frontmatter_fields(rel: str, text: str, findings: list[str]) -> dict[str, str]:
    try:
        strict = frontmatter_from_text(text)
    except FrontmatterError as exc:
        findings.append(f"{rel}: frontmatter is not valid YAML: {exc}")
        strict = None
    if strict is None and (strict := frontmatter_fields_lenient(text)) is None:
        findings.append(f"{rel}: missing frontmatter")
    fields = {k: str(v) for k, v in (strict or {}).items()}
    findings.extend(
        f"{rel}: frontmatter lacks {k}" for k in ("title", "description") if not fields.get(k)
    )
    return fields


def docs_in(
    bundle: settings.Bundle,
    folder: settings.Folder,
    docs_dir: Path,
    texts: dict[str, str],
    findings: list[str],
) -> DocumentEntries:
    full = docs_dir / folder.path
    if not full.is_dir():
        return []
    names = sorted(
        p.stem for p in full.iterdir() if p.suffix == ".md" and p.name != bundle.index_name
    )
    order = list(folder.order)
    names.sort(key=lambda n: (order.index(n) if n in order else len(order), n))

    def _fields(name: str) -> dict[str, str]:
        rel = f"{folder.path}/{name}.md"
        text = texts.get(rel) or (full / f"{name}.md").read_text(encoding="utf-8")
        return _frontmatter_fields(rel, text, findings)

    return [(n, _fields(n)) for n in names]


def line(title: str, href: str, desc: str) -> str:
    return f"* [{title}]({href}) - {desc}"


def root_index(bundle: settings.Bundle, documents: dict[str, DocumentEntries]) -> str:
    out = [*bundle.root_preamble]
    if out:
        out.append("")
    out += [f"# {bundle.root_title}", ""]
    for folder in bundle.folders:
        out += ["", f"# {folder.title}", ""]
        if folder.summary:
            out.append(line(folder.title, f"{folder.path}/{bundle.index_name}", folder.summary))
            continue
        out += [
            line(fm.get("title") or n, f"{folder.path}/{n}.md", fm.get("description") or "")
            for n, fm in documents[folder.path]
        ]
    return "\n".join(out) + "\n"


def folder_index(
    bundle: settings.Bundle, folder: settings.Folder, documents: DocumentEntries
) -> str:
    out = [f"# {folder.title}", ""]
    out += [
        line(fm.get("title") or n, f"{n}.md", fm.get("description") or "") for n, fm in documents
    ]
    subs = [f for f in bundle.folders if PurePosixPath(f.path).parent.as_posix() == folder.path]
    if subs:
        out.append("")
        for sub in subs:
            href = f"{PurePosixPath(sub.path).name}/{bundle.index_name}"
            out.append(line(sub.title, href, f"Index of {sub.title}"))
    return "\n".join(out) + "\n"


def generated(
    bundle: settings.Bundle, docs_dir: Path, texts: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """`({index path relative to the bundle: content}, findings)`; `texts` holds documents a
    caller has already read, so they are not opened twice."""
    texts = texts or {}
    findings: list[str] = []
    documents = {f.path: docs_in(bundle, f, docs_dir, texts, findings) for f in bundle.folders}
    files = {bundle.index_name: root_index(bundle, documents)}
    for folder in bundle.folders:
        files[f"{folder.path}/{bundle.index_name}"] = folder_index(
            bundle, folder, documents[folder.path]
        )
    return files, sorted(set(findings))


def write_or_check(bundle: settings.Bundle, docs_dir: Path, *, check: bool) -> int:
    files, findings = generated(bundle, docs_dir)
    stale: list[str] = []
    for rel, content in files.items():
        path = docs_dir / rel
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        stale.append(rel)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    for message in findings:
        sys.stdout.write(f"{message}\n")
    if check:
        for rel in stale:
            sys.stdout.write(f"{rel}: stale index; run python -m pystatic.docs.indexes\n")
        sys.stdout.write(f"{len(files)} indexes, {len(stale)} stale\n")
        return 1 if (stale or findings) else 0
    sys.stdout.write(f"{len(files)} indexes written ({len(stale)} changed)\n")
    return 1 if findings else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate the bundle's index.md files")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--config", type=Path, default=None, help="pystatic.toml")
    parser.add_argument("--check", action="store_true", help="exit 1 if an index is stale")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    args.scan = None
    apply_target(args)
    bundle = settings.load()
    return write_or_check(bundle, bundle.docs_dir(target_root()), check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
