"""Strict JSON boundary for host tool metadata.

``ToolMessage.artifact`` is host metadata, not model content, but it is
checkpointed together with the message. Any value that is not JSON-native makes
the whole checkpoint unreadable and stops the session. Tool functions do return
numpy/pandas scalars, paths, timestamps and similar wrappers, so the artifact is
sanitized once here, before it enters a message.

Only a closed set of scalar wrappers is converted, each losslessly:

* numpy scalars/arrays -> ``.tolist()``
* pandas ``NaT``/``NAType`` -> ``None``; ``Timestamp``/``Timedelta`` -> ISO text
* ``pathlib.PurePath`` -> ``str``
* ``datetime``/``date``/``time`` -> ISO text
* ``uuid.UUID`` -> ``str``
* ``decimal.Decimal`` -> ``str`` (exact decimal digits, never a lossy float)
* ``enum.Enum`` -> its ``.value``

Everything else is *not* silently stringified. ``strict=True`` raises;
``strict=False`` replaces the value with an explicit marker naming its type and
logs a warning, and the qualified type name is recorded so the caller can keep
an honest degradation instead of reporting success.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import enum
import logging
import math
import uuid
from pathlib import PurePath
from typing import Any

logger = logging.getLogger(__name__)

UNSUPPORTED_MARK = "__citrus_unsupported__"

_SCALARS = (str, bool, int)
_KEYS = (str, int, float, bool, bytes, type(None))


def _unsupported(value: Any, *, strict: bool, context: str) -> Any:
    module = (type(value).__module__ or "").split(".")[0]
    name = type(value).__name__
    if strict:
        raise TypeError(f"artifact_value_not_json:{module}.{name}")
    logger.warning("artifact value marked unsupported type=%s.%s context=%s",
                   module, name, context or "-")
    return {UNSUPPORTED_MARK: f"{module}.{name}"}


def sanitize(value: Any, *, strict: bool = False, context: str = "",
             unknown_log: list[str] | None = None) -> Any:
    """Return a JSON-native copy of ``value``.

    Non-finite floats become ``None`` because JSON has no representation for
    them and every downstream encoder rejects them anyway.
    """
    if value is None or type(value) in _SCALARS:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if type(value) in (bytes, bytearray):
        return bytes(value)
    if type(value) is dict:
        converted = {}
        for key, item in value.items():
            if type(key) not in _KEYS:
                key = str(key)
            elif type(key) is bytes:
                key = key.decode("utf-8", "replace")
            converted[key] = sanitize(item, strict=strict, context=context,
                                      unknown_log=unknown_log)
        return converted
    if type(value) in (list, tuple, set, frozenset):
        return [sanitize(item, strict=strict, context=context,
                         unknown_log=unknown_log) for item in value]

    module = (type(value).__module__ or "").split(".")[0]
    name = type(value).__name__
    if module == "numpy":
        tol = getattr(value, "tolist", None)
        if callable(tol):
            converted = tol()
            if converted is not value and type(converted) is not type(value):
                return sanitize(converted, strict=strict, context=context,
                                unknown_log=unknown_log)
    if module == "pandas" and name in ("NAType", "NaTType"):
        return None
    if module == "pandas" and name in ("Timestamp", "Timedelta"):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if type(value) in (_dt.datetime, _dt.date, _dt.time):
        return value.isoformat()
    if type(value) is decimal.Decimal:
        return str(value) if value.is_finite() else None
    if type(value) is uuid.UUID:
        return str(value)
    if isinstance(value, enum.Enum):
        return sanitize(value.value, strict=strict, context=context,
                        unknown_log=unknown_log)
    if unknown_log is not None:
        unknown_log.append(f"{module}.{name}")
    return _unsupported(value, strict=strict, context=context)


def sanitize_artifact(value: Any, *, context: str = "",
                      unknown_log: list[str] | None = None) -> Any:
    """Artifact entry point: never raises on unknown wrappers (they are marked)."""
    return sanitize(value, strict=False, context=context, unknown_log=unknown_log)