"""Invocation-local evidence from the actual artifact publisher.

A publication is recorded per file. ``started`` means the publisher began its
write path, ``committed`` means verified bytes reached the artifact registry,
and ``confirmed`` means the registered URL was returned to the caller. A file
that stops between two stages stays at that stage: reconciliation resumes only
the missing stage and never a completed review or plot.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


STAGES = ("started", "committed", "confirmed")


@dataclass
class PublicationReceipt:
    """One file's publication progress. A later stage is never inferred."""

    file_index: int = 1
    filename: str = ""
    started: bool = False
    committed: bool = False
    confirmed: bool = False
    url: str = ""
    sha256: str = ""

    @property
    def status(self):
        if self.confirmed:
            return "confirmed"
        if self.committed:
            return "committed"
        if self.started:
            return "started"
        return "not_started"

    @property
    def next_stage(self):
        """The only work a reconciler still has to do for this file."""
        if self.confirmed:
            return None
        if self.committed:
            return "confirmed"
        if self.started:
            return "committed"
        return "started"

    def as_dict(self):
        return {"file_index": self.file_index, "filename": self.filename, "status": self.status,
                "started": self.started, "committed": self.committed, "confirmed": self.confirmed,
                "url": self.url, "sha256": self.sha256, "next_stage": self.next_stage}


@dataclass
class _PublicationEvidence:
    tool_name: str
    call_id: str
    receipts: list[PublicationReceipt] = field(default_factory=list)
    preflight: bool = False
    outcome: dict | None = None

    @property
    def started(self):
        return sum(1 for receipt in self.receipts if receipt.started)

    @property
    def confirmed(self):
        return [receipt.url for receipt in self.receipts if receipt.confirmed]

    def begin(self, filename="", file_index=None):
        receipt = PublicationReceipt(file_index=len(self.receipts) + 1 if file_index is None else file_index,
                                     filename=str(filename or ""), started=True)
        self.receipts.append(receipt)
        return receipt

    def latest(self):
        """The newest receipt that has not yet confirmed the caller-visible URL."""
        for receipt in reversed(self.receipts):
            if not receipt.confirmed:
                return receipt
        return self.receipts[-1] if self.receipts else None

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


def publication_started(filename="", file_index=None):
    """Publisher entered its write path for one file."""
    if (evidence := _current.get()) is not None:
        return evidence.begin(filename, file_index)
    return None


def publication_committed(*, sha256="", filename="", file_index=None, receipt=None):
    """Verified bytes left the publisher and reached the artifact registry."""
    evidence = _current.get()
    if evidence is None:
        return None
    target = receipt if isinstance(receipt, PublicationReceipt) else evidence.latest()
    if target is None:
        target = evidence.begin(filename, file_index)
    target.committed = True
    if sha256:
        target.sha256 = str(sha256)
    if filename:
        target.filename = str(filename)
    if file_index is not None:
        target.file_index = file_index
    return target


def publication_confirmed(url, *, sha256=""):
    """The registered URL was returned. Never implies a review was rerun."""
    evidence = _current.get()
    if evidence is None:
        return None
    target = next((receipt for receipt in evidence.receipts
                   if receipt.started and not receipt.url), None)
    if target is None:
        target = evidence.begin()
        target.committed = True
    target.url = str(url or "")
    target.confirmed = True
    if sha256:
        target.sha256 = str(sha256)
    return target


def publication_bound(url, *, sha256="", filename="", file_index=None):
    """Publisher-side binding of the exact bytes it registered.

    The registry hook normally pairs ``started``/``confirmed`` itself; this
    attaches the file identity and hash to that receipt. When the registry hook
    did not run in this scope, the publisher still attests all three stages.
    """
    evidence = _current.get()
    if evidence is None:
        return None
    target = next((receipt for receipt in evidence.receipts
                   if receipt.confirmed and receipt.url == str(url or "")), None)
    if target is None:
        target = publication_started(filename, file_index)
        publication_committed(sha256=sha256, filename=filename, file_index=file_index, receipt=target)
        return publication_confirmed(url, sha256=sha256)
    if sha256:
        target.sha256 = str(sha256)
    if filename:
        target.filename = str(filename)
    if file_index is not None:
        target.file_index = file_index
    return target


