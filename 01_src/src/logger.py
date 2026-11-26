"""
Structured retrieval logger for traceability and manual review.
Writes allowlisted retrieval metrics to timestamped log files.
"""
import logging
import os
import re
import unicodedata
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

from src.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


def _log_dir() -> Path:
    """v8.4.3 工单10: 日志目录可被 CITRUS_LOG_DIR 覆盖（测试独立 sink，防测试帧污染生产日志）。"""
    override = os.environ.get("CITRUS_LOG_DIR", "").strip()
    if override:
        return Path(override)
    return PROJECT_ROOT / settings.LOG_DIR


class RequestIdFilter(logging.Filter):
    """注入 request_id（contextvar 贯穿请求链路，无请求时为 '-'）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from src.core.tracing import get_request_id
            record.request_id = get_request_id()
        except Exception:
            record.request_id = "-"
        return True


_SECRET = re.compile(
    r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:bearer|basic)\s+[\w./+=~-]+|\b(?:sk-|ghp_|github_pat_)[\w-]{8,}"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie|authorization|token)"
    r"[\"']?\s*[=:]\s*(?:(?:bearer|basic)\s+[^\s,;]+|\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    r"|[\w.+-]+://[^\s/@:]+:[^\s/@]+@"
)


def redact_log_text(text: str, *, single_line: bool = True) -> str:
    """Remove credentials and neutralize control characters in a log sink."""
    from src.core.pii_mask import mask_sensitive

    # Obfuscating format controls must not defeat credential recognition.
    safe = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cf", "Cs"})
    safe = mask_sensitive(_SECRET.sub("[REDACTED]", safe))
    keep = "" if single_line else "\n\t"
    return "".join(" " if ch not in keep and unicodedata.category(ch) in {"Cc", "Zl", "Zp"}
                   else ch for ch in safe)


class MaskingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def setup_logging():
    """Configure root logger: INFO+ to file, WARNING+ to console."""
    log_level = getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(log_level)

    # File handler
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        str(log_dir / "agent.log"), when="midnight",
        encoding="utf-8", backupCount=7
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(MaskingFormatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | req=%(request_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler: WARNING+ only
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(MaskingFormatter("%(levelname)s | %(message)s"))

    rid_filter = RequestIdFilter()
    file_handler.addFilter(rid_filter)
    console.addFilter(rid_filter)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # Suppress noisy library logs
    for lib in ("openai", "httpx", "httpcore", "markdown_it", "fastembed",
                "qdrant_client", "urllib3", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    logger.info(f"[Logger] initialized: file={logging.getLevelName(log_level)}, console=WARNING+")


class RetrievalLogger:
    """Record sizes and numeric settings, never query or retrieved content."""

    _METRICS = {"candidate_budget", "chunk_budget", "token_budget", "dedup_dropped",
                "dense_candidates", "bm25_candidates", "rrf_candidates", "translation_used",
                "reranker_fallback", "fetch_elapsed_ms"}

    def __init__(self):
        self._log_dir = _log_dir() / "retrieval"
        self._log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def log_retrieval(self, query: str, reranked: List[Dict], threshold: float,
                      passed: List[Dict], filtered: List[Dict],
                      elapsed: Optional[float] = None, extra: Optional[Dict] = None):
        now = datetime.now()
        fields = [f"[{now.isoformat()}] retrieval", f"query_chars={len(query)}",
                  f"candidates={len(reranked)}", f"threshold={threshold:.4f}",
                  f"passed={len(passed)}", f"filtered={len(filtered)}"]
        if elapsed is not None:
            fields.append(f"elapsed={elapsed:.2f}s")
        for key, value in (extra or {}).items():
            if key in self._METRICS and type(value) in {int, float, bool} and -2**53 < value < 2**53:
                fields.append(f"{key}={value}")
        path = self._log_dir / f"retrieval_{now:%Y-%m-%d}.log"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                               0o600), "a", encoding="utf-8") as handle:
            handle.write(" | ".join(fields) + "\n")


def log_retrieval(query: str, reranked: List[Dict], threshold: float,
                  passed: List[Dict], filtered: List[Dict],
                  elapsed: Optional[float] = None, extra: Optional[Dict] = None):
    try:
        RetrievalLogger().log_retrieval(query, reranked, threshold, passed, filtered, elapsed, extra)
    except (OSError, ValueError, TypeError) as error:
        logger.warning("retrieval_log_failed kind=%s", type(error).__name__)
