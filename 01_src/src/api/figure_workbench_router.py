"""Signed-in workbench page and revision-bound presentation API."""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from src.api.figure_router import _PAGE_HEADERS, _SVG_HEADERS, _fail, _owner, _read_scope, _write_scope
from src.config import WEB_DIR
from src.core.account_leases import run_account_thread
from src.figures import projects
from src.figures.workbench import service

router = APIRouter(prefix="/api/v2/figure-workbench", tags=["figure-workbench"])
page_router = APIRouter()
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
Format = Literal["svg", "png", "pdf"]


class GroupCommand(BaseModel):
    model_config = {"extra": "forbid"}
    group_id: str = Field(pattern=r"^grp_[a-f0-9]{32}$", strict=True)
    property: str = Field(min_length=1, max_length=24, strict=True)
    value: Any


class GroupBatch(BaseModel):
    model_config = {"extra": "forbid"}
    commands: list[GroupCommand] = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0, strict=True)
    draft_revision: int = Field(default=0, ge=0, le=9007199254740991, strict=True)


@page_router.get("/figure-workbench", include_in_schema=False)
async def workbench_page(request: Request):
    from src.auth import user_for_token

    if not user_for_token(request.cookies.get("citrus_auth", "")):
        return RedirectResponse(url="/?next=" + quote(request.url.path, safe=""), status_code=303)
    page = WEB_DIR / "figure-workbench.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="图件工作台页面未部署。")
    return FileResponse(page, headers=_PAGE_HEADERS)


@router.get("/projects")
async def list_figures(request: Request, limit: int = Query(default=24, ge=1, le=100),
                       offset: int = Query(default=0, ge=0), query: str = Query(default="", max_length=120),
                       kind: Literal["", "spec", "script", "svg"] = "",
                       chart: Literal["", "line", "bar", "scatter", "errorbar", "histogram", "boxplot", "multipanel"] = "",
                       access: Literal["", "editable", "read_only"] = ""):
    owner = _owner(request)
    with _read_scope(owner) as lease:
        result = await run_account_thread(lease, service.list_assets, owner, limit=limit, offset=offset,
                                          query=query, kind=kind, chart=chart, access=access)
    return JSONResponse(result, headers=_HEADERS)


@router.get("/projects/{figure_id}")
async def open_figure(figure_id: str, request: Request, revision: int = Query(ge=0)):
    owner = _owner(request)
    with _read_scope(owner) as lease:
        try:
            result = await run_account_thread(lease, service.detail, figure_id, owner, revision)
        except projects.ProjectError as exc:
            return _fail(exc)
    return JSONResponse(result, headers=_HEADERS)


async def _edit(figure_id, request, body, *, save):
    owner = _owner(request)
    with _write_scope(owner) as lease:
        try:
            result = await run_account_thread(lease, service.edit, figure_id, owner, body.expected_revision,
                [command.model_dump() for command in body.commands], draft_revision=body.draft_revision, save=save)
        except projects.ProjectError as exc:
            return _fail(exc)
    return JSONResponse(result, headers=_HEADERS)


@router.post("/projects/{figure_id}/preview")
async def preview_edit(figure_id: str, request: Request, body: GroupBatch):
    return await _edit(figure_id, request, body, save=False)


@router.post("/projects/{figure_id}/save")
async def save_edit(figure_id: str, request: Request, body: GroupBatch):
    return await _edit(figure_id, request, body, save=True)


async def _artifact(figure_id, fmt, request, revision, *, generate=False, inline=False):
    owner = _owner(request)
    scope = _write_scope if generate else _read_scope
    with scope(owner) as lease:
        try:
            artifact = await run_account_thread(lease, service.download, figure_id, owner, revision, fmt, generate=generate)
        except projects.ProjectError as exc:
            return _fail(exc)
    disposition = "inline" if inline else "attachment; filename*=UTF-8''" + quote(artifact.name, safe="")
    headers = {**_HEADERS, "Content-Disposition": disposition, "X-Figure-Revision": str(revision),
               "X-Figure-SHA256": artifact.digest, "Content-Length": str(len(artifact.body))}
    if fmt == "svg":
        headers.update(_SVG_HEADERS)
    return Response(artifact.body if request.method != "HEAD" else b"", media_type=projects.MIME_BY_FORMAT[fmt], headers=headers)


@router.get("/projects/{figure_id}/preview/{fmt}")
async def preview_image(figure_id: str, fmt: Literal["svg", "png"], request: Request, revision: int = Query(ge=0)):
    return await _artifact(figure_id, fmt, request, revision, inline=True)


@router.get("/projects/{figure_id}/export/{fmt}", operation_id="download_workbench_figure")
@router.head("/projects/{figure_id}/export/{fmt}", include_in_schema=False)
@router.post("/projects/{figure_id}/export/{fmt}", operation_id="generate_workbench_figure")
async def export_figure(figure_id: str, fmt: Format, request: Request, revision: int = Query(ge=0)):
    return await _artifact(figure_id, fmt, request, revision, generate=request.method == "POST")
