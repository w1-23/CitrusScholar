"""Configured launcher smoke tests with only synthetic deployment state."""
from contextlib import nullcontext
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

import apptainer_launch
import configured_local
from src.code_execution import presence


def fail(*_args, **_kwargs):
    pytest.fail("unexpected external Worker or process operation")


@pytest.mark.parametrize("offline_code", [True, False])
def test_configured_web_reaches_asgi_without_worker_heartbeat(tmp_path, monkeypatch, offline_code):
    runtime = tmp_path / "runtime"
    corpus = tmp_path / "corpus"
    model_cache = tmp_path / "model-cache"
    figure_staging = tmp_path / "figures"
    for path in (runtime / "state", corpus, model_cache, figure_staging):
        path.mkdir(parents=True)
    model = runtime / "state" / "model_config.json"
    model.write_text('{"provider_id":"fixture","main_model":"synthetic-model"}', encoding="utf-8")
    (corpus / "active_index.json").write_text('{}', encoding="utf-8")
    heartbeat = runtime / "state" / "code_executions.sqlite3"
    assert not presence.worker_available(heartbeat)
    monkeypatch.setattr(presence, "worker_available", fail)
    settings = SimpleNamespace(model_transport=lambda **_: SimpleNamespace(api_key="synthetic-fixture"),
        RESOLVED_PROVIDER_ID="fixture", RESOLVED_MAIN_MODEL="synthetic-model",
        bind_readonly_model_source=lambda path: None if path == runtime else fail())
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(settings=settings))
    from src.figures import readiness
    monkeypatch.setattr(readiness, "validate_apptainer_staging", lambda: figure_staging)
    monkeypatch.setattr(configured_local, "sys", SimpleNamespace(platform="linux", argv=[],
                                                                prefix=sys.prefix, path=list(sys.path)))
    monkeypatch.setattr(configured_local, "os", SimpleNamespace(getuid=lambda: 1000, environ=dict(os.environ)))
    environ = configured_local.os.environ
    environ["CITRUS_CODE_EXECUTION"] = "previous-setting"
    monkeypatch.setattr(configured_local.socket, "socket", lambda: nullcontext(
        SimpleNamespace(connect_ex=lambda address: 1 if address == ("127.0.0.1", 18043) else fail())))
    calls = []

    def asgi(app, **options):
        calls.append((app, options))
        assert environ["CITRUSQA_PROJECT_ROOT"] == str(runtime)
        assert environ["CITRUS_LOCAL_FULL_STARTUP"] == "1"
        assert environ["CITRUS_REQUIRE_FIGURE_WORKER"] == "1"
        assert environ.get("CITRUS_CODE_EXECUTION") == ("offline-development" if offline_code else None)
        assert not heartbeat.exists()

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=asgi))
    argv = ["configured_local.py", "--runtime-root", str(runtime), "--model-config-root", str(runtime),
            "--corpus-root", str(corpus), "--model-cache-root", str(model_cache),
            "--worker-staging", str(figure_staging), "--backend", "apptainer", "--image",
            str(tmp_path / "synthetic.sif"), "--image-sha256", "a" * 64, "--port", "18043", "--require-full",
            "--existing-runtime", "--without-source-web-search"]
    if offline_code:
        argv.append("--offline-code")
    # argparse uses the process argv, while the launcher owns only its local copy of sys.path.
    monkeypatch.setattr(sys, "argv", argv)
    configured_local.main()
    assert calls == [("src.api.main:app", {"host": "127.0.0.1", "port": 18043,
                                          "reload": False, "proxy_headers": False})]
    assert not heartbeat.exists()


def test_wait_worker_cli_still_requires_fresh_release_heartbeat(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps({"source_root": str(tmp_path), "runtime_root": str(tmp_path)}),
                          encoding="utf-8")
    unit = "citrusscholar-worker@fixture.service"
    monkeypatch.setattr(sys, "argv", ["apptainer_launch.py", "--config", str(deployment),
                                      "--role", "wait-worker", "--worker-unit", unit])
    monkeypatch.setattr(apptainer_launch.sys, "platform", "linux")
    monkeypatch.setattr(apptainer_launch.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(apptainer_launch, "command", fail)
    monkeypatch.setattr(apptainer_launch, "release_identity", lambda _: "a" * 64)
    monkeypatch.setattr(apptainer_launch, "_worker_activation", lambda actual: "b" * 32 if actual == unit else fail())
    seen = []
    live_probe = presence.worker_available

    def check(path, **options):
        seen.append((path, options))
        return live_probe(path, **options)

    monkeypatch.setattr(presence, "worker_available", check)
    with monkeypatch.context() as clock:
        ticks = iter((0., 0., 91.))
        clock.setattr(time, "monotonic", lambda: next(ticks))
        clock.setattr(time, "sleep", lambda _: None)
        with pytest.raises(SystemExit, match="No new offline Code Worker heartbeat"):
            apptainer_launch.main()
    (path, arguments), = seen
    assert path == tmp_path / "state" / "code_executions.sqlite3"
    assert arguments.keys() == {"since", "release", "activation"}
    assert arguments["release"] == "a" * 64 and arguments["activation"] == "b" * 32
    assert not path.exists()
