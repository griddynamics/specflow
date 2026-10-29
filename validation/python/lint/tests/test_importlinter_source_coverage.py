"""`kit_tools/importlinter_source_coverage.py`: a new top-level package missing from a
hand-written `source_modules` list is reported; exempt packages and covered ones are not."""

from __future__ import annotations

from pathlib import Path

import pytest

from kit_tools.importlinter_source_coverage import check_coverage, main

CONTRACTS = """\
[tool.importlinter]
root_package = "app"

[[tool.importlinter.contracts]]
name = "Vendor SDK confined to the gateway"
id = "sdk-confined"
type = "forbidden"
source_modules = ["app.billing", "app.orders.service"]
forbidden_modules = ["vendor_sdk"]
"""


def _project(tmp_path: Path, *packages: str) -> tuple[Path, Path]:
    contracts = tmp_path / "importlinter.toml"
    contracts.write_text(CONTRACTS)
    root = tmp_path / "app"
    root.mkdir()
    (root / "__init__.py").write_text("")
    for package in packages:
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("")
    # A plain directory without `__init__.py` is not a package and is never required.
    (root / "static").mkdir()
    return contracts, root


def test_listed_packages_and_modules_inside_them_cover_the_contract(tmp_path: Path) -> None:
    contracts, root = _project(tmp_path, "billing", "orders")
    coverage = check_coverage(contracts, root, ["sdk-confined"], set())
    assert coverage.missing == {}
    assert coverage.unknown_contracts == []


def test_a_new_package_missing_from_the_list_is_reported(tmp_path: Path) -> None:
    contracts, root = _project(tmp_path, "billing", "orders", "shipping")
    coverage = check_coverage(contracts, root, ["sdk-confined"], set())
    assert coverage.missing == {"sdk-confined": ["shipping"]}


def test_an_exempt_package_is_not_required(tmp_path: Path) -> None:
    contracts, root = _project(tmp_path, "billing", "orders", "gateway")
    coverage = check_coverage(contracts, root, ["sdk-confined"], {"gateway"})
    assert coverage.missing == {}


def test_a_name_prefix_is_not_coverage(tmp_path: Path) -> None:
    # `app.billing` covers `billing`, never `billing_reports`.
    contracts, root = _project(tmp_path, "billing", "orders", "billing_reports")
    coverage = check_coverage(contracts, root, ["sdk-confined"], set())
    assert coverage.missing == {"sdk-confined": ["billing_reports"]}


@pytest.mark.parametrize(
    ("packages", "contract_id", "expected"),
    [
        (("billing", "orders"), "sdk-confined", 0),
        (("billing", "orders", "shipping"), "sdk-confined", 1),
        (("billing", "orders"), "no-such-contract", 2),
    ],
)
def test_exit_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    packages: tuple[str, ...],
    contract_id: str,
    expected: int,
) -> None:
    contracts, root = _project(tmp_path, *packages)
    argv = ["--contracts", str(contracts), "--source-root", str(root), "--contract", contract_id]
    assert main(argv) == expected
    stderr = capsys.readouterr().err
    assert (stderr == "") == (expected == 0)
