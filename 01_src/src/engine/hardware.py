"""ONNX Runtime provider selection with a persisted administrator switch."""
from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
import sys
from pathlib import Path
from typing import Any

def _ort():
    """Load the optional inference runtime only for an explicit hardware operation."""
    import onnxruntime
    return onnxruntime

from src.config import PROJECT_ROOT, _atomic_private_json, _yaml_val

logger = logging.getLogger(__name__)

VALID_DEVICE_MODES = frozenset({"auto", "gpu", "cpu"})
_DEVICE_LOCK = threading.RLock()
_SWITCH_LOCK = threading.Lock()
_ACTIVE_MODELS: tuple[str, Any, Any, int] | None = None
_SWITCH_TIMEOUT_SEC = 240.0
_switch_work: threading.Thread | None = None
_DEVICE_PATH = PROJECT_ROOT / "state" / "onnx_device.json"
_GPU_PROVIDERS = frozenset({
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "DmlExecutionProvider",
    "TensorrtExecutionProvider",
})
_runtime_mode: str | None = None
# A provider can be *listed* as available and still be unusable on this host: the
# DirectML provider, for instance, completes a handful of inferences and then stops
# making progress, which hangs every request that touches the reranker.  Selection
# therefore probes the candidate once and demotes it permanently for this process.
_PROBE_MODEL = "ep_probe.onnx"
_PROBE_RUNS = 10
_PROBE_TIMEOUT_SEC = 60.0
_probe_cache: dict[str, tuple[bool, str]] = {}


class GPUUnavailableError(RuntimeError):
    """The operator requires GPU inference but it cannot be verified."""


def require_gpu(mode: str | None = None) -> bool:
    # A selected GPU mode also forbids model-level CPU retry/fallback. The
    # administrator can explicitly select cpu/auto when a fallback is desired.
    return os.getenv("CITRUS_REQUIRE_GPU", "").strip() == "1" or (mode if mode is not None else _read_mode()) == "gpu"


def cpu_thread_budget() -> int:
    """Bound CPU work even in GPU images (tokenization and ORT host work)."""
    return max(1, int(os.getenv("CITRUSQA_CPU_THREADS", "8") or 8))


def session_options(*, strict: bool = False, provider: str = "") -> Any:
    options = _ort().SessionOptions()
    options.intra_op_num_threads = cpu_thread_budget()
    options.inter_op_num_threads = 1
    if provider == "DmlExecutionProvider":
        # DirectML does not support ORT memory patterns or parallel execution.
        options.enable_mem_pattern = False
        options.execution_mode = _ort().ExecutionMode.ORT_SEQUENTIAL
    if strict:
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    return options


def model_session(model: Any) -> Any:
    """Find the actual ORT session in Optimum and FastEmbed wrappers."""
    current = model
    for _ in range(5):
        if current is None:
            break
        if callable(getattr(current, "get_providers", None)):
            return current
        session = getattr(current, "session", None)
        current = session if session is not None else getattr(current, "model", None)
    return None


def model_session_status(model: Any) -> dict[str, Any]:
    session = model_session(model)
    if session is None:
        return {"providers": [], "gpu": False, "error": "ONNX session is not loaded or inspectable"}
    try:
        providers = list(session.get_providers())
        return {"providers": providers, "gpu": bool(providers and providers[0] in _GPU_PROVIDERS)}
    except Exception as exc:
        return {"providers": [], "gpu": False, "error": str(exc)}


def verify_model_session(model: Any, component: str, *, strict: bool | None = None) -> dict[str, Any]:
    status = model_session_status(model)
    if strict is None:
        strict = require_gpu()
    if strict:
        if not status["gpu"]:
            raise GPUUnavailableError(
                f"{component}: CITRUS_REQUIRE_GPU=1 but actual ONNX session providers "
                f"are {status['providers']}. Check GPU device exposure (--nv), ONNX GPU libraries "
                "and the cached model; CPU fallback is disabled.")
        session = model_session(model)
        disable = getattr(session, "disable_fallback", None)
        if callable(disable):
            disable()
    return status


