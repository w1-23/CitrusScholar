"""Invocation-local evidence from the actual artifact publisher."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _PublicationEvidence:
    tool_name: str
    call_id: str
    started: int = 0
    confirmed: list[str] = field(default_factory=list)
    preflight: bool = False
    outcome: dict | None = None

    def matches(self, message):
        return (self.outcome is not None and message.name == self.tool_name
                and message.tool_call_id == self.call_id
                and (message.artifact or {}).get("_tool_outcome") == self.outcome)


_current = ContextVar("delivery_publication_evidence", default=None)


@contextmanager
def capture_delivery(tool_name, call_id):
    evidence = _PublicationEvidence(tool_name, call_id)
    token = _current.set(evidence)
    try:
        yield evidence
    finally:
        _current.reset(token)


def publication_started():
    if (evidence := _current.get()) is not None:
        evidence.started += 1


def publication_confirmed(url):
    if (evidence := _current.get()) is not None:
        evidence.confirmed.append(url)


def before_publication():
    evidence = _current.get()
    return evidence is not None and not evidence.started


def delivery_outcome(status, reason, receipts):
    links = [receipt["url"] for receipt in receipts]
    evidence = _current.get()
    known = False
    if evidence is not None:
        if evidence.started != len(evidence.confirmed) or set(evidence.confirmed) != set(links):
            status, reason = "unknown", "write_requires_reconciliation"
        elif status == "failed" and not evidence.started:
            status = "not_run"
            known = True
        elif status == "partial":
            known = True
    outcome = {"status": status, "reason_code": reason, "artifacts": links}
    if known:
        evidence.outcome = outcome.copy()
        evidence.preflight = status == "not_run"
    return outcome