def before_publication():
    evidence = _current.get()
    return evidence is not None and not evidence.started


def delivery_outcome(status, reason, receipts):
    links = [receipt["url"] for receipt in receipts if isinstance(receipt, dict) and receipt.get("url")]
    evidence = _current.get()
    files = []
    known = False
    if evidence is not None:
        files = [receipt.as_dict() for receipt in evidence.receipts]
        confirmed = {item["url"] for item in files if item["confirmed"]}
        if any(not item["confirmed"] for item in files) or confirmed != set(links):
            # A partly unknown publication stays unknown; a file that appeared
            # is not proof that the registry confirmed it.
            status, reason = "unknown", "write_requires_reconciliation"
        elif status == "failed" and not files:
            status, reason = "not_run", reason
            known = True
        elif status == "partial":
            known = True
    outcome = {"status": status, "reason_code": reason, "artifacts": links}
    if files:
        outcome["files"] = files
        outcome["confirmed_files"] = [item for item in files if item["confirmed"]]
        outcome["unconfirmed_files"] = [item for item in files if not item["confirmed"]]
        outcome["resume_stages"] = [
            {"file_index": item["file_index"], "filename": item["filename"],
             "stage": item["next_stage"], "url": item["url"], "sha256": item["sha256"]}
            for item in files if not item["confirmed"]]
    if known:
        evidence.outcome = outcome.copy()
        evidence.preflight = status == "not_run"
    return outcome


def reconciliation_plan(outcome):
    """After reconciliation, resume only unconfirmed stages.

    Completed reviews, plots and confirmed files are reused; nothing here
    authorizes rerunning an accepted analysis.
    """
    files = (outcome or {}).get("files") or []
    return {"status": (outcome or {}).get("status"),
            "reason_code": (outcome or {}).get("reason_code"),
            "confirmed": [item for item in files if item["confirmed"]],
            "resume": [{"file_index": item["file_index"], "filename": item["filename"],
                        "stage": item["next_stage"], "url": item["url"], "sha256": item["sha256"]}
                       for item in files if not item["confirmed"]],
            "rerun_review": False, "rerun_plot": False}


def resume_publication(plan, *, republish, authorized=False):
    """Re-publish only the files whose confirmation is missing.

    ``republish`` re-registers already written bytes for one resume entry and
    returns the artifact URL; it is supplied by the caller after the required
    human/API authorization. Nothing here runs automatically from a tool result
    (``authorized`` must be passed explicitly), confirmed files are returned
    untouched, and the review/plot stages are not even parameters — an accepted
    analysis can never be rerun through this path. A file whose re-publication
    fails stays unconfirmed instead of becoming success.
    """
    if authorized is not True:
        raise PermissionError("publication_resume_requires_authorization")
    reused, resumed, unconfirmed = [], [], []
    for item in plan.get("confirmed") or ():
        reused.append({**item, "status": "confirmed", "reused": True})
    for item in plan.get("resume") or ():
        entry = {"file_index": item.get("file_index"), "filename": item.get("filename"),
                 "stage": item.get("stage"), "sha256": item.get("sha256")}
        try:
            url = republish(dict(item))
        except (ValueError, LookupError, PermissionError, OSError) as exc:
            unconfirmed.append({**entry, "status": "unconfirmed", "error": type(exc).__name__})
            continue
        if not url:
            unconfirmed.append({**entry, "status": "unconfirmed"})
            continue
        resumed.append({**entry, "url": str(url), "status": "confirmed"})
    complete = not unconfirmed and bool(reused or resumed)
    return {"status": "complete" if complete else "unknown",
            "complete": complete, "reused": reused, "resumed": resumed,
            "unconfirmed": unconfirmed, "rerun_review": False, "rerun_plot": False}
