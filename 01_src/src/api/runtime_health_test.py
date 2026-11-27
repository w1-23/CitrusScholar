"""HTTP contracts for live ASGI serving and independent optional readiness."""
import http.client
from ipaddress import IPv4Network
import json
import socket
from threading import Event, Thread
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import uvicorn

from src.api import main
from src.api.lan_ingress import LanIngress
from src.runtime_status import RuntimeEvidence


def eventually(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            pytest.fail("expected condition did not become true")
        time.sleep(0.01)


def _get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.75)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_uvicorn_serves_live_while_core_model_and_worker_are_blocked(monkeypatch):
    core_started, queue_started, warm_started, worker_started = (Event() for _ in range(4))
    core_release, queue_release, optional_release = (Event() for _ in range(3))
    monkeypatch.setenv("CITRUS_REQUIRE_FIGURE_WORKER", "1")
    monkeypatch.delenv("CITRUS_CODE_EXECUTION", raising=False)
    monkeypatch.delenv("CITRUS_LOCAL_FULL_STARTUP", raising=False)
    monkeypatch.setattr(main.settings, "FIGURES_SANDBOX_BACKEND", "docker")

    def wait_for(started, release):
        started.set()
        if not release.wait(10):
            raise TimeoutError("fixture release missing")

    def initialize():
        wait_for(core_started, core_release)

    async def reconcile():
        import asyncio
        assert main.app.state.task_stores_ready
        await asyncio.to_thread(wait_for, queue_started, queue_release)

    async def scan(_wake, *, health):
        import asyncio
        health.update(last_scan=time.monotonic())
        await asyncio.Event().wait()

    def warm():
        wait_for(warm_started, optional_release)
        raise RuntimeError("simulated GPU probe failure")

    def worker(_backend):
        wait_for(worker_started, optional_release)
        raise RuntimeError("simulated worker failure")

    monkeypatch.setattr(main, "_initialize_core_stores", initialize)
    monkeypatch.setattr(main, "_agent_runtime_readiness", lambda: {"ready": True, "errors": []})
    monkeypatch.setattr(main, "_recover_publications", lambda: None)
    monkeypatch.setattr(main, "_reconcile_turn_queue_startup", reconcile)
    monkeypatch.setattr(main, "_consume_chat_turns", scan)
    monkeypatch.setattr(main, "_warm_retrieval", warm)
    monkeypatch.setattr(main, "_read_model_evidence", lambda: {"ready": False, "mode": "gpu"})
    monkeypatch.setattr(main, "_select_figure_worker", worker)
    monkeypatch.setattr(main, "_warm_fixed_prompts", lambda: None)
    monkeypatch.setattr(main, "_read_figure_prompt", lambda: {
        "prompt_loaded": True, "commit": "fixture", "assembled_prompt_sha256": "fixture",
        "assembled_prompt_chars": 17,
    })
    from src.core import chat_turn_queue, loop_watchdog
    from src.figures import sandbox

    def unexpected_available(_runner):
        pytest.fail("request raced the startup Figure Worker probe")

    monkeypatch.setattr(sandbox.DockerBackend, "available", unexpected_available)
    monkeypatch.setattr(main.memory_store, "_recall_semantic", unexpected_available)
    monkeypatch.setattr(main.memory_store, "_recall_keyword_fallback", lambda *_: "keyword-only")
    monkeypatch.setattr(chat_turn_queue, "ensure_schema", lambda: None)
    monkeypatch.setattr(loop_watchdog, "install", lambda: None)
    monkeypatch.setattr(loop_watchdog, "uninstall", lambda: None)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.app, lifespan="on", log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            eventually(lambda: server.started)
            assert all(started.wait(1) for started in (core_started, warm_started, worker_started))
            status, headers, body = _get(port, "/live")
            assert (status, body, headers["cache-control"]) == (200, {"live": True}, "no-store")
            status, headers, body = _get(port, "/ready")
            assert (status, body) == (503, {"ready": False})
            assert headers["cache-control"] == "no-store"
            assert _get(port, "/api/v2/auth/me")[0] == 503
            from src.retrieval.runtime import RetrievalUnavailable, get_rag
            with pytest.raises(RetrievalUnavailable, match="retrieval_warming"):
                get_rag()
            assert sandbox.select_backend(backend="docker") is None
            assert main.memory_store.recall_long_term_memory("fruit", owner_user_id=42) == "keyword-only"
            core_release.set()
            assert queue_started.wait(1)
            assert _get(port, "/live")[0] == 200
            assert _get(port, "/ready")[0] == 503
            assert _get(port, "/api/v2/auth/me")[0] == 503
            queue_release.set()
            eventually(lambda: main._turn_queue_health(main.app.state)["ready"])
            # Required Worker still not verified. In the original optional
            # profile the same GPU/Worker fault only degrades capabilities.
            assert _get(port, "/ready")[0] == 503
            monkeypatch.setenv("CITRUS_REQUIRE_FIGURE_WORKER", "0")
            assert _get(port, "/ready")[0::2] == (200, {"ready": True})
            optional_release.set()
            eventually(lambda: main.app.state.worker_readiness is not None)
            assert _get(port, "/live")[0] == 200
            assert _get(port, "/ready")[0] == 200
            assert _get(port, "/health")[2]["status"] == "ok"
            with pytest.raises(RetrievalUnavailable, match="retrieval_unavailable"):
                get_rag()
            assert sandbox.select_backend(backend="docker") is None
        finally:
            core_release.set()
            queue_release.set()
            optional_release.set()
            server.should_exit = True
            thread.join(5)
    assert not thread.is_alive()
    assert not hasattr(main.app.state, "core_accepting_requests")


