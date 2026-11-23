"""Regression: tool artifacts must survive the checkpoint JSON boundary.

The archived production session failed every turn with
``unsupported_public_message_field`` (``structure=message.public``) because a
``ToolMessage.artifact`` carried a value the checkpoint serializer rejects, and
the session could not write a single checkpoint. These tests pin the conversion
of the closed wrapper set and prove a real message round-trips.
"""
from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid
from pathlib import Path

from langchain_core.messages import ToolMessage

from src.agent.checkpoint_store import _PrivateMessages
from src.core.json_boundary import UNSUPPORTED_MARK, sanitize, sanitize_artifact


class _Color(enum.Enum):
    RED = "red"


def test_closed_wrappers_convert_losslessly():
    stamp = dt.datetime(2026, 11, 23, 12, 0, 0)
    day = dt.date(2026, 11, 23)
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    amount = decimal.Decimal("0.10000000000000000001")
    payload = {
        "path": Path("/input/data/book.xlsx"),
        "stamp": stamp,
        "day": day,
        "uid": uid,
        "amount": amount,
        "color": _Color.RED,
        "tags": {"a", "b"},
        "nested": [Path("/tmp/x"), {"n": decimal.Decimal("1.50")}],
    }
    result = sanitize(payload, strict=True)
    assert result["path"] == "/input/data/book.xlsx"
    assert result["stamp"] == stamp.isoformat()
    assert result["day"] == day.isoformat()
    assert result["uid"] == str(uid)
    # Decimal keeps every digit instead of collapsing through a float.
    assert result["amount"] == "0.10000000000000000001"
    assert result["color"] == "red"
    assert sorted(result["tags"]) == ["a", "b"]
    assert result["nested"][0] == "/tmp/x"
    assert result["nested"][1]["n"] == "1.50"


def test_unknown_wrapper_is_marked_not_stringified():
    class Opaque:
        pass

    marked = sanitize_artifact({"value": Opaque()})
    assert isinstance(marked["value"], dict)
    assert marked["value"][UNSUPPORTED_MARK].endswith("Opaque")

    try:
        sanitize({"value": Opaque()}, strict=True)
    except TypeError as exc:
        assert "artifact_value_not_json" in str(exc)
    else:  # pragma: no cover - strict mode must refuse unknown types
        raise AssertionError("strict sanitize accepted an unknown type")


def test_tool_message_artifact_round_trips_through_checkpoint():
    serializer = _PrivateMessages({"owner_user_id": 7, "provider": "p", "model": "m",
                                   "session_id": "s"})
    message = ToolMessage(
        content="done", tool_call_id="call-1", name="run_research_figure_script",
        artifact=sanitize_artifact({
            "script_path": Path("/workspace/figures/plot.py"),
            "created": dt.datetime(2026, 11, 23, 9, 30, 0),
            "run_id": uuid.UUID("abcdefab-1234-5678-1234-567812345678"),
            "stat": decimal.Decimal("3.14159265358979323846"),
            "mode": _Color.RED,
            "objects": {1, 2, 3},
        }))
    payload = serializer.dumps_typed({"messages": [message]})
    restored = serializer.loads_typed(payload)
    restored_message = restored["messages"][0]
    assert isinstance(restored_message, ToolMessage)
    assert restored_message.artifact["script_path"] == "/workspace/figures/plot.py"
    assert restored_message.artifact["stat"] == "3.14159265358979323846"
    assert sorted(restored_message.artifact["objects"]) == [1, 2, 3]


def test_sanitize_keeps_bytes_and_non_finite_floats_bounded():
    result = sanitize({"blob": bytearray(b"ab"), "nan": float("nan"),
                       "inf": float("inf"), "ok": 1.5})
    assert result["blob"] == b"ab"
    assert result["nan"] is None
    assert result["inf"] is None
    assert result["ok"] == 1.5