def _cpu_control_node(args: dict[str, Any]) -> bool:
    """Accept only bounded shape/index bookkeeping, never model tensor arithmetic.

    ORT deliberately puts shape vectors and attention dimension scalars on CPU.
    A missing/unknown profile type or a tensor larger than eight elements fails
    closed. Floating point is allowed only for scalar dimension Cast/Sqrt/Div.
    """
    op = args.get("op_name")
    vectors = {"Gather", "Unsqueeze", "Concat", "Reshape", "Equal", "Where", "Slice"}
    scalars = {"Cast", "Sqrt", "Div"}
    if op not in vectors | scalars | {"Shape"}:
        return False
    def bounded(items, types, maximum):
        if not isinstance(items, list) or not items:
            return False
        for item in items:
            if not isinstance(item, dict) or len(item) != 1:
                return False
            dtype, dimensions = next(iter(item.items()))
            if dtype not in types or not isinstance(dimensions, list):
                return False
            size = 1
            for dim in dimensions:
                if type(dim) is not int or dim < 0:
                    return False
                size *= dim
            if size > maximum:
                return False
        return True
    integer = {"int32", "int64", "bool"}
    inputs, outputs = args.get("input_type_shape"), args.get("output_type_shape")
    if op == "Shape":
        # Shape reads tensor dimensions, not its floating-point data.
        return bounded(outputs, {"int32", "int64"}, 8)
    if op in vectors:
        return bounded(inputs, integer, 8) and bounded(outputs, integer, 8)
    if op == "Cast":
        return bounded(inputs, {"int32", "int64"}, 1) and bounded(outputs, {"float", "double"}, 1)
    return bounded(inputs, {"float", "double"}, 1) and bounded(outputs, {"float", "double"}, 1)


def profile_compute_providers(session: Any, *, allow_cpu_control: bool = False,
                              evidence: dict[str, Any] | None = None) -> list[str]:
    """Report real compute; optionally record explicitly bounded CPU control ops."""
    path = Path(session.end_profiling())
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
        providers, control = set(), {}
        for event in events:
            args = event.get("args", {})
            if event.get("cat") != "Node" or not args.get("provider"):
                continue
            if args.get("op_name") in {"MemcpyFromHost", "MemcpyToHost"}:
                continue
            if allow_cpu_control and args["provider"] == "CPUExecutionProvider" and _cpu_control_node(args):
                op = args["op_name"]
                control[op] = control.get(op, 0) + 1
            else:
                providers.add(args["provider"])
        if evidence is not None:
            evidence["cpu_control_ops"] = control
        return sorted(providers)
    finally:
        path.unlink(missing_ok=True)


def _provider_priority() -> list[str]:
    configured = _yaml_val("onnx", "provider_priority", default=None)
    if configured and isinstance(configured, list):
        return [str(value) for value in configured]
    return [
        "ROCMExecutionProvider",
        "CUDAExecutionProvider",
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    ]


def _read_mode() -> str:
    global _runtime_mode
    with _DEVICE_LOCK:
        if _ACTIVE_MODELS is not None:
            return _ACTIVE_MODELS[0]
        if _runtime_mode in VALID_DEVICE_MODES:
            return str(_runtime_mode)
        env_mode = str(os.getenv("CITRUS_ONNX_DEVICE") or "").strip().casefold()
        if env_mode in VALID_DEVICE_MODES:
            _runtime_mode = env_mode
            return env_mode
        try:
            payload = json.loads(_DEVICE_PATH.read_text(encoding="utf-8"))
            persisted = str(payload.get("mode") or "").strip().casefold()
            if persisted in VALID_DEVICE_MODES:
                _runtime_mode = persisted
                return persisted
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not read ONNX device preference: %s", exc)
        _runtime_mode = "gpu"
        return "gpu"