def test_strict_ready_uses_only_fresh_verified_evidence_and_queue(monkeypatch):
    state = main.app.state
    monkeypatch.setenv("CITRUS_LOCAL_FULL_STARTUP", "1")
    monkeypatch.setenv("CITRUS_REQUIRE_FIGURE_WORKER", "yes")
    model = RuntimeEvidence(main._UNVERIFIED_MODELS)
    worker = RuntimeEvidence(main._UNVERIFIED_WORKER)
    queue_consumer = SimpleNamespace(done=lambda: False)
    for name, value in dict(lan_ingress=None, task_stores_ready=True, retrieval_ready=True,
                            worker_readiness=None, agent_readiness={"ready": True},
                            figure_skill_readiness={"prompt_loaded": True}, model_evidence=model,
                            worker_evidence=worker, turn_queue_consumer=queue_consumer,
                            turn_queue_health={"last_scan": time.monotonic(),
                                               "consecutive_failures": 0}).items():
        monkeypatch.setattr(state, name, value, raising=False)
    from src.engine import hardware
    from src.figures import readiness

    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("health must read cached evidence without touching the backing probe")

    monkeypatch.setattr(hardware, "runtime_readiness", unexpected_probe)
    monkeypatch.setattr(readiness, "worker_snapshot", unexpected_probe)
    client = TestClient(main.app)
    local_models = {name: {"loaded": True, "gpu": name != "reranker", "inference_verified": True}
                    for name in ("embedder", "reranker")}
    evidence = {"ready": True, "mode": "gpu", "require_gpu": True, "models": local_models}
    model.publish(evidence)
    worker.publish({"verified": True})
    assert client.get("/ready").status_code == 503  # no silent downshift
    local_models["reranker"]["gpu"] = True
    model.publish(evidence)
    assert client.get("/ready").json() == {"ready": True}
    state.turn_queue_health.update(consecutive_failures=1)
    assert client.get("/ready").status_code == 503
    assert client.get("/live").json() == {"live": True}
    state.turn_queue_health = {"last_scan": None, "consecutive_failures": 0}
    assert client.get("/ready").status_code == 503
    state.turn_queue_health["last_scan"] = time.monotonic()
    assert client.get("/ready").status_code == 200
    model._sample = (time.monotonic() - 16, evidence)
    assert client.get("/ready").status_code == 503
    assert client.get("/live").status_code == 200


def test_lan_health_bypass_is_loopback_get_or_head_only(monkeypatch):
    ingress = LanIngress("192.0.2.18:9443", "127.0.0.1:18043", b"f" * 64,
                         (IPv4Network("192.0.2.16/28"),))
    monkeypatch.setattr(main.app.state, "lan_ingress", ingress, raising=False)
    local = TestClient(main.app, base_url="http://127.0.0.1:18043", client=("127.0.0.1", 50000))
    other = TestClient(main.app, base_url="http://127.0.0.1:18043", client=("192.0.2.21", 50000))
    proxy = TestClient(main.app, base_url="https://192.0.2.18:9443", client=("127.0.0.1", 50000))
    assert local.get("/live").status_code == 200
    assert local.head("/live").status_code == 200
    assert other.get("/live").status_code == 400
    assert proxy.get("/live").status_code == 403
    assert local.post("/live").status_code == 400
    assert local.get("/live/private").status_code == 400
    assert local.get("/api/v2/auth/me").status_code == 400


def test_ready_embedder_admits_semantic_memory_without_requiring_the_reranker(monkeypatch):
    from src.runtime_status import set_web_model_evidence

    evidence = RuntimeEvidence(main._UNVERIFIED_MODELS)
    monkeypatch.setattr(main.memory_store, "_recall_semantic", lambda *_: "semantic")
    monkeypatch.setattr(main.memory_store, "_recall_keyword_fallback", lambda *_: "keyword")
    set_web_model_evidence(evidence)
    try:
        assert main.memory_store.recall_long_term_memory("fruit", owner_user_id=42) == "keyword"
        for gpu, expected in ((False, "keyword"), (True, "semantic")):
            evidence.publish({"ready": False, "mode": "gpu", "models": {"embedder": {
                "loaded": True, "inference_verified": True, "gpu": gpu,
            }}})
            assert main.memory_store.recall_long_term_memory("fruit", owner_user_id=42) == expected
    finally:
        set_web_model_evidence(None)


