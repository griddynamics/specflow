"""Self-test for `files` (scan target) and `cli` (the shared standalone command line)."""

from __future__ import annotations

from pathlib import Path

from pystatic import cli, files


def _tree(root: Path) -> None:
    for rel in (
        "src/a.py",
        "src/pkg/b.py",
        "src/fixtures/bad.py",
        "src/__pycache__/c.py",
        "tests/test_a.py",
        "scripts/tool.py",
        "src/q.sql",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def test_default_walk_covers_src_and_tests_and_skips_fixtures(tmp_path: Path) -> None:
    _tree(tmp_path)
    files.configure(tmp_path)
    found = [files.repo_rel(p) for p in files.iter_py_files()]
    assert found == ["src/a.py", "src/pkg/b.py", "tests/test_a.py"]


def test_scan_option_narrows_the_walk(tmp_path: Path) -> None:
    _tree(tmp_path)
    cli.parse_target(["--root", str(tmp_path), "--scan", "scripts"])
    assert [files.repo_rel(p) for p in files.iter_py_files()] == ["scripts/tool.py"]
    assert files.scan_roots(("src",)) == ("scripts",)
    assert files.scan_dirs(("src",)) == [tmp_path.resolve() / "scripts"]


def test_defaults_apply_without_a_scan_option(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert files.scan_roots(("src/schemas",)) == ("src/schemas",)


def test_iter_files_by_suffix(tmp_path: Path) -> None:
    _tree(tmp_path)
    files.configure(tmp_path)
    assert [files.repo_rel(p) for p in files.iter_files(".sql", ("src",))] == ["src/q.sql"]


def test_repo_rel_walks_up_out_of_the_root(tmp_path: Path) -> None:
    files.configure(tmp_path / "root")
    assert files.repo_rel(tmp_path / "other" / "x.py") == "../other/x.py"
    assert files.repo_rel(Path("src/x.py")) == "src/x.py"


def test_paths_are_collected_for_checks_that_take_them(tmp_path: Path) -> None:
    args = cli.parse_target(["--root", str(tmp_path), "a.sql", "b.sql"])
    assert args.paths == ["a.sql", "b.sql"]
    assert files.target_root() == tmp_path.resolve()
