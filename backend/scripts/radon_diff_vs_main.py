#!/usr/bin/env python3
"""Compare radon metrics: current `backend/app/` vs a git ref's `backend/app/`.

Supports cyclomatic complexity (cc), maintainability index (mi), and Halstead (hal).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Metric = Literal["cc", "mi", "hal"]

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


@dataclass(frozen=True)
class BlockInfo:
    name: str
    type: str
    classname: str | None
    complexity: int

    @property
    def qual_name(self) -> str:
        if self.type == "class" or not self.classname:
            return self.name
        return f"{self.classname}.{self.name}"


@dataclass(frozen=True)
class DiffRow:
    path: str
    lineno: int
    symbol: str
    main_val: str
    head_val: str
    delta: str


def _resolve_ref(ref: str | None) -> str:
    if ref:
        return ref
    for candidate in ("main", "origin/main"):
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", candidate],
            capture_output=True,
        )
        if r.returncode == 0:
            return candidate
    sys.exit("error: neither 'main' nor 'origin/main' exists locally")


def _extract_app(ref: str, dest: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", ref, "backend/app"],
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(proc.stderr.decode())
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r|") as tar:
        tar.extractall(dest, filter="data")
    app = dest / "backend" / "app"
    if not app.is_dir():
        sys.exit(f"error: extracted tree missing {app}")
    return app


def _normalize_key(key: str) -> str:
    nk = key.replace("\\", "/")
    if "/backend/app/" in nk:
        return "app/" + nk.split("/backend/app/", 1)[1]
    p = Path(nk)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(BACKEND_DIR.resolve())).replace("\\", "/")
        except ValueError:
            pass
    return nk


def _run_radon(metric: Metric, app_path: Path | str, extra: list[str]) -> str:
    return subprocess.check_output(
        ["uv", "run", "radon", metric, str(app_path), *extra],
        cwd=str(BACKEND_DIR),
        text=True,
    )


def _parse_cc_average(output: str) -> float:
    m = re.search(r"Average complexity:\s*\w+\s*\(([\d.]+)\)", output)
    if not m:
        raise ValueError("could not parse radon cc average line")
    return float(m.group(1))


def _avg_mi_files(data: dict[str, Any]) -> float:
    vals: list[float] = []
    for v in data.values():
        if isinstance(v, dict) and "mi" in v:
            vals.append(float(v["mi"]))
    if not vals:
        raise ValueError("no MI values in radon mi JSON")
    return sum(vals) / len(vals)


def _avg_hal_file_volume(data: dict[str, Any]) -> float:
    """Mean of each file's Halstead `total.volume` (whole-file aggregate)."""
    vals: list[float] = []
    for v in data.values():
        if isinstance(v, dict) and "total" in v and isinstance(v["total"], dict):
            vals.append(float(v["total"]["volume"]))
    if not vals:
        raise ValueError("no Halstead totals in radon hal JSON")
    return sum(vals) / len(vals)


