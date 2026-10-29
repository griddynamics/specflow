"""Self-test for `blocking_in_async`, colocated with it."""

from __future__ import annotations

from pathlib import Path

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import blocking_in_async as rule

FIXTURES = Path(__file__).parent / "fixtures" / "blocking_in_async"


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_positive_fixture_is_flagged() -> None:
    findings = rule.check_source("positive.py", FIXTURES.joinpath("positive.py").read_text())
    assert len(findings) == 1
    assert findings[0].rule == "blocking-in-async"


def test_negative_fixture_is_clean() -> None:
    findings = rule.check_source("negative.py", FIXTURES.joinpath("negative.py").read_text())
    assert findings == []


def test_configured_call_is_flagged_and_replaces_the_default(tmp_path: Path) -> None:
    source = "async def f(s3):\n    s3.fetch_object()\n    pandas.read_csv('x')\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(source)
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text('[blocking-in-async]\nextra-calls = ["fetch_object"]\n')
    files.configure(tmp_path)
    config.configure(cfg)
    assert [f.message.split("(")[0] for f in rule.check()] == ["fetch_object", "read_csv"]
    cfg.write_text('[blocking-in-async]\ncalls = ["fetch_object"]\n')
    assert [f.message.split("(")[0] for f in rule.check()] == ["fetch_object"]
