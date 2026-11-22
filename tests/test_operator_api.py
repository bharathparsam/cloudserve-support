"""Configuration and input API integration. Provider metadata is synthetic, no inference."""

import threading
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock
from src.api import create_app
from src.operator_settings import OperatorSettingsUnavailable
from src.runtime import load_configuration
from test_integration import fixture_runtime

HEADERS = {"Authorization": "Bearer operator-test"}
VALUES = {
    "generation_model": "test/generator:free",
    "decider_model": "test/observer:free",
    "decider_mode": "disabled",
    "timeout_seconds": 20,
    "max_retries": 1,
}


def test_local_api_keeps_docs_and_retires_console_only_endpoints(fixture_runtime):
    with TestClient(create_app(fixture_runtime, start_worker=False)) as client:
        assert client.get("/").json()["api_docs"] == "/docs"
        assert client.get("/docs").status_code == 200
        for route in ("/api/v1/models", "/api/v1/datasets", "/api/v1/datasets/template"):
            assert client.get(route, headers=HEADERS).status_code == 404
        assert client.put(
            "/api/v1/settings", json={**VALUES, "decider_mode": "enforce"}, headers=HEADERS
        ).status_code == 422


def fixture_adapters(monkeypatch, fail=False, entered=None, resume=None):
    def build(settings, values):
        value = values.model_dump() if hasattr(values, "model_dump") else values

        def preflight():
            if entered:
                entered.set()
                resume.wait(5)
            if fail:
                raise RuntimeError("synthetic provider failure with private data")
            return {"verified": True, "fixture": True}

        p = SimpleNamespace(
            model=value["generation_model"],
            api_key="fixture",
            metadata={"fixture": True},
            preflight=preflight,
        )
        e = (
            None
            if value["decider_mode"] == "disabled"
            else SimpleNamespace(
                provider=SimpleNamespace(preflight=preflight), policy_ready=False
            )
        )
        return p, e

    monkeypatch.setattr("src.api.build_providers", build)


def test_authenticated_settings_no_secret(fixture_runtime):
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        for url in [
            "/api/v1/settings",
        ]:
            assert c.get(url).status_code == 401
        s = c.get("/api/v1/settings", headers=HEADERS).json()
        assert s["source"] == "environment" and s["revision"] == 0
        assert "api_key" not in s and "operator_token" not in s


def test_save_applies_and_restart_resolves_durable_settings(
    fixture_runtime, monkeypatch
):
    fixture_adapters(monkeypatch)
    settings, store, pipeline = fixture_runtime
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        r = c.put("/api/v1/settings", json=VALUES, headers=HEADERS)
        assert r.status_code == 200 and r.json()["revision"] == 1
        assert (
            pipeline.provider.model == VALUES["generation_model"]
            and pipeline.evaluator is None
        )
        assert pipeline.decider_mode == "disabled" and not store.control()["enabled"]
    from src.config import Settings

    restarted = load_configuration(Settings(storage=settings.storage))
    assert (
        restarted.model == VALUES["generation_model"]
        and restarted.timeout_seconds == 20
    )
    assert restarted.max_retries == 1
    with store.connection() as db:
        assert (
            db.execute("select count(*) from operator_settings_events").fetchone()[0]
            == 1
        )
    from evaluation.harness import configured_fingerprint

    fp = configured_fingerprint(restarted)["details"]["parameters"]
    assert fp["timeout_seconds"] == 20 and fp["max_retries"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"generation_model": "test/paid"},
        {"max_retries": 3},
        {"timeout_seconds": False},
        {"api_key": "forbidden"},
    ],
)
def test_invalid_settings_do_not_change_provider(fixture_runtime, change):
    p = fixture_runtime[2].provider
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        assert (
            c.put(
                "/api/v1/settings", json={**VALUES, **change}, headers=HEADERS
            ).status_code
            == 422
        )
    assert fixture_runtime[2].provider is p


def test_free_preflight_failure_does_not_persist(fixture_runtime, monkeypatch):
    fixture_adapters(monkeypatch, fail=True)
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        r = c.put("/api/v1/settings", json=VALUES, headers=HEADERS)
        assert r.status_code == 503 and "private data" not in r.text
        assert c.get("/api/v1/settings", headers=HEADERS).json()["revision"] == 0


def test_settings_enabled_or_active_batch_rejected(fixture_runtime, monkeypatch):
    fixture_adapters(monkeypatch)
    settings, store, p = fixture_runtime
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        store.set_control(True, "fixture")
        assert (
            c.put("/api/v1/settings", json=VALUES, headers=HEADERS).status_code == 409
        )
        store.set_control(False, "fixture")
        store.create_run([{"channel": "chat", "body": "hello"}])
        assert (
            c.put("/api/v1/settings", json=VALUES, headers=HEADERS).status_code == 409
        )


def test_settings_save_excludes_concurrent_enable_but_stop_remains_available(
    fixture_runtime, monkeypatch
):
    entered = threading.Event()
    resume = threading.Event()
    fixture_adapters(monkeypatch, entered=entered, resume=resume)
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        result = []
        t = threading.Thread(
            target=lambda: result.append(
                c.put("/api/v1/settings", json=VALUES, headers=HEADERS)
            )
        )
        t.start()
        assert entered.wait(2)
        try:
            assert (
                c.put(
                    "/api/v1/controls/automation",
                    json={"enabled": True, "reason": "fixture"},
                    headers=HEADERS,
                ).status_code
                == 409
            )
            assert (
                c.put(
                    "/api/v1/controls/automation",
                    json={"enabled": False, "reason": "fixture stop"},
                    headers=HEADERS,
                ).status_code
                == 200
            )
        finally:
            resume.set()
            t.join(5)
        assert (
            result[0].status_code == 200 and not fixture_runtime[1].control()["enabled"]
        )
