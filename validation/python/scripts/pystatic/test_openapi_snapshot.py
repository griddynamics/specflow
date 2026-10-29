"""Self-test for `openapi_snapshot` against a stand-in app module written under `tmp_path`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pystatic import openapi_snapshot as snapshot
from pystatic.harness.docref import doc_anchor_exists

APP_MODULE = """\
import os


class App:
    def openapi(self):
        return {"openapi": "3.1.0", "info": {"title": "Orders", "version": "1"},
                "x-secret-set": bool(os.environ.get("ORDERS_SIGNING_KEY"))}


app = App()


def create_app():
    return App()
"""


@pytest.fixture(autouse=True)
def _app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "orders_app.py").write_text(APP_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("ORDERS_SIGNING_KEY", raising=False)


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(snapshot.DOC) is True


def test_render_is_deterministic() -> None:
    assert snapshot.render({"b": 1, "a": {"d": 2, "c": 3}}) == (
        '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n'
    )


@pytest.mark.parametrize("spec", ["orders_app:app", "orders_app:create_app"])
def test_write_then_check_round_trips(tmp_path: Path, spec: str) -> None:
    out = tmp_path / "contracts" / "openapi.json"
    args = ["--app", spec, "--out", str(out), "--secret-env", "ORDERS_SIGNING_KEY"]
    assert snapshot.main(args) == 0
    assert json.loads(out.read_text())["x-secret-set"] is True
    assert snapshot.main([*args, "--check"]) == 0


def test_check_reports_a_stale_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "openapi.json"
    out.write_text("{}\n")
    assert snapshot.main(["--app", "orders_app:app", "--out", str(out), "--check"]) == 1
    assert "openapi-snapshot-stale" in capsys.readouterr().out
    assert out.read_text() == "{}\n"


def test_a_malformed_app_spec_is_refused() -> None:
    with pytest.raises(ValueError, match="MODULE:ATTRIBUTE"):
        snapshot.load_app("orders_app")
