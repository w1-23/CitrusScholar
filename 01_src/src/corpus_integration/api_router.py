"""Admin intake receipts for the dedicated corpus queue."""
import os
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.auth import require_admin

from .settings import durable_intake_enabled, legacy_enabled

router = APIRouter(prefix="/api/v2/isolated-corpus/admin", tags=["isolated-corpus"])
_PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class IntakeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    doi: str = Field(default="", max_length=512)
    url: str = Field(default="", max_length=2048)
    saved_path: str = Field(default="", max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    title: str = Field(default="", max_length=512)
    revision: str = Field(default="", max_length=128)


def _authorized(request):
    user = getattr(request.state, "user", None) or {}
    if not user.get("active") or user.get("approval_status") != "approved" or not require_admin(user):
        raise HTTPException(403, "管理员权限不足。")
    return f"account-{int(user['user_id'])}"


def _intake():
    from citrus_corpus.intake import Intake
    from citrus_corpus.store import Store

    root = os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    if legacy_enabled() or not root:
        raise HTTPException(404, "独立文献库未启用。")
    return Intake(Store(root, read_timeout=0.05))


@router.post("/intake")
def submit(body: IntakeRequest, request: Request):
    from src.core.capability_policy import allows

    actor = _authorized(request)
    try:
        intake = _intake()
        if body.saved_path:
            if not body.sha256 or (body.doi and body.url) or body.revision:
                raise ValueError("saved_source_invalid")
            job = intake.submit_local(body.saved_path, expected_sha256=body.sha256, actor=actor,
                                      doi=body.doi, url=body.url, title=body.title, timeout=0.05)
        else:
            if not allows("web_fetch"):
                raise HTTPException(403, "联网入库未获授权。")
            job = intake.submit_explicit(actor=actor, doi=body.doi, url=body.url, title=body.title,
                                         expected_sha256=body.sha256, revision=body.revision)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, {"code": "corpus_intake_unavailable"}) from exc
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(409 if code == "corpus_queue_full" else 422,
                            {"code": code if code.isidentifier() else "corpus_intake_invalid"}) from exc
    return JSONResponse({"accepted": True, "job": job}, status_code=202, headers=_PRIVATE)


@router.get("/intake/{job_id}")
def job_status(job_id: str, request: Request):
    _authorized(request)
    try:
        job = _intake().queue.status(job_id)
    except (LookupError, ValueError):
        raise HTTPException(404, "入库任务不可用。") from None
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, {"code": "corpus_intake_unavailable"}) from exc
    return JSONResponse(job, headers=_PRIVATE)


class PublicPaperRequest(BaseModel):
    model_config = {"extra": "forbid"}
    doi: str = Field(default="", max_length=512)
    pmid: str = Field(default="", max_length=16)
    pmcid: str = Field(default="", max_length=19)
    public_title: str = Field(default="", max_length=512)
    title_is_public: bool = Field(default=False, strict=True)


@router.post("/public-paper")
def submit_public_paper(body: PublicPaperRequest, request: Request):
    from citrus_corpus.auto_intake import AutoIntake
    from src.core.capability_policy import allows
    from .automatic_worker import account_epoch
    from .paper_source import public_identifiers

    actor = _authorized(request)
    if not durable_intake_enabled():
        raise HTTPException(409, {"code": "corpus_automatic_disabled"})
    if not (allows("web_fetch") and allows("bibliographic_lookup")):
        raise HTTPException(403, "联网入库未获授权。")
    owner = int(actor[8:])
    epoch = account_epoch(owner, admin=True)
    if epoch is None:
        raise HTTPException(403, "管理员权限不足。")
    try:
        doi, pmid, pmcid = public_identifiers(body.doi, body.pmid, body.pmcid)
        if bool(body.public_title) != body.title_is_public or (body.public_title and (doi or pmid or pmcid)):
            raise ValueError("publication_title_attestation_required")
        job = AutoIntake(_intake().store).submit(owner=owner, epoch=epoch, producer="admin", doi=doi, pmid=pmid,
                                                pmcid=pmcid, title=body.public_title)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, {"code": "corpus_intake_unavailable"}) from exc
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(409 if code == "corpus_queue_full" else 422,
                            {"code": code if code.isidentifier() else "corpus_intake_invalid"}) from exc
    return JSONResponse({"accepted": True, "job": job}, status_code=202, headers=_PRIVATE)


@router.get("/intake")
def recent_intake(request: Request):
    _authorized(request)
    try:
        jobs = _intake().queue.recent()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise HTTPException(503, {"code": "corpus_intake_unavailable"}) from exc
    return JSONResponse({"jobs": jobs, "automatic_enabled": durable_intake_enabled()}, headers=_PRIVATE)
