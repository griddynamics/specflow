"""The committed tool manifest of the web app's guide; ``make mcp-manifest`` runs this file to rewrite it."""

from __future__ import annotations

from pathlib import Path

from api.mcp.registry import render_manifest_json

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "guides" / "mcp-tools.json"


def main() -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(render_manifest_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
