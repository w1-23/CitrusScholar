"""Same-origin browser gateway to the independent Workbench HTTP service."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .workbench_bridge_protocol import launch_for_owner

_WORKBENCH_COOKIE = "workbench_session"
_BINDING_COOKIE = "citrus_wb_binding"
_MAX_REQUEST = 30 * 1024 * 1024
_MAX_RESPONSE = 64 * 1024 * 1024
_ALLOWED_RESPONSE = frozenset({"cache-control", "content-disposition", "content-security-policy",
    "content-type", "etag", "retry-after", "x-content-type-options", "x-content-sha256", "x-figure-revision", "x-export-id"})


def workbench_gateway(current_user):
    router = APIRouter(include_in_schema=False)
    upstream = os.getenv("CITRUS_WORKBENCH_UPSTREAM", "")
    if upstream != "http://127.0.0.1:8062":
        raise ValueError("Invalid Workbench loopback upstream")
    key = Path(os.environ["WORKBENCH_HOST_KEY_FILE"]).read_bytes().strip()
    if len(key) < 32:
        raise ValueError("Invalid Workbench binding key")

    def binding(uid, token):
        return hmac.new(key, f"citrus-workbench-browser-v1\n{uid}\n{token}".encode(), hashlib.sha256).hexdigest()

    @router.api_route("/workbench/{resource:path}", methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"])
    async def proxy(request: Request, resource: str):
        uid = int(current_user(request)["user_id"])
        if (len(resource) > 512 or not re.fullmatch(r"[a-zA-Z0-9_.:/-]*", resource)
                or any(part in (".", "..", "") for part in resource.rstrip("/").split("/") if resource)):
            raise HTTPException(404, "Invalid Workbench route")
        # Mount the standalone page beneath /workbench/workbench/ so its
        # relative URLs remain intact. Do not expose arbitrary upstream paths.
        if resource == "":
            return Response(status_code=307, headers={"Location": "/workbench/workbench/"})
        if not (resource == "workbench" or resource.startswith(("workbench/", "api/v1/")) and resource != "api/v1/standalone"):
            raise HTTPException(404, "Unknown Workbench route")
        headers = {key: request.headers[key] for key in ("accept", "content-type", "origin") if key in request.headers}
        token = request.cookies.get(_WORKBENCH_COOKIE)
        proof = request.cookies.get(_BINDING_COOKIE, "")
        if (token and re.fullmatch(r"[a-zA-Z0-9_.~-]{1,8192}", token)
                and re.fullmatch(r"[a-f0-9]{64}", proof) and hmac.compare_digest(proof, binding(uid, token))):
            headers["cookie"] = f"{_WORKBENCH_COOKIE}={token}"
        body = bytearray()
        if request.method not in {"GET", "HEAD"}:
            async for chunk in request.stream():
                if len(body) + len(chunk) > _MAX_REQUEST:
                    raise HTTPException(413, "Workbench request exceeds limit")
                body.extend(chunk)
        if resource == "api/v1/launch":
            if request.method != "POST" or len(body) > 20000:
                raise HTTPException(403, "Invalid Workbench launch")
            try:
                claims = json.loads(body)
            except (ValueError, RecursionError):
                claims = None
            audience = os.getenv("WORKBENCH_AUDIENCE", "citrus-workbench")
            if not isinstance(claims, dict) or not launch_for_owner(key, audience, claims.get("ticket"), uid):
                raise HTTPException(403, "Invalid Workbench launch")
        if len(request.url.query) > 4096:
            raise HTTPException(414, "Workbench query exceeds limit")
        url = upstream + "/" + quote(resource, safe="/:")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=45.0, follow_redirects=False) as client:
                async with client.stream(request.method, url, params=request.query_params.multi_items(),
                                         headers=headers, content=bytes(body)) as response:
                    if 300 <= response.status_code < 400:
                        return Response(status_code=502)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > _MAX_RESPONSE:
                            return Response(status_code=502)
                        raw.extend(chunk)
                    out = Response(bytes(raw), status_code=response.status_code, headers={
                        k: v for k, v in response.headers.items() if k in _ALLOWED_RESPONSE or k.startswith("x-workbench-")})
                    if resource == "api/v1/launch":
                        for value in response.headers.get_list("set-cookie"):
                            if value.startswith(_WORKBENCH_COOKIE + "="):
                                issued = value.split(";", 1)[0].split("=", 1)[1]
                                if not re.fullmatch(r"[a-zA-Z0-9_.~-]{1,8192}", issued):
                                    return Response(status_code=502)
                                out.set_cookie(_BINDING_COOKIE, binding(uid, issued), max_age=8*60*60,
                                               path="/workbench/", secure=True, httponly=True, samesite="strict")
                                value = re.sub(r"(?i)(;\s*path=)[^;]+", r"\g<1>/workbench/", value)
                                out.headers.append("set-cookie", value)
                    return out
        except (httpx.TimeoutException, httpx.RequestError):
            return Response(status_code=503, headers={"Cache-Control": "no-store"})

    return router
