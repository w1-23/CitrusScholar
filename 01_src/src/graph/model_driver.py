"""Shared timeout policy for model transport and request admission."""
from __future__ import annotations
import math
import time
from src.config import settings


def _request_timeout_disabled() -> bool:
    try:
        return float(getattr(settings, "AGENT_REQUEST_TIMEOUT_SEC", 0) or 0) <= 0
    except (TypeError, ValueError):
        return False


def request_deadline_from_settings() -> float:
    """The one place a turn's wall-clock budget is decided.

    The API used to hard-code ``now + 180`` while the graph ignored it through
    the disabled flag. Two sources meant a future config change could silently
    reintroduce a hidden 180s ceiling; both now read this function.
    """
    if _request_timeout_disabled():
        return math.inf
    try:
        seconds = float(settings.AGENT_REQUEST_TIMEOUT_SEC)
    except (TypeError, ValueError):
        seconds = 180.0
    return time.monotonic() + max(1.0, seconds)


def _model_timeout() -> float:
    """Per-call transport timeout for a model client.

    The task deadline and the network timeout answer different questions. A
    task configured with ``request_timeout_sec: 0`` means "no wall-clock
    ceiling", not "abandon every call after five seconds"; the old
    ``min(300, max(5, AGENT_REQUEST_TIMEOUT_SEC))`` clamp turned the disabled
    deadline into the shortest client timeout and produced the connection and
    read timeouts seen in the logs. The two settings are now separate.
    """
    try:
        seconds = float(getattr(settings, "AGENT_MODEL_TIMEOUT_SEC", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        seconds = 180.0
    return max(5.0, min(300.0, seconds))
