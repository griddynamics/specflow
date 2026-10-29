"""Import-only shims are rejected without confusing implementation or type definitions."""

from __future__ import annotations

from pathlib import Path

from tests.semgrep_support import real_scan


def test_import_only_shims_and_implementation_modules(tmp_path: Path) -> None:
    sources = {
        "__init__.py": "from domain.models import RegisteredModel\n",
        "single.py": "from domain.schemas import Record\n",
        "multiline.py": (
            '"""Convenience imports."""\nfrom __future__ import annotations\n'
            "from domain.schemas import (\n    Record,\n    Finding,\n)\n"
            '__all__ = ["Record", "Finding"]\n'
        ),
        "inline_implementation.py": "from domain.schemas import Record; register(Record)\n",
        "implementation.py": (
            "from domain.schemas import Record\n\ndef build():\n    return Record()\n"
        ),
        "type_definition.py": "from domain.schemas import Record\n\ntype Records = list[Record]\n",
        "metadata.py": "from domain.schemas import Record\n\nVERSION = 2\n",
        "empty.py": '"""A namespace package."""\nfrom __future__ import annotations\n',
    }
    paths: dict[str, Path] = {}
    for filename, source in sources.items():
        path = tmp_path / filename
        path.write_text(source)
        paths[filename] = path
    findings = real_scan("compat_shims.yml", *paths.values())
    actual = set(findings.get("kit.python.reexport-only-shim", {}))
    assert actual == {str(paths["single.py"]), str(paths["multiline.py"])}