def _write_mode(mode: str) -> None:
    _atomic_private_json(_DEVICE_PATH, {"mode": mode})


def get_device_mode() -> str:
    return _read_mode()


def active_runtime_model(component: str) -> Any | None:
    # Read one immutable generation. A call taking its model before publication
    # holds that concrete instance until that call finishes.
    generation = _ACTIVE_MODELS
    return None if generation is None else generation[1 if component == "embedder" else 2]


def runtime_model_generation() -> int:
    """Distinguish cache data generated before/after a successful runtime activation."""
    generation = _ACTIVE_MODELS
    return generation[3] if generation is not None else 0


def _probe_model_path() -> "os.PathLike[str] | None":
    """Build a tiny Gemm model once; it is only used to exercise an EP."""
    path = PROJECT_ROOT / "state" / _PROBE_MODEL
    if path.is_file():
        return path
    try:
        import numpy as np
        import onnx
        from onnx import TensorProto, helper, numpy_helper
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("[Hardware] Cannot build EP probe model: %s", exc)
        return None
    weight = numpy_helper.from_array(
        np.eye(8, dtype=np.float32) * 0.5, name="w")
    node = helper.make_node("Gemm", ["x", "w"], ["y"], alpha=1.0, beta=0.0)
    graph = helper.make_graph(
        [node], "ep_probe",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [4, 8])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [4, 8])],
        [weight])
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 17)],
        producer_name="citrus-qa-ep-probe")
    model.ir_version = 9
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(model.SerializeToString())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def probe_provider(provider: str, cache: dict | None = None) -> tuple[bool, str]:
    """Return (usable, reason). Runs the probe in a watchdog thread so a hung EP
    is reported as a failure instead of blocking startup forever."""
    cache = _probe_cache if cache is None else cache
    if provider in cache:
        return cache[provider]
    if provider == "CPUExecutionProvider":
        cache[provider] = (True, "cpu")
        return cache[provider]
    try:
        path = _probe_model_path()
    except Exception as exc:
        cache[provider] = (False, f"cannot prepare EP probe: {exc}")
        return cache[provider]
    if path is None:
        cache[provider] = (False, "probe unavailable: install onnx and provide a writable state directory")
        return cache[provider]

    result: dict[str, object] = {}

    def _run() -> None:
        try:
            import numpy as np
            with tempfile.TemporaryDirectory(prefix="citrus-ep-probe-") as profile_dir:
                options = session_options(provider=provider)
                options.enable_profiling = True
                options.profile_file_prefix = str(Path(profile_dir) / "probe")
                session = _ort().InferenceSession(
                    str(path), providers=[provider], sess_options=options)
                actual = list(session.get_providers())
                if not actual or actual[0] != provider:
                    raise GPUUnavailableError(f"requested {provider}, actual session providers={actual}")
                session.disable_fallback()
                data = np.ones((4, 8), dtype=np.float32)
                for _ in range(_PROBE_RUNS):
                    output = session.run(None, {"x": data})
                    if not np.allclose(output[0], data * 0.5):
                        raise RuntimeError("Gemm probe returned an incorrect result")
                executed = profile_compute_providers(session)
                if executed != [provider]:
                    raise GPUUnavailableError(
                        f"requested {provider}, actual session providers={actual}, "
                        f"executed compute providers={executed}")
                result["ok"] = True
                result["reason"] = f"actual={actual}; executed compute={executed}"
        except BaseException as exc:  # noqa: BLE001 - report, never raise
            result["ok"] = False
            result["reason"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_run, name=f"ep-probe-{provider}", daemon=True)
    thread.start()
    thread.join(_PROBE_TIMEOUT_SEC)
    if thread.is_alive():
        usable, reason = False, f"no progress after {_PROBE_RUNS} runs (>{_PROBE_TIMEOUT_SEC:.0f}s)"
    else:
        usable = bool(result.get("ok"))
        reason = str(result.get("reason") or "probe failed")
    cache[provider] = (usable, reason)
    if not usable:
        logger.error(
            "[Hardware] Execution provider %s is advertised but unusable (%s); "
            "falling back to CPU. Set onnx.provider_priority to override.",
            provider, reason)
    else:
        logger.info("[Hardware] Execution provider %s passed the probe (%s)", provider, reason)
    return cache[provider]


