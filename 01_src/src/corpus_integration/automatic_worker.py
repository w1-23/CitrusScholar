"""Standalone consumption under live account and network authorization."""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3

from urllib3.exceptions import HTTPError

from citrus_corpus.prepare import PreparationError

from .paper_source import ExcludedPaper, resolve
from .settings import durable_intake_enabled


def account_epoch(owner, *, admin=False):
    # The ordinary auth connection can create/chmod files and initialize WAL.
    # Read only the live account/admission state; do not open normal auth writes.
    from src.auth import DB_PATH

    path = Path(DB_PATH).absolute()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
            row = conn.execute("""SELECT a.epoch,u.role FROM users AS u JOIN account_admission AS a
                                  ON u.user_id=a.owner WHERE u.user_id=? AND u.active=1
                                  AND u.approval_status='approved' AND a.deleting=0 AND a.claim_token IS NULL""",
                               (owner,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and (not admin or row[1] == "admin") else None


class AutomaticWorker:
    def __init__(self, root, *, network_allowed, account=account_epoch):
        self.root = str(root)
        self.network_allowed = network_allowed
        self.account = account

    def enabled(self):
        return (durable_intake_enabled() and self.root == os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
                and self.network_allowed())

    def allowed(self, job):
        from src.core.capability_policy import current_policy

        actor = re.fullmatch(r"account-([1-9][0-9]{0,17})", job["actor"])
        if not actor or not self.enabled():
            return False
        policy = current_policy()  # system policy, not a fabricated Web tracing scope
        request = job["request"]
        epoch = request.get("epoch")
        return (type(epoch) is int and epoch > 0 and policy.allows("web_fetch")
                and policy.allows("bibliographic_lookup")
                and self.account(int(actor[1]), admin=request["producer"] == "admin") == epoch)

    def resolve(self, job, *, raw, reuse, allowed):
        request = job["request"]
        cached = None
        title = request["title"]
        if raw is not None:
            title = ""
            cached = {"raw": raw, "license_id": request["license"], "evidence_url": request["evidence_url"],
                      "text_url": request["text_url"], "proof_title": request.get("proof_title") or request["title"]}
        try:
            value = resolve(request["doi"], request["pmid"], pmcid=request["pmcid"], title=title,
                            cached=cached, reuse=reuse, allowed=allowed, strict=True)
        except ExcludedPaper as exc:
            raise PreparationError(str(exc)) from exc
        except (OSError, HTTPError) as exc:
            if not allowed():
                raise PreparationError("network_scope_denied") from exc
            code = "network_timeout" if isinstance(exc, TimeoutError) else "network_unavailable"
            raise PreparationError(code) from exc
        if value is None:
            raise PreparationError("identity_or_article_unverified")
        return value
