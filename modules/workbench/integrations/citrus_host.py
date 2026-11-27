"""Optional Host-side bridge using existing owner-scoped resource functions.

This file is a consumer of the Workbench wire contract. It is not loaded by the
Workbench service and does not install, migrate, or modify the live Host.
"""
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from wb.auth import InvalidTicket, issue, verify
from wb.library import MAX_BYTES
from wb.store import digest

FileRef = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "svg": "image/svg+xml", "pdf": "application/pdf", "py": "text/plain", "r": "text/plain"}


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_ref: str = Field(default="", pattern=r"^[a-zA-Z0-9_.:-]{0,100}$")
    source_assets: list[FileRef] = Field(default_factory=list, max_length=12)


def figure_format(info):
    if not info:
        return None
    fmt = Path(info.get("filename", "")).suffix.lower().lstrip(".")
    kind = info.get("kind")
    if (kind == "image" and fmt in {"png", "jpeg", "jpg"}) or (kind == "svg" and fmt == "svg"):
        return fmt
    # Ordinary research PDFs are not figures. Hosts can use their existing
    # resource classification to explicitly mark PDF or source attachments.
    return fmt if info.get("resource_type") == "figure" and fmt in MEDIA else None


def make_router(*, key, current_user, require_owned_session, list_account_files,
                get_account_file, audience="citrus-workbench", workbench_url="/workbench/docs",
                return_to=lambda _sid: "/"):
    if len(key) < 32:
        raise ValueError("Host key must be at least 32 bytes")
    router = APIRouter()

    def auth_resource(request, authorization):
        try:
            if not authorization or not authorization.startswith("Bearer "):
                raise InvalidTicket
            claims = verify(key, "workbench-resource", "citrus-resource-api", authorization[7:], max_lifetime=30)
            if claims.get("scope") != "figures:read" or claims.get("request_path") != request.url.path:
                raise InvalidTicket
            user = claims["user_ref"]
            project = claims["project_ref"]
            if not isinstance(user, str) or not user.isdecimal() or int(user) <= 0:
                raise InvalidTicket
            if not isinstance(project, str) or not (project == "account" or project.startswith("session:")):
                raise InvalidTicket
            return int(user), project.removeprefix("session:") if project != "account" else ""
        except (KeyError, InvalidTicket, ValueError):
            raise HTTPException(401, "Invalid resource assertion") from None

    def owned_file(ref, owner, sid):
        info = get_account_file(ref, owner, touch=False)
        fmt = figure_format(info)
        if fmt is None or (sid and info.get("session_id") != sid):
            raise HTTPException(404, "Figure asset not available")
        return info, fmt

    @router.post("/api/internal/workbench/open")
    def open_workbench(body: OpenRequest, request: Request):
        user = current_user(request)
        owner = int(user["user_id"])
        if body.session_ref:
            require_owned_session(body.session_ref, user)
        for ref in body.source_assets:
            owned_file(ref, owner, body.session_ref)
        ticket = issue(key, "citrus-workbench-launch", audience, {
            "user_ref": str(owner), "project_ref": f"session:{body.session_ref}" if body.session_ref else "account",
            "source_assets": list(dict.fromkeys(body.source_assets)), "return_to": return_to(body.session_ref),
            "jti": uuid4().hex,
        })
        return JSONResponse({"workbench_url": workbench_url, "launch_ticket": ticket,
                             "launch_endpoint": "/api/v1/launch", "status": "authorized"},
                            headers={"Cache-Control": "private, no-store"})

    @router.get("/_internal/workbench/assets")
    def list_figures(request: Request, type: str = Query(pattern="^figure$"), format: str = "",
                     limit: int = Query(default=100, ge=1, le=100), authorization: str | None = Header(default=None)):
        owner, sid = auth_resource(request, authorization)
        rows = []
        for item in list_account_files(owner, session_id=sid):
            fmt = figure_format(item)
            if fmt is None or (format and fmt != format):
                continue
            rows.append({"asset_ref": item["file_id"], "name": item["filename"], "format": fmt,
                         "version": item["sha256"], "source": item["origin"],
                         "editability": "preview_only" if fmt in {"py", "r"} else "layout_only"})
            if len(rows) == limit:
                break
        return rows

    @router.get("/_internal/workbench/assets/{ref}/{role}")
    def asset(ref: FileRef, role: str, request: Request, authorization: str | None = Header(default=None)):
        owner, sid = auth_resource(request, authorization)
        if role not in {"source", "preview"}:
            raise HTTPException(404)
        info, fmt = owned_file(ref, owner, sid)
        if role == "preview" and fmt in {"py", "r"}:
            raise HTTPException(404, "Script has no static preview")
        # This is the trusted Host side. The existing accessor authorizes and
        # resolves the file. The path never leaves Host or enters Workbench.
        try:
            with Path(info["path"]).open("rb") as file:
                data = file.read(MAX_BYTES + 1)
        except OSError:
            raise HTTPException(404, "Figure asset not available") from None
        sha = digest(data)
        if len(data) > MAX_BYTES or sha != info.get("sha256"):
            raise HTTPException(409, "Figure resource has changed or exceeds the import limit")
        return Response(data, media_type=MEDIA[fmt], headers={"Cache-Control": "private, no-store",
                        "X-Asset-Filename": quote(info["filename"], safe=""), "X-Asset-SHA256": sha,
                        "X-Asset-Version": sha, "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'"})

    return router
