"""Tier 1: /healthz, /readyz and the shared health checks (AC-7.11, AC-4.5, AC-2.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import health
from api.runtime import Runtime
from health import CheckReport, CheckResult, Readiness, run_checks, run_probes
from settings import load_settings
from specflow_api_support import FakeTemporal


async def test_readyz_is_200_with_all_four_checks(harness):
    response = await harness.client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "temporal", "storage", "neo4j"}
    assert all(c["ok"] and c["detail"] == "ok" for c in body["checks"].values())


@pytest.mark.parametrize("failing", ["database", "temporal", "storage", "neo4j"])
async def test_readyz_is_503_naming_the_failing_check_without_secrets(harness, failing):
    async def broken() -> None:
        raise ConnectionError("postgres://user:hunter2@10.1.1.1/db")

    harness.probes[failing] = broken
    response = await harness.client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert [n for n, c in body["checks"].items() if not c["ok"]] == [failing]
    assert body["checks"][failing]["detail"] == "unavailable"
    assert "hunter2" not in response.text and "10.1.1.1" not in response.text


async def test_a_hanging_check_times_out_without_blocking_the_others(harness):
    async def hang() -> None:
        await asyncio.Event().wait()

    harness.runtime.readiness = Readiness({**harness.probes, "storage": hang}, ttl=0, timeout=0.05)
    response = await harness.client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"]["detail"] == "timeout"
    assert response.json()["checks"]["database"]["ok"] is True


async def test_the_snapshot_is_cached_for_ten_seconds_and_shared():
    calls = 0
    now = [0.0]

    async def probe() -> None:
        nonlocal calls
        calls += 1

    readiness = Readiness({"database": probe}, clock=lambda: now[0])
    await asyncio.gather(*(readiness.snapshot() for _ in range(5)))
    assert calls == 1
    now[0] = 9.9
    await readiness.snapshot()
    assert calls == 1
    now[0] = 10.0
    await readiness.snapshot()
    assert calls == 2


async def test_probe_details_are_fixed_codes():
    async def raising() -> None:
        raise RuntimeError("secret-looking text")

    async def hanging() -> None:
        await asyncio.Event().wait()

    report = await run_probes({"a": raising, "b": hanging}, limit_seconds=0.05)
    assert (report["a"].detail, report["b"].detail) == ("unavailable", "timeout")
    assert not report["a"].ok and report["a"].duration_ms >= 0
    assert "secret" not in repr(report)


def test_the_report_exit_code_is_zero_only_when_every_check_is_ok():
    ok = CheckResult("database", ok=True, detail="ok", duration_ms=1)
    bad = CheckResult("storage", ok=False, detail="timeout", duration_ms=2000)
    assert int(CheckReport({"database": ok})) == 0
    assert int(CheckReport({"database": ok, "storage": bad})) == 1
    assert CheckReport({"database": ok}).all_ok and not CheckReport({"storage": bad}).all_ok


async def test_the_temporal_probe_fails_when_the_server_is_not_serving():
    temporal = FakeTemporal(healthy=False)
    report = await run_probes({"temporal": health.temporal_probe(temporal)})
    assert report["temporal"].ok is False


async def test_the_neo4j_probe_wraps_the_raising_graph_check():
    class Driver:
        async def verify_connectivity(self) -> None:
            raise ConnectionError("down")

    report = await run_probes({"neo4j": health.neo4j_probe(Driver(), "neo4j")})
    assert report["neo4j"].ok is False and report["neo4j"].detail == "unavailable"


async def test_run_checks_for_the_worker_covers_db_temporal_storage_and_neo4j_only_with_agents(monkeypatch, valid_env):

    async def ok() -> None:
        return None

    closed: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            closed.append("engine")

    class Driver:
        async def close(self) -> None:
            closed.append("driver")

    monkeypatch.setattr("db.session.build_engine", lambda _settings: Engine())
    monkeypatch.setattr("graph.core.connect", lambda _config: Driver())
    monkeypatch.setattr(
        "storage.factory.build_object_store",
        lambda _settings, **_kw: SimpleNamespace(check=ok),
    )
    monkeypatch.setattr(health, "database_probe", lambda _engine: ok)
    monkeypatch.setattr(health, "_temporal_check", lambda _settings: ok)
    monkeypatch.setattr(health, "neo4j_probe", lambda _driver, _database: ok)

    both = await run_checks("worker", settings=load_settings("worker", valid_env))
    assert set(both) == {"database", "temporal", "storage", "neo4j"} and int(both) == 0
    assert closed == ["engine", "driver"]
    parse_only = await run_checks("worker", settings=load_settings("worker", valid_env, queues=["parse"]))
    assert set(parse_only) == {"database", "temporal", "storage"}


async def test_run_checks_exit_code_is_one_when_a_dependency_is_down(monkeypatch, valid_env):

    async def ok() -> None:
        return None

    async def down() -> None:
        raise ConnectionError("down")

    monkeypatch.setattr("db.session.build_engine", lambda _s: SimpleNamespace(dispose=ok))
    monkeypatch.setattr("storage.factory.build_object_store", lambda _s, **_kw: SimpleNamespace(check=ok))
    monkeypatch.setattr(health, "database_probe", lambda _e: down)
    monkeypatch.setattr(health, "_temporal_check", lambda _s: ok)
    report = await run_checks("worker", settings=load_settings("worker", valid_env, queues=["parse"]))
    assert int(report) == 1 and report["database"].ok is False


def test_the_runtime_dataclass_is_the_only_shared_state():
    assert {
        "settings",
        "session_factory",
        "store",
        "temporal",
        "driver",
        "readiness",
    } <= set(Runtime.__dataclass_fields__)