def _usable_provider_order(providers: list[str], cache: dict | None = None) -> list[str]:
    """Keep the configured order but drop GPU providers that fail the probe."""
    kept: list[str] = []
    for provider in providers:
        if provider in _GPU_PROVIDERS:
            ok, _reason = probe_provider(provider, cache)
            if not ok:
                continue
        kept.append(provider)
    return kept or ["CPUExecutionProvider"]


def mark_provider_unusable(provider: str, reason: str, *, runtime: Any = None) -> None:
    """Record at runtime that a provider is broken, so later sessions skip it."""
    if not provider or provider == "CPUExecutionProvider":
        return
    with _DEVICE_LOCK:
        active = active_runtime_model("reranker")
        if runtime is not None and active is not None and active is not runtime:
            return
        _probe_cache[provider] = (False, reason)
    logger.error("[Hardware] Marking %s unusable for this process: %s", provider, reason)

def get_ort_providers(*, mode: str | None = None, cache: dict | None = None) -> list[str]:
    available = list(_ort().get_available_providers())
    mode = mode if mode is not None else _read_mode()
    logger.info("[Hardware] ONNX available providers=%s mode=%s", available, mode)
    strict = require_gpu(mode)
    if mode == "cpu":
        if strict:
            raise GPUUnavailableError("CITRUS_REQUIRE_GPU=1 conflicts with ONNX device mode cpu; select gpu or auto")
        return ["CPUExecutionProvider"]
    if mode == "gpu" or strict:
        preferred = [
            provider for provider in _provider_priority()
            if provider in available and provider in _GPU_PROVIDERS
        ]
        preferred = _usable_provider_order(preferred, cache)
        if preferred and preferred[0] in _GPU_PROVIDERS:
            return preferred
        logger.error("[Hardware] GPU mode requested but no usable GPU provider is available")
        raise GPUUnavailableError(
            "GPU mode or CITRUS_REQUIRE_GPU=1: no verified GPU execution provider. "
            f"Available={available}; probe failures={_probe_cache if cache is None else cache}. "
            "Check container --nv, CUDA/cuDNN libraries, onnxruntime-gpu and provider_priority.")
    matched = [
        provider for provider in _provider_priority()
        if provider in available
    ]
    return _usable_provider_order(matched, cache)


