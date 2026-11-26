"""
Unified configuration for Citrus RAG Agent using pydantic-settings + YAML.
Priority: environment variable > YAML > pydantic-settings default
"""
import json
import os
import logging
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from openai import OpenAI

# v6（B1b）：源码搬到 01_src/src，但**运行期数据根仍在 agent/**
#   （data/ state/ workspace/ .env 按 B3(a) 原地不动）。
_REPO = Path(__file__).resolve().parents[2]
from src.runtime_paths import resolve_project_root

_PROJECT_ROOT_OVERRIDE = os.getenv("CITRUSQA_PROJECT_ROOT")
PROJECT_ROOT = resolve_project_root(_REPO, _PROJECT_ROOT_OVERRIDE)


def _assert_managed_tree(repo: Path, data_root: Path) -> None:
    """Fail fast when a stray source tree would silently switch the data root.

    ``PROJECT_ROOT`` is derived from ``__file__``, so importing ``src`` from a
    copy of the tree (or from a second ``src/`` at the repository root) moves
    every runtime artifact -- API keys, sessions, the corpus -- to a brand-new
    empty directory.  That looks exactly like "my web-search key was deleted"
    while the real ``agent/`` data is untouched.  Detect the wrong tree loudly
    instead of degrading quietly.
    """
    expected = repo / "agent"
    # 本机与发布包统一使用 “01_src”；旧备份目录里的中文夹名仍按受管树处理，
    # 以免归档副本被误判为“数据根落在源码树外”。其余情况一律 fail fast。
    managed_src_dirs = ("01_src", "01_源码_src")
    if (repo.is_dir() and data_root == expected
            and any((repo / name).is_dir() for name in managed_src_dirs)):
        return
    msg = (
        "[Config] 运行期数据根不在受管源码树内："
        f"__file__ 推导出的仓库根={repo}，数据根={data_root}。"
        "正确的启动方式是 01_src/scripts/run_server.sh（PYTHONPATH=<repo>/01_src"
        " 且 cd <repo>/01_src）。若在仓库根直接 `python -m uvicorn src.api.main:app`，"
        "import 会命中另一棵树并切到空数据根（表现：API key 像被删、会话和语料丢失）。"
    )
    if os.getenv("CITRUSQA_ALLOW_UNMANAGED_TREE", "") == "1":
        logging.getLogger(__name__).warning(msg)
        return
    raise RuntimeError(msg)


if _PROJECT_ROOT_OVERRIDE is None:
    _assert_managed_tree(_REPO, PROJECT_ROOT)
MODEL_CACHE_ROOT = Path(os.getenv(
    "CITRUSQA_MODEL_CACHE_ROOT", str(PROJECT_ROOT / ".hf_cache")
)).expanduser().resolve()
# 配置文件目录：统一为 02_config；旧的 02_配置_config 仅在归档副本里出现，仍需兼容，
# 否则 config.yaml 会被静默忽略、所有阈值退回代码默认值（超时 60s 等）。
_CONFIG_DIR_NAMES = ("02_config", "02_配置_config", "02_配置")
CONFIG_DIR = next(
    (path for path in (_REPO / name for name in _CONFIG_DIR_NAMES) if path.is_dir()),
    _REPO / _CONFIG_DIR_NAMES[0],
)
WEB_DIR = Path(__file__).resolve().parents[1] / "web"  # 前端与源码同目录维护

_yaml_config: Dict[str, Any] = {}

def _load_yaml() -> Dict[str, Any]:
    yaml_path = CONFIG_DIR / "config.yaml"
    if not yaml_path.exists():
        logging.warning("[Config] 未找到 %s，全部使用内置默认值。", yaml_path)
        return {}
    try:
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logging.warning(f"[Config] YAML 加载失败: {e}")
        return {}

_yaml_config = _load_yaml()

def _yaml_val(*keys: str, default=None):
    """Traverse _yaml_config with dot-path keys."""
    val = _yaml_config
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
    return val if val is not None else default


MODEL_PROVIDER_BASES = {
    "deepseek": "https://api.deepseek.com",
    "opencode_go": "https://opencode.ai/inference/go/openai/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "aliyun_bailian": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
MODEL_PROVIDER_ORIGINS = {
    "deepseek": "https://api.deepseek.com",
    "opencode_go": "https://opencode.ai",
    "zhipu": "https://open.bigmodel.cn",
    "aliyun_bailian": "https://dashscope.aliyuncs.com",
}
_MODEL_ACTIVATION_LOCK = threading.RLock()


def model_origin(base_url: str) -> str:
    """Exact scheme, hostname and effective port; invalid/ambiguous URLs have no origin."""
    if not base_url or re.search(r"[\s\x00-\x1f\x7f\\?#]", base_url):
        return ""
    try:
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or "@" in url.netloc
                or "%" in url.netloc or url.netloc.endswith(":")):
            return ""
        host = url.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = url.port
        if port is not None and port != (443 if url.scheme == "https" else 80):
            host += f":{port}"
        return f"{url.scheme}://{host}"
    except (ValueError, UnicodeError):
        return ""


@dataclass(frozen=True)
class ModelTransport:
    provider_id: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    configured_context_window: int | None = None


