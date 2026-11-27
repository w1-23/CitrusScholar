"""Optional, owner-scoped HTTP boundary for the independent Workbench."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import workbench_bridge_protocol as wire

_PRIVATE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
          "svg": "image/svg+xml", "pdf": "application/pdf", "py": "text/plain", "r": "text/plain"}
_SCRIPT = {"py", "r"}
_BROWSER_PATH = re.compile(r"/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]*/?")
FileRef = Annotated[str, StringConstraints(pattern="^" + wire.FILE_REF + "$")]


@dataclass(frozen=True)
class BridgeConfig:
    key: bytes = field(repr=False)
    audience: str
    page: str
    endpoint: str

    @classmethod
    def from_environment(cls):
        flag = os.environ.get("CITRUS_WORKBENCH_BRIDGE_ENABLED", "0")
        if flag == "0":
            return None
        if flag != "1":
            raise ValueError("CITRUS_WORKBENCH_BRIDGE_ENABLED must be 0 or 1")
        page = _browser_destination("CITRUS_WORKBENCH_PUBLIC_URL")
        endpoint = _browser_destination("CITRUS_WORKBENCH_LAUNCH_ENDPOINT")
        if not endpoint.endswith("/api/v1/launch") or endpoint == page:
            raise ValueError("Invalid CITRUS_WORKBENCH_LAUNCH_ENDPOINT")
        audience = os.environ.get("WORKBENCH_AUDIENCE", "citrus-workbench")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", audience):
            raise ValueError("Invalid WORKBENCH_AUDIENCE")
        key_path = os.environ.get("WORKBENCH_HOST_KEY_FILE", "")
        try:
            if not Path(key_path).is_absolute():
                raise ValueError
            with Path(key_path).open("rb") as secret:
                key = secret.read(4097).removesuffix(b"\n")
            if not 32 <= len(key) <= 4096 or not all(32 < byte < 127 for byte in key):
                raise ValueError
        except (ValueError, OSError):
            raise ValueError("WORKBENCH_HOST_KEY_FILE must supply 32–4096 printable non-space ASCII bytes") from None
        return cls(key, audience, page, endpoint)


def _browser_destination(name: str) -> str:
    path = os.environ.get(name, "")
    if (len(path) > 512 or "//" in path or not _BROWSER_PATH.fullmatch(path)
            or path == "/" or any(segment in {".", ".."} for segment in path.split("/"))):
        raise ValueError(f"{name} must be a same-origin absolute path")
    return path


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_ref: str = Field(default="", pattern=rf"^(?:{wire.SESSION_REF})?$")
    source_assets: list[FileRef] = Field(default_factory=list, max_length=12)


def _deny(status: int, detail: str):
    raise HTTPException(status, detail, headers=_PRIVATE)


def _missing():
    _deny(404, "Figure asset not available")


def _asset_format(record: dict | None, session: str) -> str | None:
    if not record or record.get("visibility", "public") != "public":
        return None
    if session and record.get("session_id") != session:
        return None
    name = record.get("filename")
    if (not isinstance(name, str) or not 1 <= len(name) <= 240 or name.strip() != name
            or name in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)):
        return None
    if not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", ""))):
        return None
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in _MEDIA:
        return None
    kind = record.get("kind")
    if (suffix in {"png", "jpg", "jpeg"} and kind == "image") or (suffix == "svg" and kind == "svg"):
        return suffix
    return suffix if record.get("resource_type") == "figure" else None


def _bounded_asset(record: dict) -> bytes:
    path, size = record.get("path"), record.get("size_bytes")
    if type(size) is not int or not 0 < size <= wire.ASSET_LIMIT:
        _deny(409, "Figure resource has changed or exceeds the import limit")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                _deny(409, "Figure resource has changed or exceeds the import limit")
            data = handle.read(size + 1)
    except (OSError, TypeError, ValueError):
        _missing()
    if len(data) != size or hashlib.sha256(data).hexdigest() != record["sha256"]:
        _deny(409, "Figure resource has changed or exceeds the import limit")
    return data


def bridge_router(config: BridgeConfig, *, current_user, require_owned_session,
                  account_user, list_account_files, get_account_file) -> APIRouter:
    router = APIRouter()

    def owned(ref: str, uid: int, session: str):
        record = get_account_file(ref, uid, touch=False)
        fmt = _asset_format(record, session)
        if not fmt:
            _missing()
        return record, fmt

    def delegated_actor(request: Request, authorization: str | None, path: str):
        principal = wire.resource_principal(config.key, authorization, path)
        if not principal:
            _deny(401, "Invalid Workbench resource authorization")
        if getattr(request.app.state, "core_accepting_requests", None) is False:
            _deny(503, "Host resources unavailable")
        uid, session = principal
        user = account_user(uid)
        if not user or not user.get("active") or user.get("approval_status") != "approved":
            _deny(401, "Invalid Workbench resource authorization")
        if session:
            require_owned_session(session, user)
        return uid, session

    @router.post("/api/internal/workbench/open")
    def open_workbench(body: OpenRequest, request: Request):
        user = current_user(request)
        owner = int(user["user_id"])
        if body.session_ref:
            require_owned_session(body.session_ref, user)
        for ref in set(body.source_assets):
            owned(ref, owner, body.session_ref)
        ticket = wire.launch_ticket(config.key, config.audience, owner, body.session_ref, body.source_assets)
        return JSONResponse({"status": "authorized", "workbench_url": config.page,
                             "launch_endpoint": config.endpoint, "launch_ticket": ticket}, headers=_PRIVATE)

    @router.get(wire.ASSETS_PATH, include_in_schema=False)
    def inventory(request: Request, resource_type: Annotated[Literal["figure"], Query(alias="type")],
                  format: Annotated[str, Query(max_length=20)] = "",
                  limit: Annotated[int, Query(ge=1, le=100)] = 100,
                  authorization: Annotated[str | None, Header()] = None):
        owner, session = delegated_actor(request, authorization, wire.ASSETS_PATH)
        results = []
        for record in list_account_files(owner, session_id=session):
            fmt = _asset_format(record, session)
            ref, size = record.get("file_id"), record.get("size_bytes")
            origin = record.get("origin", "")
            if (not fmt or (format and fmt != format) or not isinstance(ref, str)
                    or not re.fullmatch(wire.FILE_REF, ref) or type(size) is not int
                    or not 0 < size <= wire.ASSET_LIMIT or not isinstance(origin, str) or len(origin) > 120):
                continue
            results.append({"asset_ref": ref, "name": record["filename"], "format": fmt,
                            "version": record["sha256"], "source": origin,
                            "editability": "preview_only" if fmt in _SCRIPT else "layout_only"})
            if len(results) >= limit:
                break
        return JSONResponse(results, headers=_PRIVATE)

    @router.get(wire.ASSETS_PATH + "/{ref}/{role}", include_in_schema=False)
    def asset(ref: FileRef, role: Literal["preview", "source"], request: Request,
              authorization: Annotated[str | None, Header()] = None):
        owner, session = delegated_actor(request, authorization, f"{wire.ASSETS_PATH}/{ref}/{role}")
        record, fmt = owned(ref, owner, session)
        if role == "preview" and fmt in _SCRIPT:
            _missing()
        return Response(_bounded_asset(record), media_type=_MEDIA[fmt], headers={
            **_PRIVATE, "X-Asset-Filename": quote(record["filename"], safe=""),
            "X-Asset-SHA256": record["sha256"], "X-Asset-Version": record["sha256"],
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'",
        })

    return router


def register_workbench_bridge(app, *, current_user, require_owned_session):
    config = BridgeConfig.from_environment()
    if config is None:
        return
    from src.auth import user_by_id
    from src.session.files import get_account_file, list_account_files

    app.include_router(bridge_router(config, current_user=current_user,
        require_owned_session=require_owned_session, account_user=user_by_id,
        list_account_files=list_account_files, get_account_file=get_account_file))