def _normalize_json_keys(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        out[_normalize_key(k)] = v
    return out


def _collect_cc_blocks(path: str, item: dict[str, Any], acc: dict[tuple[str, int], BlockInfo]) -> None:
    t = item.get("type", "")
    if t not in ("function", "method", "class"):
        return
    acc[(_normalize_key(path), item["lineno"])] = BlockInfo(
        name=item["name"],
        type=t,
        classname=item.get("classname"),
        complexity=item["complexity"],
    )
    if t == "class":
        for method in item.get("methods") or []:
            _collect_cc_blocks(path, method, acc)


def _parse_cc_blocks(data: dict[str, Any]) -> dict[tuple[str, int], BlockInfo]:
    acc: dict[tuple[str, int], BlockInfo] = {}
    for path, items in data.items():
        for item in items:
            _collect_cc_blocks(path, item, acc)
    return acc


def _diff_cc(main_blocks: dict[tuple[str, int], BlockInfo], head_blocks: dict[tuple[str, int], BlockInfo]) -> list[DiffRow]:
    rows: list[DiffRow] = []
    for key in sorted(set(main_blocks) | set(head_blocks)):
        path, lineno = key
        m, h = main_blocks.get(key), head_blocks.get(key)
        if m and h and m.complexity != h.complexity:
            rows.append(
                DiffRow(
                    path,
                    lineno,
                    h.qual_name,
                    str(m.complexity),
                    str(h.complexity),
                    f"{h.complexity - m.complexity:+d}",
                )
            )
        elif m and not h:
            rows.append(DiffRow(path, lineno, m.qual_name, str(m.complexity), "—", "removed"))
        elif h and not m:
            rows.append(DiffRow(path, lineno, h.qual_name, "—", str(h.complexity), "added"))
    return rows


def _diff_mi(main: dict[str, Any], head: dict[str, Any]) -> list[DiffRow]:
    rows: list[DiffRow] = []
    paths = set(main) | set(head)
    for path in sorted(paths):
        m_v = main.get(path)
        h_v = head.get(path)
        m_mi = float(m_v["mi"]) if isinstance(m_v, dict) and "mi" in m_v else None
        h_mi = float(h_v["mi"]) if isinstance(h_v, dict) and "mi" in h_v else None
        if m_mi is not None and h_mi is not None and m_mi != h_mi:
            rows.append(
                DiffRow(path, 0, "(file)", f"{m_mi:.2f}", f"{h_mi:.2f}", f"{h_mi - m_mi:+.2f}")
            )
        elif m_mi is not None and h_mi is None:
            rows.append(DiffRow(path, 0, "(file)", f"{m_mi:.2f}", "—", "removed"))
        elif h_mi is not None and m_mi is None:
            rows.append(DiffRow(path, 0, "(file)", "—", f"{h_mi:.2f}", "added"))
    return rows


def _diff_hal(main: dict[str, Any], head: dict[str, Any]) -> list[DiffRow]:
    """Compare file `total.volume` and per-function `volume` within each file."""
    rows: list[DiffRow] = []
    paths = set(main) | set(head)

    def file_entry(d: dict[str, Any], p: str) -> dict[str, Any] | None:
        v = d.get(p)
        return v if isinstance(v, dict) else None

    for path in sorted(paths):
        mf, hf = file_entry(main, path), file_entry(head, path)
        m_tot = mf.get("total") if mf else None
        h_tot = hf.get("total") if hf else None
        m_vol = float(m_tot["volume"]) if isinstance(m_tot, dict) and "volume" in m_tot else None
        h_vol = float(h_tot["volume"]) if isinstance(h_tot, dict) and "volume" in h_tot else None
        if m_vol is not None and h_vol is not None and m_vol != h_vol:
            rows.append(
                DiffRow(
                    path,
                    0,
                    "<file total>",
                    f"{m_vol:.2f}",
                    f"{h_vol:.2f}",
                    f"{h_vol - m_vol:+.2f}",
                )
            )
        elif m_vol is not None and h_vol is None:
            rows.append(DiffRow(path, 0, "<file total>", f"{m_vol:.2f}", "—", "removed"))
        elif h_vol is not None and m_vol is None:
            rows.append(DiffRow(path, 0, "<file total>", "—", f"{h_vol:.2f}", "added"))

        m_funcs = mf.get("functions") if mf else None
        h_funcs = hf.get("functions") if hf else None
        if not isinstance(m_funcs, dict):
            m_funcs = {}
        if not isinstance(h_funcs, dict):
            h_funcs = {}
        names = set(m_funcs) | set(h_funcs)
        for name in sorted(names):
            mv = m_funcs.get(name)
            hv = h_funcs.get(name)
            m_fv = float(mv["volume"]) if isinstance(mv, dict) and "volume" in mv else None
            h_fv = float(hv["volume"]) if isinstance(hv, dict) and "volume" in hv else None
            if m_fv is not None and h_fv is not None and m_fv != h_fv:
                rows.append(
                    DiffRow(
                        path,
                        0,
                        name,
                        f"{m_fv:.2f}",
                        f"{h_fv:.2f}",
                        f"{h_fv - m_fv:+.2f}",
                    )
                )
            elif m_fv is not None and h_fv is None:
                rows.append(DiffRow(path, 0, name, f"{m_fv:.2f}", "—", "removed"))
            elif h_fv is not None and m_fv is None:
                rows.append(DiffRow(path, 0, name, "—", f"{h_fv:.2f}", "added"))

    return rows


def _print_table(rows: list[DiffRow], metric: Metric) -> None:
    col_main, col_head = ("Main", "HEAD")
    if metric == "mi":
        col_main, col_head = "Main MI", "HEAD MI"
    elif metric == "hal":
        col_main, col_head = "Main vol.", "HEAD vol."

    w_path = max(len(r.path) for r in rows)
    w_sym = max(len(r.symbol) for r in rows)
    header = f"{'Location':<{w_path}}  Line  {'Symbol':<{w_sym}}  {col_main:>10}  {col_head:>10}  {'Δ':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.path:<{w_path}}  {r.lineno:4d}  {r.symbol:<{w_sym}}  {r.main_val:>10}  {r.head_val:>10}  {r.delta:>10}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default=None, help="Git ref to compare against (default: main or origin/main)")
    ap.add_argument(
        "--metric",
        choices=("cc", "mi", "hal"),
        default="cc",
        help="cc=McCabe cyclomatic complexity; mi=maintainability index; hal=Halstead (volume)",
    )
    args = ap.parse_args()
    metric: Metric = args.metric
    main_ref = _resolve_ref(args.ref)

    with tempfile.TemporaryDirectory() as tmp:
        main_app = _extract_app(main_ref, Path(tmp))
        raw_m = json.loads(_run_radon(metric, main_app, ["-j"]))
        main_norm = _normalize_json_keys(raw_m)
        if metric == "cc":
            avg_m = _parse_cc_average(_run_radon(metric, main_app, ["-a"]))
        elif metric == "mi":
            avg_m = _avg_mi_files(main_norm)
        else:
            avg_m = _avg_hal_file_volume(main_norm)

    raw_h = json.loads(_run_radon(metric, "app", ["-j"]))
    head_norm = _normalize_json_keys(raw_h)

    label_avg: str
    avg_h: float

    if metric == "cc":
        avg_h = _parse_cc_average(_run_radon(metric, "app", ["-a"]))
        label_avg = "Average complexity"
        rows = _diff_cc(_parse_cc_blocks(main_norm), _parse_cc_blocks(head_norm))
    elif metric == "mi":
        avg_h = _avg_mi_files(head_norm)
        label_avg = "Average MI (mean per file)"
        rows = _diff_mi(main_norm, head_norm)
    else:
        avg_h = _avg_hal_file_volume(head_norm)
        label_avg = "Average Halstead volume (mean of file totals)"
        rows = _diff_hal(main_norm, head_norm)

    print(f"Metric: {metric.upper()} — compared app/ to {main_ref}@{REPO_ROOT.name} (git archive backend/app)")
    print()
    print(f"{label_avg} ({main_ref}): {avg_m:.2f}")
    print(f"{label_avg} (HEAD):       {avg_h:.2f}")
    print(f"Δ ({label_avg}, HEAD − {main_ref}): {avg_h - avg_m:+.2f}")
    print()

    if not rows:
        print("No per-item differences for this metric.")
        return

    _print_table(rows, metric)


if __name__ == "__main__":
    main()