def get_device_status(*, probe: bool = True) -> dict[str, Any]:
    """Inspect sessions; only an explicit administrator operation may probe GPUs."""
    selection_error = None
    runtime_available = True
    try:
        available = list(_ort().get_available_providers())
    except (ImportError, OSError) as exc:
        available = []
        runtime_available = False
        selection_error = f"onnx_runtime_unavailable:{type(exc).__name__}"
    with _DEVICE_LOCK:
        mode = _read_mode()
        generation = _ACTIVE_MODELS
        probes = _probe_cache.copy()
    strict = require_gpu(mode)
    selected = []
    if runtime_available:
        if probe:
            try:
                selected = get_ort_providers(mode=mode, cache=probes)
            except GPUUnavailableError as exc:
                selection_error = str(exc)
        elif mode == "cpu" and not strict:
            selected = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else []
        else:
            # Missing evidence stays unverified. Never call _usable_provider_order
            # here: it launches probe threads and writes a probe model/profile.
            selected = [p for p in _provider_priority() if p in available
                        and (p not in _GPU_PROVIDERS or probes.get(p, (False, ""))[0])
                        and (p in _GPU_PROVIDERS or (mode == "auto" and not strict))]
            if strict and not any(p in _GPU_PROVIDERS for p in selected):
                selection_error = "gpu_execution_unverified"
    if mode == "cpu" and strict:
        selection_error = "CITRUS_REQUIRE_GPU=1 conflicts with ONNX device mode cpu"
    models: dict[str, dict[str, Any]] = {}
    for name, class_name in (("embedder", "Embedder"), ("reranker", "Reranker")):
        module = sys.modules.get(f"src.engine.{name}")
        instance = (generation[1 if name == "embedder" else 2] if generation is not None else
                    getattr(getattr(module, class_name, None), "_initial_runtime", None))
        model = getattr(instance, "_shared_model", None)
        evidence = model_session_status(model)
        evidence["loaded"] = model is not None
        evidence["inference_verified"] = bool(getattr(instance, "_inference_verified", False))
        evidence["runtime_error"] = getattr(instance, "_runtime_error", None)
        evidence["compute_providers"] = list(getattr(instance, "_compute_providers", []))
        evidence["cpu_control_ops"] = dict(getattr(instance, "_profile_evidence", {}).get("cpu_control_ops", {}))
        models[name] = evidence
    return {
        "mode": mode,
        "available_providers": available,
        "selected_providers": selected,
        "gpu_available": any(provider in _GPU_PROVIDERS for provider in available),
        "provider_probe": {name: {"usable": ok, "reason": reason}
                           for name, (ok, reason) in probes.items()},
        "active_model_providers": {name: evidence["providers"] for name, evidence in models.items()
                                   if evidence["loaded"]},
        "models": models,
        "require_gpu": strict,
        "selection_error": selection_error,
        "cpu_threads": cpu_thread_budget(),
    }


def runtime_readiness() -> dict[str, Any]:
    """Read-only readiness evidence; never load/download models or run real-model probes.

    In strict mode both loaded sessions must have completed a forward pass. The
    tiny EP probe separately proves GPU kernel execution. FastEmbed does not
    expose profiling options, so its evidence is session providers + forward.
    """
    status = get_device_status(probe=False)
    errors = [status["selection_error"]] if status["selection_error"] else []
    # Lazy CPU startup remains supported, but a loaded or failed session cannot
    # advertise readiness after a failed forward pass in either device mode.
    for name, model in status["models"].items():
        if model["runtime_error"]:
            errors.append(f"{name}: {model['runtime_error']}")
        elif model["loaded"] and not model["inference_verified"]:
            errors.append(f"{name}: loaded session has not completed a verified inference")
    if status["require_gpu"]:
        for name, model in status["models"].items():
            if not model["loaded"]:
                errors.append(f"{name}: load the cached model and run a local inference to verify GPU readiness")
            elif not model["gpu"]:
                errors.append(f"{name}: actual session providers={model['providers']}; GPU required")
            elif not model["inference_verified"]:
                errors.append(f"{name}: actual GPU session has not completed a verified inference")
            elif name == "reranker" and set(model["compute_providers"]) != {model["providers"][0]}:
                errors.append("reranker: exclusive GPU model compute has not been observed in the loaded session profile")
    status.update(ready=not errors, status="ready" if not errors else "unavailable", errors=errors)
    return status


class DeviceActivationError(RuntimeError):
    """Safe, actionable description of a failed staging step."""