@contextmanager
def _model_file_lock(path: Path):
    """Serialize compare-and-replace across serving processes on the same state volume."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    if os.name != "nt":
        # The atomic replacement is already visible. Never report failure after
        # that commit point, even on filesystems that cannot fsync directories.
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            logging.getLogger(__name__).warning("Could not fsync runtime state directory")


class Settings(BaseSettings):
    # v8.4.5: case_sensitive=False——兼容 Windows 上历史遗留的小写环境变量
    # （deepseek_api_key 等）。case_sensitive=True 在 pydantic-settings>=2.12
    # 下会把大小写不匹配的 env 键判为 extra 并拒绝启动（跨版本兼容性修复）。
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    # 1. API Keys
    LLAMA_CLOUD_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    # Deployment-owned credential for DeepSeek Search/Fetch, independent from
    # the user-configurable main-model provider key.
    DEEPSEEK_WEB_API_KEY: str = ""

    # 旧分类器配置仅为迁移兼容保留；当前工具链不注册该分类器。
    VISION_ENABLED: bool = False
    VISION_MODEL_DIR: str = Field(default_factory=lambda: _yaml_val("vision", "model_dir", default=""))
    VISION_CONFIDENCE_THRESHOLD: float = Field(
        default_factory=lambda: _yaml_val("vision", "confidence_threshold", default=0.60))
    VISION_MARGIN_THRESHOLD: float = Field(
        default_factory=lambda: _yaml_val("vision", "margin_threshold", default=0.12))
    VISION_HEALTHY_VETO: float = Field(
        default_factory=lambda: _yaml_val("vision", "healthy_veto", default=0.25))
    # 许可不明的权重（dima806 ViT、PlantVillage MobileNetV2）默认不加载；
    # 打开后补上 melanose / greening / 白粉病弱标签。
    VISION_ALLOW_UNLICENSED: bool = Field(
        default_factory=lambda: _yaml_val("vision", "allow_unlicensed_models", default=False))
    # 2. Model Endpoints & Fallback Keys
    MAIN_API_KEY: str = Field(default="")
    MAIN_BASE_URL: str = "https://api.deepseek.com"
    MAIN_MODEL: str = Field(default_factory=lambda: _yaml_val("model", "main", default="deepseek-flash"))

    FAST_API_KEY: str = ""
    FAST_BASE_URL: str = ""
    FAST_MODEL: str = Field(default_factory=lambda: _yaml_val("model", "fast", default="deepseek-flash"))

    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_MODEL: str = Field(default_factory=lambda: _yaml_val("model", "embedding", default="intfloat/multilingual-e5-large"))
    RERANKER_MODEL: str = Field(default_factory=lambda: _yaml_val("model", "reranker", default="BAAI/bge-reranker-v2-m3"))

    # 3. Retrieval Parameters (核心检索阈值)
    TOP_K_VECTOR: int = Field(default_factory=lambda: _yaml_val("retrieval", "top_k_vector", default=40))
    TOP_K_BM25: int = Field(default_factory=lambda: _yaml_val("retrieval", "top_k_bm25", default=40))
    TOP_K_FINAL: int = Field(default_factory=lambda: _yaml_val("retrieval", "top_k_final", default=10))
    # v9.4 (P0#1 论文参数化): RRF 融合后精排候选窗口（原 multi_retriever.py _fuse_rerank_select
    # 隐式 top_k_final*2=20 硬编码）。行为不变：config.yaml candidate_window: 20。
    CANDIDATE_WINDOW: int = Field(default_factory=lambda: _yaml_val("retrieval", "candidate_window", default=20))
    CANDIDATE_POOL_SIZE: int = Field(
        default_factory=lambda: _yaml_val("retrieval", "candidate_pool_size", default=12))
    CANDIDATE_POOL_MAX: int = Field(
        default_factory=lambda: _yaml_val("retrieval", "candidate_pool_max", default=40))
    CANDIDATE_POOL_TTL_SEC: int = Field(
        default_factory=lambda: _yaml_val("retrieval", "candidate_pool_ttl_sec", default=1800))
    RRF_K: int = Field(default_factory=lambda: _yaml_val("retrieval", "rrf_k", default=60))
    RERANK_THRESHOLD: float = Field(default_factory=lambda: _yaml_val("retrieval", "rerank_threshold", default=0.25))
    RERANK_BATCH_SIZE: int = Field(
        default_factory=lambda: _yaml_val("retrieval", "rerank_batch_size", default=4), ge=1, le=32)
    MAX_REACT_STEPS: int = Field(default_factory=lambda: _yaml_val("react", "max_steps", default=5))
    DYNAMIC_THRESHOLD_RATIO: float = Field(default_factory=lambda: _yaml_val("retrieval", "dynamic_threshold_ratio", default=0.60))
    # v9.4 (P0#2 论文参数化): 代码级收敛早停参数（原 agent_runner.py:593 硬编码 6 / 0.25）
    EARLY_STOP_MIN_EVIDENCE: int = Field(default_factory=lambda: _yaml_val("retrieval", "early_stop_min_evidence", default=6))
    EARLY_STOP_NEW_RATIO: float = Field(default_factory=lambda: _yaml_val("retrieval", "early_stop_new_ratio", default=0.25))
    RAG_HYDE_ENABLED: bool = Field(default_factory=lambda: _yaml_val("retrieval", "rag_hyde_enabled", default=True))
    # v9.4 (P0#5 论文参数化): 多粒度查询消融模式——full/raw/hyde_only/mq_only/sum_only/hyde_mq/hyde_sum。
    # 默认 full 与旧行为一致（HyDE+MQ+SUM 全路）；消融变体供论文实验2扫描。
    # v9.4b（2026-08-26，论文实证回写）：生产默认改 raw——exp1/1b/1c/2 四重互证
    # full 多路（MRR 0.256）远劣于原始查询单路（0.557 生产实测/0.653 对齐上界），
    # 与 config.yaml:29 保持一致（该字段始终被 YAML 覆盖，此处为无 YAML 时兜底）。
    QUERY_MODE: str = Field(default_factory=lambda: _yaml_val("retrieval", "query_mode", default="raw"))
    # v8.4.3 指令A: RETRIEVE_CONVERGE_MIN_DOCS 已移除（动态阈值已过滤 chunk，
    # 全部证据应进入报告，代码级收敛不再需要）
    HYDE_MAX_TOKENS: int = Field(default_factory=lambda: _yaml_val("retrieval", "hyde_max_tokens", default=512))
    # v8.16.3c: HyDE 结构化生成关闭思维链——实测 22:02:33/44 连续两次真空输出
    # （content 为空，"第 N 次返回为空"日志实证=成功调用但内容空，非超时/解析），
    # 与 draft call 同一根因：v4-flash 思维链吃光预算。thinking:disabled 为 DeepSeek
    # chat 端点厂商参数（v8.15.3 同款通道）；不兼容时抛异常→现有 fail-soft 回退基础检索
    HYDE_THINKING_OFF: bool = Field(default_factory=lambda: _yaml_val("retrieval", "hyde_thinking_off", default=True))
    RRF_WEIGHT_ORIG_DENSE: float = Field(default_factory=lambda: _yaml_val("retrieval", "rrf_weights", "orig_dense", default=1.0))
    RRF_WEIGHT_HYDE_DENSE: float = Field(default_factory=lambda: _yaml_val("retrieval", "rrf_weights", "hyde_dense", default=1.0))
    RRF_WEIGHT_BM25: float = Field(default_factory=lambda: _yaml_val("retrieval", "rrf_weights", "bm25", default=1.0))

    # v8.15: 查询级结果缓存（citrus_rag_search 全链路；键=规范化query+HyDE开关+语料指纹）
    RAG_CACHE_ENABLED: bool = Field(default_factory=lambda: _yaml_val("retrieval", "rag_cache_enabled", default=True))
    RAG_CACHE_SIZE: int = Field(default_factory=lambda: _yaml_val("retrieval", "rag_cache_size", default=300))
    RAG_CACHE_TTL_HOURS: int = Field(default_factory=lambda: _yaml_val("retrieval", "rag_cache_ttl_hours", default=24))

    # Search transport and credentials are selected in administrator runtime
    # configuration; this YAML value controls only the HTTP timeout budget.
    WEB_SEARCH_TIMEOUT: int = Field(default_factory=lambda: _yaml_val("web_search", "timeout_sec", default=90))

    # 4. Chat Parameters (centralized)
    TEMPERATURE_MAIN: float = Field(default_factory=lambda: _yaml_val("chat", "temperature_main", default=0.2))
    TEMPERATURE_FAST: float = Field(default_factory=lambda: _yaml_val("chat", "temperature_fast", default=0.0))
    MAX_TOKENS: int = Field(default_factory=lambda: _yaml_val("chat", "max_tokens", default=4096))
    # Provider and model limits are operator-supplied capabilities. Older
    # deployments without a model entry use chat.max_tokens as the fallback.
    MAIN_MODEL_BUDGETS: dict[str, dict[str, dict[str, int]]] = Field(default_factory=lambda: _yaml_val(
        "model", "provider_budgets", default={}))
    # Application safety ceiling, including after recovery.
    AGENT_MAX_OUTPUT_TOKENS: int = Field(default_factory=lambda: _yaml_val(
        "agent", "max_output_tokens", default=16384), ge=1)
    # Cumulative per-request use is separate from the single-call context window.
    # Include translation/retries/synthesis; keep unknown failed usage charged.
    AGENT_MAX_MODEL_CALLS: int = Field(default_factory=lambda: _yaml_val(
        "agent", "max_model_calls", default=64), ge=1)
    # A full user turn retains its evidence across many model calls. The former
    # 250k aggregate cap admitted only about four 40-50k scientific work steps
    # plus final synthesis. This is a separate configurable cost ceiling, not
    # the model context window; explicit administrator values remain binding.
    AGENT_MAX_MODEL_TOKENS: int = Field(default_factory=lambda: _yaml_val(
        "agent", "max_model_tokens", default=1000000), ge=1)
    # Native `task` subagent: explicit, file-only, metered on the same job ledger.
    AGENT_ENABLE_TASK_SUBAGENT: bool = Field(default_factory=lambda: _yaml_val(
        "agent", "enable_task_subagent", default=True))
    # Auxiliary model calls use the configured provider's supported reasoning-off
    # payload. The main conversation request does not inherit this setting.
    MODEL_REASONING_OFF_BODY: dict = Field(default_factory=lambda: _yaml_val(
        "model", "reasoning_off_body", default={"thinking": {"type": "disabled"}}))
    # v8.4 清理: RECENT_CONTENT_MAX_CHARS / COMPACT_MAX_TOKENS / FALLBACK_CONTENT_MAX_CHARS
    # 死配置已删（对应旧 graph.py check_history/compact_history 节点，代码已无引用；
    # 压缩统一走 context_budget 段的 ContextBudgetConfig）

    # Completed-history economics and active-request admission are separate.
    # This configured ceiling is not a claim about the provider's actual window.
    CONTEXT_ACTIVE_MAX_INPUT_TOKENS: int = Field(default_factory=lambda: _yaml_val(
        "context_budget", "active_max_input_tokens", default=128000), ge=1024)
    CONTEXT_CHECKPOINT_MAX_TOKENS: int = Field(default_factory=lambda: _yaml_val(
        "context_budget", "checkpoint_max_tokens", default=48000), ge=1024)
    CONTEXT_WORKING_MAX_INPUT_TOKENS: int = Field(default_factory=lambda: _yaml_val("context_budget", "working_max_input_tokens", default=64000), ge=1024)
    CONTEXT_WORKING_SOFT_TOKENS: int = Field(default_factory=lambda: _yaml_val("context_budget", "working_soft_tokens", default=48000), ge=512)
    CONTEXT_WORKING_TARGET_TOKENS: int = Field(default_factory=lambda: _yaml_val("context_budget", "working_target_tokens", default=32000), ge=256)
    CONTEXT_SAFETY_MARGIN_TOKENS: int = Field(default_factory=lambda: _yaml_val("context_budget", "safety_margin_tokens", default=1024), ge=0)
    AGENT_FINAL_OUTPUT_TOKENS: int = Field(default=2048, ge=128)
    AGENT_REFLECTION_OUTPUT_TOKENS: int = Field(default=512, ge=64)
    EXTERNAL_DATA_NETWORK: bool = Field(default_factory=lambda: _yaml_val("capabilities", "external_data_network", default=True))
    LOCAL_RAG: bool = Field(default_factory=lambda: _yaml_val("capabilities", "local_rag", default=True))
    WEB_SEARCH: bool = Field(default_factory=lambda: _yaml_val("capabilities", "web_search", default=True))
    WEB_FETCH: bool = Field(default_factory=lambda: _yaml_val("capabilities", "web_fetch", default=True))
    BROWSER: bool = Field(default_factory=lambda: _yaml_val("capabilities", "browser", default=True))
    BIBLIOGRAPHIC_LOOKUP: bool = Field(default_factory=lambda: _yaml_val("capabilities", "bibliographic_lookup", default=True))
    ENABLED_SKILLS: list[str] | None = Field(default_factory=lambda: _yaml_val("capabilities", "enabled_skills"))
    SUMMARIZATION: bool = Field(default_factory=lambda: _yaml_val("capabilities", "summarization", default=True))
    CAPABILITY_EXPERIMENT_GROUP: str = Field(default_factory=lambda: _yaml_val("capabilities", "experiment_group", default=""))
    CAPABILITY_REVOCATIONS_FILE: str = Field(default_factory=lambda: _yaml_val("capabilities", "revocations_file", default=""))

    CONTEXT_BUDGET_ENABLED: bool = Field(default_factory=lambda: _yaml_val("context_budget", "enabled", default=True))
    CONTEXT_BUDGET_MAX_TOKENS: int = Field(default_factory=lambda: _yaml_val("context_budget", "max_tokens", default=1000000))
    CONTEXT_BUDGET_SOFT_THRESHOLD: float = Field(default_factory=lambda: _yaml_val("context_budget", "soft_threshold", default=0.75))
    CONTEXT_BUDGET_HARD_THRESHOLD: float = Field(default_factory=lambda: _yaml_val("context_budget", "hard_threshold", default=0.93))
    CONTEXT_BUDGET_TARGET_RATIO: float = Field(default_factory=lambda: _yaml_val("context_budget", "target_ratio", default=0.50))
    CONTEXT_BUDGET_PROTECT_RECENT_TURNS: int = Field(default_factory=lambda: _yaml_val("context_budget", "protect_recent_turns", default=3))

    # ── Permission (v8.4.3 结构化权限确认) ──
    PERMISSION_MODE: str = Field(default_factory=lambda: _yaml_val("permission", "mode", default="auto_workspace"))
    # v8.4.5: ask 模式授权等待秒数（前端卡片未回应时超时按拒绝处理）
    PERMISSION_WAIT_SEC: int = Field(default_factory=lambda: _yaml_val("permission", "wait_sec", default=90))
    # A workspace-scoped grant is still session-bound by default. Operators may
    # explicitly opt into cross-session reuse; the agent never assumes it.
    PERMISSION_WORKSPACE_CROSS_SESSION: bool = Field(
        default_factory=lambda: _yaml_val("permission", "workspace_cross_session",
                                          default=False))

        # ── Version (v8.4.5: 版本单源——UI/健康检查/文档以此为准) ──
    # v9.4.4: 应用版本与引用编号池修复（renumber 死号清除/统一编号池/前端 remap
    # 双重应用/缓存防护）合并升版；侧栏 ref_pool 徽标由本字段派生，单源不漂移
    # v9.5.0: 提示词增强（结论一致性自检/来源可信度分级/时效触发/意图感知输出组织），
    # 仅改 source/ 与固定构建产物，不改检索与编号逻辑
    # v9.6.3: ①联网单次调用的输出预算可配置（web_search.max_tokens，默认 8000；
    #   原硬编码 2000 会被内部检索+思考吃光 → stop_reason=max_tokens → 只拿到引用
    #   条目、拿不到综合正文）；②每轮"按正文过滤 + 重编号"后的 cited_refs 落盘，
    #   /citations 刷新路径改读它并去掉 LIMIT 10，侧栏与正文标号一致。
    VERSION: str = "1.0"

    # ── Context Engineering (阶段1: 静态前缀灰度开关) ──
    # true = SystemMessage 字节级稳定（format 指南/策略卡片/skills 移出前缀，
    #        追加到当前轮 HumanMessage 尾部），利于 DeepSeek 上下文缓存命中；
    # false = 旧行为（动态内容进 SystemMessage，前缀每次请求重算）

    # 5. Tools Parameters
    # v8.3.3: MAX_TOOL_CALLS 死配置已删除（无引用）

    # 6. Graph Parameters
    RECURSION_LIMIT: int = Field(default_factory=lambda: _yaml_val("graph", "recursion_limit", default=25))

    # 7. API Timeouts & Limits
    QDRANT_TIMEOUT: int = Field(default_factory=lambda: _yaml_val("api", "qdrant_timeout", default=60))

    # v8.9 向量检索后端（auto | qdrant | lancedb）：lancedb 嵌入式百万级+热更新+无锁；
    # auto = 检测 data/lancedb 有表则用 lancedb，否则回退 qdrant（新旧数据包开箱即用）
    RETRIEVAL_BACKEND: str = Field(default_factory=lambda: _yaml_val("retrieval", "backend", default="auto"))

    # 8. System Paths & Logging
    DATA_DIR: Path = Field(default_factory=lambda: PROJECT_ROOT / "data")
    LOG_DIR: str = Field(default_factory=lambda: _yaml_val("logging", "dir", default="logs"))
    LOG_LEVEL: str = Field(default_factory=lambda: _yaml_val("logging", "level", default="INFO"))
    EMBEDDER_FORCE_CPU: bool = Field(default_factory=lambda: _yaml_val("embedder", "force_cpu", default=False))

    # ── Single Agent ──
    WORKSPACE_DIR: str = Field(default_factory=lambda: _yaml_val("agent", "workspace_dir", default="workspace"))
    TOOL_EXEC_TIMEOUT_SEC: int = Field(default_factory=lambda: _yaml_val("agent", "tool_exec_timeout_sec", default=60))
    # Single-agent request deadline. The model chooses how many tool turns to use;
    # this wall-clock deadline is the only loop-level stop condition.
    AGENT_REQUEST_TIMEOUT_SEC: int = Field(
        default_factory=lambda: _yaml_val("agent", "request_timeout_sec", default=180))
    # Per-call transport timeout for the model client. It is deliberately not
    # derived from AGENT_REQUEST_TIMEOUT_SEC: a task with no wall-clock ceiling
    # (request_timeout_sec: 0) still needs a concrete connect/read timeout, and
    # clamping the disabled task budget into the client used to turn "no
    # deadline" into the shortest possible one.
    AGENT_MODEL_TIMEOUT_SEC: int = Field(
        default_factory=lambda: _yaml_val("agent", "model_timeout_sec", default=180))
    # Navigation disconnects the observer, not the task. Its existing execution
    # budgets, deadline and explicit cancellation still apply. Administrators may
    # opt into a finite observer grace; 0 disables this additional cancellation.
    AGENT_DETACH_GRACE_SEC: int = Field(
        default_factory=lambda: _yaml_val("agent", "detach_grace_sec", default=0))
    SEARCH_FETCH_TIMEOUT_SEC: int = Field(
        default_factory=lambda: _yaml_val("agent", "search_fetch_timeout_sec", default=45))
    # v8.15.3b: 单工具超时覆盖（{tool_name: 秒}，覆盖 TOOL_EXEC_TIMEOUT_SEC 默认值）
    TOOL_TIMEOUTS: dict = Field(default_factory=lambda: _yaml_val("agent", "tool_timeouts", default={}))

    # ── Write Pipeline (v8.3.2) ──
    PIPELINE_MATERIAL_MIN_COUNT: int = Field(default_factory=lambda: _yaml_val("pipeline", "material_min_count", default=8))
    PIPELINE_MAX_PLAN_RETRIES: int = Field(default_factory=lambda: _yaml_val("pipeline", "max_plan_retries", default=1))
    PIPELINE_REFS_COVERAGE_RATIO: float = Field(default_factory=lambda: _yaml_val("pipeline", "refs_coverage_ratio", default=0.4))
    PIPELINE_SECTION_MAX_TOKENS: int = Field(default_factory=lambda: _yaml_val("pipeline", "section_max_tokens", default=4000))
    PIPELINE_SECTION_TIMEOUT: int = Field(default_factory=lambda: _yaml_val("pipeline", "section_timeout", default=120))
    PIPELINE_RESUME_ENABLED: bool = Field(default_factory=lambda: _yaml_val("pipeline", "resume_enabled", default=True))
    # v8.4.6: 单章生成并发度（生成并发、按序写盘；1=串行，测试环境用 1 保证确定性）
    PIPELINE_PARALLEL_SECTIONS: int = Field(default_factory=lambda: _yaml_val("pipeline", "parallel_sections", default=3))

    # ── File I/O ──
    FILE_READ_MAX_SIZE_MB: int = Field(default_factory=lambda: _yaml_val("file_io", "read_max_size_mb", default=50))
    FILE_WRITE_MAX_SIZE_MB: int = Field(default_factory=lambda: _yaml_val("file_io", "write_max_size_mb", default=10))
    # v8.3.3: 绝对路径读取仅限项目根 + 此额外根目录列表（默认空 = 严格工作区模式）
    FILE_READ_EXTRA_ROOTS: list = Field(default_factory=lambda: _yaml_val("file_io", "read_extra_roots", default=[]))

    # ── Statistics ──
    STATS_ALPHA: float = Field(default_factory=lambda: _yaml_val("statistics", "alpha", default=0.05))
    STATS_MAX_SAMPLE_SIZE: int = Field(default_factory=lambda: _yaml_val("statistics", "max_sample_size", default=100000))

    # Task scripts expire independently of permanent, code-owned presets.
    RESEARCH_ROUTE_NOTICE_ENABLED: bool = False
    RESEARCH_SCRIPT_IDLE_DAYS: int = Field(default=7, ge=1, le=365)

    # ── Figures（科研绘图与图内微调）──
    # FIGURES_ENABLED 关闭时，图件工具不出现在模型可见列表里。
    FIGURES_ENABLED: bool = Field(default_factory=lambda: _yaml_val("figures", "enabled", default=True))
    # 沙箱后端：off / docker / apptainer / local。
    #   off（默认）—— 声明式出图可用；执行自定义脚本的路径整体关闭。
    #   docker      —— 自定义脚本在一次性容器里跑。
    #   apptainer   —— 固定 SIF + systemd 用户 cgroup；隔离与资源探测失败即关闭。
    #   local       —— 仅供本机开发：不是安全边界，脚本仍能访问本机。
    FIGURES_SANDBOX_BACKEND: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_backend", default="off"))
    FIGURES_SANDBOX_WSL_DISTRO: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_wsl_distro", default="Ubuntu-24.04"))
    FIGURES_SANDBOX_IMAGE: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_image", default="citrusqa/sandbox:1.0.0"))
    # Apptainer 使用本机绝对 .sif 路径与必填 SHA-256；镜像不可被组/其他用户写入。
    FIGURES_SANDBOX_IMAGE_SHA256: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_image_sha256", default=""))
    # Docker: gVisor 运行时名；Apptainer: 绝对可执行文件路径（默认 /usr/bin/apptainer）。
    FIGURES_SANDBOX_RUNTIME: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_runtime", default=""))
    FIGURES_SANDBOX_TIMEOUT_SEC: int = Field(default_factory=lambda: _yaml_val("figures", "sandbox_timeout_sec", default=120))
    FIGURES_SANDBOX_MEMORY_MB: int = Field(default_factory=lambda: _yaml_val("figures", "sandbox_memory_mb", default=1024))
    FIGURES_SANDBOX_CPUS: str = Field(default_factory=lambda: _yaml_val("figures", "sandbox_cpus", default="1.0"))
    # 仅当 ENVIRONMENT=development 时，local 后端才可能被显式启用。
    FIGURES_SANDBOX_ALLOW_LOCAL: bool = Field(default_factory=lambda: _yaml_val("figures", "sandbox_allow_local", default=False))
    # 出图 DPI 与图幅上限（对标投稿规范：单栏 ~85mm，双栏 ~180mm）。
    FIGURES_DPI: int = Field(default_factory=lambda: _yaml_val("figures", "dpi", default=300))
    FIGURES_MAX_SCRIPT_CHARS: int = Field(default_factory=lambda: _yaml_val("figures", "max_script_chars", default=200000))
    # Zero inherits the task model-call limit; host deadline/cancellation still apply.
    RESEARCH_SCRIPT_MAX_FAILURES: int = Field(default_factory=lambda: _yaml_val(
        "figures", "script_max_failures", default=0), ge=0, le=1024)

    # ── Databases (可插拔，从 databases: 段读取) ──
    @property
    def DATABASES(self) -> dict:
        return _yaml_val("databases", default={})

    # v8.4.4: BIO_DB_CACHE_TTL 死配置已删（无消费点）
    NCBI_ENTREZ_EMAIL: str = Field(default_factory=lambda: _yaml_val("bio_db", "entrez_email", default="citrus-agent@localhost"))
    NCBI_ENTREZ_TOOL: str = Field(default_factory=lambda: _yaml_val("bio_db", "entrez_tool", default="CitrusQAAgent"))
    KEGG_API_URL: str = Field(default_factory=lambda: _yaml_val("bio_db", "kegg_api_url", default="https://rest.kegg.jp"))
    UNIPROT_API_URL: str = Field(default_factory=lambda: _yaml_val("bio_db", "uniprot_api_url", default="https://rest.uniprot.org"))
    STRING_API_URL: str = Field(default_factory=lambda: _yaml_val("bio_db", "string_api_url", default="https://string-db.org/api"))

    # ── Baidu Unlimited-OCR ──
    BAIDU_OCR_API_KEY: str = Field(default_factory=lambda: _yaml_val("baidu_ocr", "api_key", default=""))
    BAIDU_OCR_SECRET_KEY: str = Field(default_factory=lambda: _yaml_val("baidu_ocr", "secret_key", default=""))
    BAIDU_OCR_URL: str = Field(default_factory=lambda: _yaml_val("baidu_ocr", "url", default="https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser"))

    # ── LaTeX ──
    LATEX_OUTPUT_DIR: str = Field(default_factory=lambda: _yaml_val("latex", "output_dir", default="workspace/output"))

    # v8.4.4: HITL_* 死配置已删（stub 端点无消费点）；权限由 permission 体系承担

    # ── Sandbox ──
    SANDBOX_ENABLED: bool = Field(default_factory=lambda: _yaml_val("sandbox", "enabled", default=True))
    SANDBOX_ALLOWED_PATTERNS: list = Field(default_factory=lambda: _yaml_val("sandbox", "allowed_patterns", default=["read_*", "search_*", "list_*"]))
    SANDBOX_DANGEROUS_PATTERNS: list = Field(default_factory=lambda: _yaml_val("sandbox", "dangerous_patterns", default=["delete_*", "exec_*", "write_*"]))

    # v8.5.0 开源版：运行时 API Key（WebUI 引导填写）——优先于 .env，
    # 持久化到 state/api_key（gitignore 内，不入仓库）；.env 保持不动
    _runtime_api_key: str = ""
    _runtime_provider_api_keys: Dict[str, str] = {}
    _readonly_model_keys: dict[tuple[str, str], str] = {}
    _runtime_configured_model_providers: set[str] = set()
    # DeepSeek Search/Fetch has a separate runtime credential from the main model.
    _runtime_deepseek_web_api_key: str = ""
    _runtime_zhipu_web_api_key: str = ""
    _runtime_web_search_cfg: Dict[str, str] = {}

    def _load_runtime_api_key(self) -> None:
        """The historical generic key has no inherent provider/origin binding."""
        if not model_origin(os.getenv("CITRUS_LEGACY_MODEL_API_KEY_ORIGIN", "")):
            return
        try:
            p = PROJECT_ROOT / "state" / "api_key"
            if p.exists():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    self._runtime_api_key = v
        except Exception:
            pass

    def _load_runtime_provider_api_keys(self) -> None:
        try:
            p = PROJECT_ROOT / "state" / "provider_api_keys.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._runtime_provider_api_keys = {
                        str(k): str(v).strip() for k, v in data.items()
                        if re.fullmatch(r"[a-z][a-z0-9_]{1,40}", str(k)) and str(v).strip()
                    }
        except Exception:
            self._runtime_provider_api_keys = {}
    def _load_runtime_zhipu_web_api_key(self) -> None:
        try:
            path = PROJECT_ROOT / "state" / "zhipu_web_api_key.json"
            self._runtime_zhipu_web_api_key = json.loads(path.read_text(encoding="utf-8"))["key"].strip()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def save_runtime_zhipu_web_api_key(self, key: str) -> bool:
        try:
            _atomic_private_json(PROJECT_ROOT / "state" / "zhipu_web_api_key.json", {"key": key})
            self._runtime_zhipu_web_api_key = key
            return True
        except OSError:
            return False

    def _load_runtime_configured_model_providers(self) -> None:
        """Load provider IDs configured for the main model (not search credentials)."""
        try:
            p = PROJECT_ROOT / "state" / "configured_model_providers.json"
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
            if isinstance(data, list):
                self._runtime_configured_model_providers = {
                    str(item) for item in data
                    if re.fullmatch(r"[a-z][a-z0-9_]{1,40}", str(item))
                }
        except Exception:
            self._runtime_configured_model_providers = set()
        # Provider keys saved by earlier versions remain configured. Zhipu also
        # stores Web Search credentials under the same provider ID, so only
        # infer it from the active main-model configuration.
        self._runtime_configured_model_providers.update(
            provider_id for provider_id in self._runtime_provider_api_keys
            if provider_id != "zhipu"
        )
    @property
    def configured_model_providers(self) -> set[str]:
        store = self._runtime_model_activation
        configured = self._runtime_configured_model_providers | set(store.get("keys", {}))
        if self.model_transport().api_key:
            configured.add(self.RESOLVED_PROVIDER_ID)
        return configured

    @property
    def RESOLVED_PROVIDER_ID(self) -> str:
        return self._current_model_config().get("provider_id") or "deepseek"

    def _provider_headers(self, provider_id: str, base_url: str) -> Dict[str, str]:
        if provider_id != "opencode_go" or model_origin(base_url) != MODEL_PROVIDER_ORIGINS[provider_id]:
            return {}
        env_session = os.getenv("CITRUS_OPENCODE_SESSION", "").strip()
        if env_session:
            return {"x-opencode-session": env_session}
        try:
            p = PROJECT_ROOT / "state" / "opencode_session_id"
            if p.exists():
                session_id = p.read_text(encoding="utf-8").strip()
            else:
                session_id = str(uuid.uuid4())
                p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with p.open("x", encoding="utf-8") as stream:
                        stream.write(session_id)
                except FileExistsError:
                    session_id = p.read_text(encoding="utf-8").strip()
            return {"x-opencode-session": session_id or "citrusqa-local"}
        except OSError:
            return {"x-opencode-session": "citrusqa-local"}

    @property
    def RESOLVED_PROVIDER_HEADERS(self) -> Dict[str, str]:
        return self.model_transport().headers

    def _load_runtime_deepseek_web_api_key(self) -> None:
        """Load the separately configured Search/Fetch key from local state."""
        try:
            p = PROJECT_ROOT / "state" / "deepseek_web_api_key"
            if p.exists():
                value = p.read_text(encoding="utf-8").strip()
                if value:
                    self._runtime_deepseek_web_api_key = value
        except Exception:
            pass

    def save_runtime_deepseek_web_api_key(self, api_key: str) -> bool:
        """Persist a WebUI-provided Search/Fetch key outside tracked config."""
        try:
            p = PROJECT_ROOT / "state" / "deepseek_web_api_key"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(api_key.strip(), encoding="utf-8")
            try:
                import os
                os.chmod(p, 0o600)
            except Exception:
                pass
            self._runtime_deepseek_web_api_key = api_key.strip()
            return True
        except Exception:
            return False

    def _load_runtime_web_search_config(self) -> None:
        try:
            p = PROJECT_ROOT / "state" / "web_search_config.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                provider = str(data.get("provider_id", "")).strip()
                if provider in {"deepseek_search_fetch", "zhipu_web_search"}:
                    self._runtime_web_search_cfg = {"provider_id": provider}
        except Exception:
            self._runtime_web_search_cfg = {}

    def save_runtime_web_search_provider(self, provider_id: str) -> bool:
        if provider_id not in {"deepseek_search_fetch", "zhipu_web_search"}:
            return False
        try:
            p = PROJECT_ROOT / "state" / "web_search_config.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"provider_id": provider_id}, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
            self._runtime_web_search_cfg = {"provider_id": provider_id}
            return True
        except Exception:
            return False

    # ── v8.14.1 运行时模型覆盖（前端设置面板切换底座模型）──
    # state/model_config.json（gitignore 内）存 {main_model,fast_model,main_base_url,fast_base_url}；
    # 空字段 = 回退 yaml/env 默认。优先级: 运行时覆盖 > 环境变量 > YAML > 默认值。
    _runtime_model_cfg: Dict[str, str] = {}

    def _load_runtime_model_config(self) -> None:
        try:
            p = PROJECT_ROOT / "state" / "model_config.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                self._runtime_model_cfg = {
                    k: str(v).strip() for k, v in data.items()
                    if k in ("main_model", "fast_model", "main_base_url", "fast_base_url",
                             "context_window_tokens", "provider_id")
                    and str(v).strip()
                }
        except Exception:
            self._runtime_model_cfg = {}

    # A separate writable record holds both the selection and any operator-typed
    # keys. Read-only deployment dotenv/legacy sources remain source-only.
    _runtime_model_activation: dict = {}

    def _load_runtime_model_activation(self) -> None:
        try:
            self._runtime_model_activation = self._read_model_activation()
        except (OSError, ValueError):
            # Do not silently revive a legacy credential after a corrupt activation.
            logging.getLogger(__name__).error("Could not load runtime model activation")
            self._runtime_model_activation = {"invalid": True}

    @staticmethod
    def _read_model_activation(root: Path | None = None) -> dict:
        try:
            path = (root if root is not None else PROJECT_ROOT) / "state" / "model_activation.json"
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (not isinstance(data, dict) or not isinstance(data.get("revision"), str)
                or not isinstance(data.get("model"), dict) or not isinstance(data.get("keys"), dict)
                or any(not isinstance(v, str) for v in data["model"].values())
                or any(not isinstance(keys, dict) or any(not isinstance(k, str) for k in keys.values())
                       for keys in data["keys"].values())):
            raise ValueError("Invalid model activation record")
        return data

    @property
    def model_activation_revision(self) -> str:
        return self._runtime_model_activation.get("revision", "")

    def activate_model(self, *, revision: str, provider_id: str, main: str, main_base_url: str,
                       fast: str = "", fast_base_url: str = "", context_window_tokens: int = 0,
                       new_key: str = "") -> bool:
        """Commit a preflighted configuration; false means the preflight snapshot went stale."""
        origin = model_origin(main_base_url)
        if not origin or not main or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", provider_id):
            raise ValueError("Invalid model activation")
        path = PROJECT_ROOT / "state" / "model_activation.json"
        with _MODEL_ACTIVATION_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _model_file_lock(path.with_suffix(".lock")):
                old = self._read_model_activation()
                if revision != old.get("revision", "") or revision != self.model_activation_revision:
                    return False
                keys = dict(old.get("keys", {}))
                if new_key:
                    keys[provider_id] = {**keys.get(provider_id, {}), origin: new_key}
                cfg = {"provider_id": provider_id, "main_model": main, "main_base_url": main_base_url,
                       "fast_model": fast, "fast_base_url": fast_base_url,
                       "context_window_tokens": str(context_window_tokens or "")}
                next_state = {"revision": uuid.uuid4().hex, "model": cfg, "keys": keys}
                _atomic_private_json(path, next_state)
                self._runtime_model_activation = next_state
        return True

    def _current_model_config(self, store: dict | None = None) -> dict:
        if store is None:
            store = self._runtime_model_activation
        return store.get("model", {} if store.get("invalid") else self._runtime_model_cfg)

    def _main_base_url(self, cfg: dict) -> str:
        provider_id = cfg.get("provider_id")
        if provider_id == "opencode_go":
            return MODEL_PROVIDER_BASES[provider_id]
        return cfg.get("main_base_url") or MODEL_PROVIDER_BASES.get(provider_id) or self.MAIN_BASE_URL

    def _credential_for(self, provider_id: str, base_url: str, store: dict) -> str:
        origin = model_origin(base_url)
        if not origin or store.get("invalid"):
            return ""
        official = MODEL_PROVIDER_ORIGINS.get(provider_id)
        stored = store.get("keys", {}).get(provider_id, {}).get(origin)
        if stored:
            return stored
        lane_provider = os.getenv("CITRUS_PROVIDER_ID", "").strip() or "opencode_go"
        lane_origin = model_origin(os.getenv("CITRUS_PROVIDER_BASE_URL", "")) or official
        if provider_id == lane_provider and origin == lane_origin:
            key = os.getenv("CITRUS_PROVIDER_API_KEY", "").strip()
            if key:
                return key
        # Provider-only legacy keys can be used only at the provider's known
        # origin. An unbound legacy custom key has no trustworthy provenance.
        if origin == official and (provider_id != "zhipu"
                or provider_id in self._runtime_configured_model_providers
                or self._runtime_model_cfg.get("provider_id") == "zhipu"):
            key = self._runtime_provider_api_keys.get(provider_id)
            if key:
                return key
        bound_source = self._readonly_model_keys.get((provider_id, origin))
        if bound_source:
            return bound_source
        if provider_id == "deepseek":
            if origin == model_origin(self.MAIN_BASE_URL) and self.MAIN_API_KEY:
                return self.MAIN_API_KEY
            if origin == official:
                if self._runtime_api_key and origin == model_origin(os.getenv("CITRUS_LEGACY_MODEL_API_KEY_ORIGIN", "")):
                    return self._runtime_api_key
                return self.DEEPSEEK_API_KEY
        return ""

    def bind_readonly_model_source(self, source: Path) -> None:
        """Read only the active, origin-matched credential from an authorized source."""
        target = self.model_transport(include_headers=False)
        if target.api_key:
            return
        origin = model_origin(target.base_url)
        try:
            store = self._read_model_activation(source)
            cfg = store.get("model")
            if cfg is None:
                cfg = json.loads((source / "state" / "model_config.json").read_text(encoding="utf-8"))
            provider = cfg.get("provider_id")
            configured_base = cfg.get("main_base_url") or MODEL_PROVIDER_BASES.get(provider, "")
            if not origin or provider != target.provider_id or model_origin(configured_base) != origin:
                return
            key = store.get("keys", {}).get(provider, {}).get(origin)
            if not key and origin == MODEL_PROVIDER_ORIGINS.get(provider):
                # Provider-only legacy keys have a subject only at fixed catalog origins.
                keys = json.loads((source / "state" / "provider_api_keys.json").read_text(encoding="utf-8"))
                key = keys.get(provider)
            if isinstance(key, str) and key.strip():
                self._readonly_model_keys = {(provider, origin): key.strip()}
        except (OSError, ValueError, TypeError, AttributeError):
            logging.getLogger(__name__).warning("Could not read an origin-bound model credential from the configured source")

    def model_credential_for(self, provider_id: str, base_url: str) -> str:
        return self._credential_for(provider_id, base_url, self._runtime_model_activation)

    def model_transport(self, *, fast: bool = False, include_headers: bool = True) -> ModelTransport:
        """Capture one generation for the model, endpoint, credential and routing headers."""
        store = self._runtime_model_activation
        cfg = self._current_model_config(store)
        provider_id = cfg.get("provider_id") or "deepseek"
        main_base = self._main_base_url(cfg)
        main_model = cfg.get("main_model") or self.MAIN_MODEL
        key = self._credential_for(provider_id, main_base, store)
        base, model = main_base, main_model
        if fast:
            base = cfg.get("fast_base_url") or (main_base if store.get("revision") else
                                                self.FAST_BASE_URL or main_base)
            model = cfg.get("fast_model") or (main_model if cfg.get("main_model") else self.FAST_MODEL)
            if cfg.get("fast_base_url") or not store.get("revision"):
                key = self.fast_credential_for(base, main_base, key)
        return ModelTransport(provider_id, model, base, key,
                              self._provider_headers(provider_id, base) if include_headers and key else {},
                              self._configured_context_window(cfg))

    def fast_credential_for(self, base: str, main_base: str, main_key: str) -> str:
        origin = model_origin(base)
        if not origin:
            return ""
        if self.FAST_API_KEY and origin == model_origin(self.FAST_BASE_URL or self.MAIN_BASE_URL):
            return self.FAST_API_KEY
        return main_key if origin == model_origin(main_base) else ""

    @property
    def has_model_overrides(self) -> bool:
        return bool(self._current_model_config())

    @property
    def RESOLVED_MAIN_MODEL(self) -> str:
        return self.model_transport(include_headers=False).model

    @property
    def RESOLVED_MAIN_MODEL_IMAGE_INPUT(self) -> Optional[bool]:
        from src.core.model_capabilities import model_supports_images
        model = self.model_transport(include_headers=False)
        return model_supports_images(model.provider_id, model.model)

    def _configured_context_window(self, cfg: dict) -> int:
        value = cfg.get("context_window_tokens")
        try:
            return min(max(int(value), 4096), 1_000_000) if value else self.CONTEXT_BUDGET_MAX_TOKENS
        except (TypeError, ValueError):
            return self.CONTEXT_BUDGET_MAX_TOKENS

    @property
    def RESOLVED_CONTEXT_BUDGET_MAX_TOKENS(self) -> int:
        return self._configured_context_window(self._current_model_config())

    @property
    def RESOLVED_FAST_MODEL(self) -> str:
        return self.model_transport(fast=True, include_headers=False).model

    @property
    def RESOLVED_MAIN_BASE_URL(self) -> str:
        return self.model_transport(include_headers=False).base_url

    @property
    def RESOLVED_MAIN_API_KEY(self) -> str:
        return self.model_transport(include_headers=False).api_key

    @property
    def RESOLVED_DEEPSEEK_WEB_API_KEY(self) -> str:
        """Resolve deployment web credential without coupling it to other Providers."""
        if self._runtime_deepseek_web_api_key:
            return self._runtime_deepseek_web_api_key
        dedicated = (self.DEEPSEEK_WEB_API_KEY or self.DEEPSEEK_API_KEY or "").strip()
        if dedicated:
            return dedicated
        transport = self.model_transport(include_headers=False)
        if transport.provider_id == "deepseek" and model_origin(transport.base_url) == MODEL_PROVIDER_ORIGINS["deepseek"]:
            return transport.api_key
        return ""

    @property
    def RESOLVED_WEB_SEARCH_PROVIDER(self) -> str:
        return (self._runtime_web_search_cfg.get("provider_id")
                or "deepseek_search_fetch")

    @property
    def RESOLVED_ZHIPU_API_KEY(self) -> str:
        dedicated = self._runtime_zhipu_web_api_key or self._runtime_provider_api_keys.get("zhipu", "")
        if dedicated:
            return dedicated
        transport = self.model_transport(include_headers=False)
        if transport.provider_id == "zhipu" and model_origin(transport.base_url) == MODEL_PROVIDER_ORIGINS["zhipu"]:
            return transport.api_key
        return ""

    @property
    def HAS_EXPLICIT_DEEPSEEK_WEB_API_KEY(self) -> bool:
        """Whether Search/Fetch has a dedicated key, rather than a main-key fallback."""
        return bool(self._runtime_deepseek_web_api_key or self.DEEPSEEK_WEB_API_KEY)

    @property
    def RESOLVED_FAST_API_KEY(self) -> str:
        return self.model_transport(fast=True, include_headers=False).api_key

    @property
    def RESOLVED_FAST_BASE_URL(self) -> str:
        return self.model_transport(fast=True, include_headers=False).base_url

def load_runtime_settings(*, worker: bool = False):
    # Worker controllers use storage/authorization configuration, never provider
    # credentials. Skip dotenv and every credential loader before opening files.
    # This is process hygiene, not OS isolation between processes with one UID.
    if worker:
        keys = {name: '' for name in Settings.model_fields
                if name.endswith(('_API_KEY', '_SECRET_KEY'))}
        return Settings(_env_file=None, **keys)
    instance = Settings()
    instance._load_runtime_deepseek_web_api_key()
    instance._load_runtime_api_key()
    instance._load_runtime_provider_api_keys()
    instance._load_runtime_model_config()
    instance._load_runtime_configured_model_providers()
    instance._load_runtime_zhipu_web_api_key()
    instance._load_runtime_model_activation()
    instance._load_runtime_web_search_config()
    return instance


_worker_role = os.environ.get('CITRUS_RUNTIME_ROLE') == 'code-worker'
settings = load_runtime_settings(worker=_worker_role)

# v8.5.0 模型镜像兜底：任何启动方式（run.ps1 / 手动 uvicorn）下，
# HuggingFace 模型（reranker/embedding）首次下载统一走国内镜像，
# 已有 HF_ENDPOINT 环境变量则尊重用户自定义
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# ── 启动配置校验（v8.3.3 fail-fast）──
def validate_config() -> list:
    """关键配置自检，返回问题列表（缺失 API key 等）。"""
    issues = []
    if not settings.RESOLVED_MAIN_API_KEY:
        issues.append("缺少主模型 API Key（.env 的 DEEPSEEK_API_KEY 或 MAIN_API_KEY）")
    if not settings.RESOLVED_FAST_API_KEY:
        issues.append("缺少快速模型 API Key（将回退主 Key）")
    if settings.CONTEXT_BUDGET_MAX_TOKENS <= 0:
        issues.append("CONTEXT_BUDGET_MAX_TOKENS 必须为正数")
    if settings.PIPELINE_SECTION_MAX_TOKENS <= 0:
        issues.append("PIPELINE_SECTION_MAX_TOKENS 必须为正数")
    return issues


_config_issues = [] if _worker_role else validate_config()
if _config_issues:
    _w = logging.getLogger("src.config")
    for _issue in _config_issues:
        _w.warning(f"[Config] 配置问题: {_issue}")

# --- 主模型解析（v8.14.1）---
# 全链路主模型唯一入口 get_deepseek_model()：运行时覆盖（前端设置面板，
# state/model_config.json）优先，未覆盖回退 yaml/env(model.main)。
# 遗留的 switch_model/_available_models 运行时切换已删除（无调用点；
# 切换统一走前端设置面板 POST /api/v2/config/model）。

def get_deepseek_model() -> str:
    return settings.RESOLVED_MAIN_MODEL
