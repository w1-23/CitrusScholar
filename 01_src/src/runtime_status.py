"""Read-only capability projections for public health and administrator diagnostics."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from threading import Thread
import time


_SAMPLE_INTERVAL = 5.0
_SAMPLE_MAX_AGE = 15.0
_web_models = None


class RuntimeEvidence:
    """Single-writer snapshots with fail-closed expiry, independent of probe locks."""

    def __init__(self, unavailable):
        self._unavailable = unavailable
        self._sample = None

    def publish(self, details):
        self._sample = (time.monotonic(), details)

    def read(self):
        sample = self._sample
        if sample is None:
            return dict(self._unavailable)
        observed, details = sample
        if time.monotonic() - observed > _SAMPLE_MAX_AGE:
            return self._unavailable | {"reason_code": "evidence_stale"}
        return dict(details)


def set_web_model_evidence(evidence):
    """Share readiness with optional model consumers only during Web lifespan."""
    global _web_models
    _web_models = evidence


def web_model_ready(component):
    evidence = _web_models
    if evidence is None:
        return True  # Standalone callers retain their existing lazy operation.
    status = evidence.read()
    model = status.get("models", {}).get(component, {})
    gpu_required = status.get("require_gpu") or status.get("mode", "gpu") == "gpu"
    return bool(model.get("loaded") and model.get("inference_verified") and not model.get("runtime_error")
                and (not gpu_required or model.get("gpu")))


async def optional_call(name, operation, /, *args, **kwargs):
    """Keep native optional work out of the server's default executor and shutdown."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def deliver(value, error):
        if result.done():
            return
        if error is None:
            result.set_result(value)
        else:
            result.set_exception(error)

    def run():
        try:
            value, error = operation(*args, **kwargs), None
        except Exception as exc:
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            # A native job can finish after its request-independent owner stopped.
            pass

    Thread(target=run, name=f"citrus-{name}", daemon=True).start()
    return await result


async def observe_runtime(name, publish, read, unavailable):
    """Sample serially so a wedged observer cannot create accumulating threads."""
    while True:
        try:
            publish(await optional_call(name, read))
        except Exception as exc:
            publish(unavailable | {"reason_code": type(exc).__name__})
        await asyncio.sleep(_SAMPLE_INTERVAL)


def workspace_id() -> str:
    """Opaque checkout identity for safe startup reuse; never publish a host path."""
    root = os.path.normcase(str(Path(__file__).resolve().parents[2]))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()


def complete_model_evidence(status):
    """Both real model sessions must match the administrator's selected mode."""
    items = status.get('models', {})
    gpu_required = bool(status.get('require_gpu')) or status.get('mode', 'gpu') == 'gpu'
    return all(items.get(name, {}).get('loaded')
               and (not gpu_required or items[name].get('gpu'))
               and items[name].get('inference_verified')
               and not items[name].get('runtime_error')
               for name in ('embedder', 'reranker'))


def required_capabilities_ready(*, stores, retrieval, models, worker, agent, figure_skill,
                                full=False, worker_required=False, external_corpus=False):
    if not stores or not agent.get("ready"):
        return False
    if full and not external_corpus and not (retrieval and models.get("ready") and complete_model_evidence(models)
                                             and figure_skill.get("prompt_loaded")):
        return False
    return not worker_required or bool(worker.get("verified") and figure_skill.get("prompt_loaded"))


def public_readiness(*, stores, retrieval, models, worker, agent, figure_skill,
                     model_configured, full=False, worker_required=False, external_corpus=False, version=""):
    """Optional capabilities do not determine basic service availability."""
    model_ready = bool(models.get("ready"))
    if full:
        model_ready = model_ready and complete_model_evidence(models)
    retrieval_ready = bool(retrieval and (external_corpus or model_ready))
    worker_ready = bool(worker.get("verified"))
    figure_ready = bool(figure_skill.get("prompt_loaded"))
    ready = required_capabilities_ready(stores=stores, retrieval=retrieval, models=models, worker=worker,
                                        agent=agent, figure_skill=figure_skill, full=full,
                                        worker_required=worker_required, external_corpus=external_corpus)
    capabilities = {
        "chat": {"ready": bool(agent.get("ready") and model_configured),
                 "reason_code": "ready" if model_configured else "model_credential_unconfigured"},
        "local_retrieval": {"ready": retrieval_ready,
                            "reason_code": "ready" if retrieval_ready else "retrieval_unavailable"},
        "figure_worker": {"ready": worker_ready, "required": worker_required,
                          "reason_code": "ready" if worker_ready else "worker_unavailable"},
    }
    degraded = not all(item["ready"] for item in capabilities.values())
    return {
        "ready": ready, "status": ("degraded" if degraded else "ready") if ready else "unavailable",
        "version": version, "stores_ready": bool(stores), "retrieval_ready": bool(retrieval),
        "agent_runtime": {"ready": bool(agent.get("ready"))},
        "models": {"ready": model_ready, "mode": models.get("mode", "gpu"),
                   "require_gpu": bool(models.get("require_gpu"))},
        "worker": {"verified": worker_ready, "required": worker_required,
                   "image_fingerprint": hashlib.sha256(str(worker.get("image_id", "")).encode()).hexdigest(),
                   "status": "ready" if worker_ready else "unavailable"},
        "figure_skill": {"prompt_loaded": figure_ready}, "capabilities": capabilities,
        "local_runtime": ({"profile": "full-v1", "instance_id": os.getenv("CITRUS_LOCAL_INSTANCE_ID", ""),
                           "pid": os.getpid(), "workspace_id": workspace_id()} if full else None),
    }