def _stage_runtime_models(mode: str) -> tuple[Any, Any, dict]:
    import math
    from src.engine.embedder import Embedder
    from src.engine.reranker import Reranker

    # Testing a failing candidate must not demote the active generation's EP.
    probes: dict = {}
    try:
        providers = get_ort_providers(mode=mode, cache=probes)
    except GPUUnavailableError as exc:
        raise DeviceActivationError("未检测到通过执行探针的 GPU，或部署要求强制使用 GPU。") from exc
    try:
        embedder = Embedder._new_runtime(mode=mode, providers=providers, staging=True)
        vector = embedder.embed_query("device activation probe")
        if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
            raise ValueError("invalid probe vector")
    except Exception as exc:
        raise DeviceActivationError("嵌入模型无法在所选设备完成推理，请检查模型缓存和运行时。") from exc
    try:
        reranker = Reranker._new_runtime(mode=mode, providers=providers, staging=True)
        ranked = reranker.rerank("device activation probe", [{"text": "local model verification"}], top_k=1)
        if not ranked or not math.isfinite(ranked[0]["rerank_score"]):
            raise ValueError("invalid probe score")
    except Exception as exc:
        raise DeviceActivationError("重排序模型无法在所选设备完成推理，请检查模型缓存和运行时。") from exc
    for name, instance in (("embedder", embedder), ("reranker", reranker)):
        evidence = model_session_status(instance._shared_model)
        if (not evidence["providers"] or evidence["providers"][0] != instance._providers[0]
                or not instance._inference_verified or instance._runtime_error):
            label = "嵌入" if name == "embedder" else "重排序"
            raise DeviceActivationError(f"{label}模型未在所选设备上完成验证。")
    return embedder, reranker, probes


def set_device_mode(mode: str) -> dict[str, Any]:
    """Verify two detached sessions before atomically publishing preference and runtime."""
    global _ACTIVE_MODELS, _probe_cache, _switch_work
    selected = str(mode or "").strip().casefold()
    if selected not in VALID_DEVICE_MODES:
        raise ValueError("mode must be auto, gpu, or cpu")
    override = os.getenv("CITRUS_ONNX_DEVICE", "").strip().casefold()
    if override in VALID_DEVICE_MODES and override != selected:
        raise RuntimeError("部署环境固定了推理设备，未更改当前配置。")
    if not _SWITCH_LOCK.acquire(blocking=False):
        raise RuntimeError("正在验证另一项推理设备切换，请稍后重试。")
    try:
        if _switch_work is not None and _switch_work.is_alive():
            raise RuntimeError("上一次设备验证还未释放资源，请稍后重试。")
        outcome: dict[str, Any] = {}

        def prepare() -> None:
            try:
                outcome["staged"] = _stage_runtime_models(selected)
            except Exception as exc:
                outcome["error"] = exc

        _switch_work = threading.Thread(target=prepare, name="onnx-activation", daemon=True)
        _switch_work.start()
        _switch_work.join(_SWITCH_TIMEOUT_SEC)
        if _switch_work.is_alive():
            raise RuntimeError("推理设备验证超时，当前设备与偏好未改变。")
        if "staged" not in outcome:
            exc = outcome.get("error")
            logger.warning("ONNX activation probe failed: mode=%s error=%s", selected, type(exc).__name__)
            reason = str(exc) if isinstance(exc, DeviceActivationError) else "无法验证所选设备的推理模型。"
            raise RuntimeError(reason + "当前设备与偏好未改变。") from None
        embedder, reranker, probes = outcome["staged"]
        try:
            with _DEVICE_LOCK:
                _write_mode(selected)
                embedder._staging = reranker._staging = False
                _probe_cache = probes
                next_generation = runtime_model_generation() + 1
                _ACTIVE_MODELS = (selected, embedder, reranker, next_generation)
                # Old handles do not own the retired sessions. An operation
                # already using a retired runtime still holds its own reference.
                type(embedder)._initial_runtime = type(reranker)._initial_runtime = None
        except OSError:
            raise RuntimeError("推理设备偏好保存失败，当前设备未改变。") from None
        status = get_device_status(probe=False)
        status["reconfigured"] = ["embedder", "reranker"]
        status["note"] = "嵌入和重排序模型已在所选设备通过验证并切换。"
        return status
    finally:
        _SWITCH_LOCK.release()
