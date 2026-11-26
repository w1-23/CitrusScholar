"""Bounded graph diagnostics that never serialize exception text or inputs."""
from functools import cache
from importlib.metadata import PackageNotFoundError, version
import re


_LABEL = re.compile(r"[A-Za-z0-9_.<>-]{1,96}\Z")
_ID = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
_NODE_NOTE = re.compile(r"During task with name '([A-Za-z0-9_.-]{1,96})' and id '[0-9a-fA-F-]{8,64}'\Z")


def _bounded(value, pattern=_LABEL):
    return value if isinstance(value, str) and pattern.fullmatch(value) else ""


@cache
def _versions():
    result = {}
    for name in ("langgraph", "deepagents", "langchain"):
        try:
            result[name] = _bounded(version(name))
        except PackageNotFoundError:
            result[name] = "unavailable"
    return result


def graph_failure(error, *, graph, request_id, job_id, checkpoint=""):
    chain = []
    seen = set()
    while error is not None and len(chain) < 5 and id(error) not in seen:
        seen.add(id(error))
        nodes, frames = [], []
        for note in getattr(error, "__notes__", ()):
            match = _NODE_NOTE.fullmatch(note) if isinstance(note, str) else None
            if match:
                nodes.append(match[1])
        tb = error.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            name = _bounded(frame.f_code.co_name)
            module = _bounded(frame.f_globals.get("__name__"))
            if name and module:
                frames.append((module, name, tb.tb_lineno))
            tb = tb.tb_next
        chain.append({"class": _bounded(type(error).__name__), "nodes": nodes[-6:], "frames": frames[-16:]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return {"graph": graph, "schema_version": 1, "versions": _versions(),
            "request_id": _bounded(request_id, _ID), "job_id": _bounded(job_id, _ID),
            "checkpoint": _bounded(checkpoint, _ID), "chain": chain}