def test_web_figure_selection_recovers_and_skips_redundant_pinned_probe(monkeypatch, tmp_path):
    from src.figures import readiness, sandbox
    from src.figures.apptainer import ApptainerBackend
    from src.graph.tool_availability import _SANDBOX_PROBE, script_sandbox_available

    started, release = Event(), Event()
    probes, pin_checks = [], []
    options = {"backend": "apptainer", "image": str(tmp_path / "fixture.sif"),
               "image_sha256": "a" * 64, "runtime": "/usr/bin/apptainer",
               "wsl_distribution": "Ubuntu-24.04"}
    for argument, field in {"backend": "FIGURES_SANDBOX_BACKEND", "image": "FIGURES_SANDBOX_IMAGE",
            "image_sha256": "FIGURES_SANDBOX_IMAGE_SHA256", "runtime": "FIGURES_SANDBOX_RUNTIME",
            "wsl_distribution": "FIGURES_SANDBOX_WSL_DISTRO"}.items():
        monkeypatch.setattr(main.settings, field, options[argument])
    monkeypatch.setattr(main.settings, "FIGURES_SANDBOX_ALLOW_LOCAL", False)
    monkeypatch.setenv("CITRUSQA_LOOP_WATCHDOG", "0")

    def validate_image(runner):
        pin_checks.append((runner.image, runner.image_sha256))
        if len(pin_checks) > 1:
            # Prove an actual run still stops at the job-time pin check.
            raise sandbox.SandboxUnavailable("fixture SIF no longer matches its pin")
        return tmp_path / "fixture.sif"

    def verify(_settings, runner):
        assert runner.available()
        return {"backend": "apptainer", "verified": True, "image_id": runner.image_id}

    def available(runner):
        probes.append(runner)
        started.set()
        assert release.wait(3)
        runner._validate_image()
        return True

    async def no_service(_state):
        pass

    for service in ("_serve_core_runtime", "_serve_model_runtime", "_serve_figure_prompt"):
        monkeypatch.setattr(main, service, no_service)
    monkeypatch.setattr(main, "_warm_fixed_prompts", lambda: None)
    monkeypatch.setattr(ApptainerBackend, "available", available)
    monkeypatch.setattr(ApptainerBackend, "_validate_image", validate_image)
    monkeypatch.setattr(ApptainerBackend, "_validate_host", lambda _: None)
    monkeypatch.setattr(readiness, "verify_worker", verify)
    evidence = {"backend": "apptainer", "verified": True, "image_id": "sha256:" + options["image_sha256"]}
    monkeypatch.setattr(readiness, "worker_snapshot", lambda _: evidence)
    from src import runtime_status
    monkeypatch.setattr(runtime_status, "_SAMPLE_INTERVAL", 60.)
    # A standalone result, even if already cached, cannot poison Web availability.
    cached_key = (options["backend"], options["image"], options["image_sha256"], options["runtime"],
                  False, options["wsl_distribution"])
    with TestClient(main.app):
        try:
            assert started.wait(1)
            assert not script_sandbox_available()
            assert cached_key not in _SANDBOX_PROBE
            monkeypatch.setitem(_SANDBOX_PROBE, cached_key, False)
            assert sandbox.select_backend(**options) is None
        finally:
            release.set()
        eventually(script_sandbox_available)
        for _ in range(3):
            assert sandbox.select_backend(**options) is probes[0]
            assert script_sandbox_available()
        assert sandbox.select_backend(**(options | {"image_sha256": "b" * 64})) is None
        assert sandbox.select_backend(**(options | {"runtime": "/wrong/apptainer"})) is None
        assert len(probes) == len(pin_checks) == 1
        with pytest.raises(sandbox.SandboxUnavailable, match="no longer matches its pin"):
            probes[0].run(sandbox.SandboxJob(script="pass"))
        assert len(probes) == 1 and len(pin_checks) == 2
        main.app.state.worker_evidence.publish({"backend": "apptainer", "verified": False})
        assert not script_sandbox_available()
        assert sandbox.select_backend(**options) is None


def test_unified_readiness_does_not_require_legacy_host_models_or_corpus():
    from src.runtime_status import public_readiness, required_capabilities_ready

    evidence = dict(stores=True, retrieval=False, models=main._UNVERIFIED_MODELS,
                    worker={"verified": True}, agent={"ready": True}, figure_skill={"prompt_loaded": True},
                    full=True, worker_required=True, external_corpus=True)
    assert required_capabilities_ready(**evidence)
    diag = public_readiness(**evidence, model_configured=True)
    assert not diag['capabilities']['local_retrieval']['ready']
    assert public_readiness(**{**evidence, "retrieval": True}, model_configured=True)['capabilities']['local_retrieval']['ready']
    assert not required_capabilities_ready(**{**evidence, "external_corpus": False})
    assert not required_capabilities_ready(**{**evidence, "worker": {"verified": False}})
