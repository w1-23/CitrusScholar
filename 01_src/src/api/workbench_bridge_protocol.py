"""MIT Host implementation of the standalone Workbench HTTP ticket format."""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import time
from uuid import uuid4

FILE_REF = r"[a-f0-9]{32}"
SESSION_REF = r"[A-Za-z0-9_-]{8,128}"
ASSET_LIMIT = 12 * 1024 * 1024
ASSETS_PATH = "/_internal/workbench/assets"
FROZEN_BUNDLE_PATH = "/_internal/workbench/frozen-bundles"
FROZEN_BUNDLE_MEDIA = "application/vnd.citrus.frozen-script-v1"
FROZEN_STYLE_MEDIA = "application/vnd.citrus.frozen-script-v2"
FROZEN_REVISION = r"[1-9][0-9]{0,8}"
FROZEN_BUNDLE_HEADER_LIMIT = 8192
_LAUNCH = "citrus-workbench-launch"
_RESOURCE = "workbench-resource"
_RESOURCE_AUDIENCE = "citrus-resource-api"
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")


def styled_import_enabled() -> bool:
    return os.getenv("CITRUS_WORKBENCH_STYLED_IMPORT") == "1"


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(component: str) -> bytes:
    return base64.b64decode(component + "=" * (-len(component) % 4),
                            altchars=b"-_", validate=True)


def _mac(key: bytes, purpose: str, payload: str) -> bytes:
    return hmac.digest(key, f"{purpose}.{payload}".encode("ascii"), "sha256")


def launch_ticket(key: bytes, audience: str, owner: int, session: str,
                  assets: list[str], *, frozen_project: str = "", frozen_render_version: int = 1) -> str:
    claims = {
        "iss": _LAUNCH,
        "aud": audience,
        "exp": int(time.time()) + 60,
        "jti": uuid4().hex,
        "user_ref": str(owner),
        "project_ref": f"session:{session}" if session else "account",
        "source_assets": list(dict.fromkeys(assets)),
        "return_to": f"/?figure_session={session}" if session else "/",
    }
    if frozen_project:
        claims["frozen_project"] = frozen_project
        if frozen_render_version > 1:
            claims["frozen_render_version"] = frozen_render_version
    payload = _encode(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_encode(_mac(key, _LAUNCH, payload))}"


def launch_for_owner(key: bytes, audience: str, ticket: str, owner: int) -> bool:
    """Reject a valid ticket for another browser identity before forwarding it."""
    if not isinstance(ticket, str) or not 1 <= len(ticket) <= 16384:
        return False
    try:
        payload, signature = ticket.split('.')
        if not _COMPONENT.fullmatch(payload) or not _COMPONENT.fullmatch(signature):
            return False
        if not hmac.compare_digest(_mac(key, _LAUNCH, payload), _decode(signature)):
            return False
        claims = json.loads(_decode(payload))
    except (ValueError, RecursionError):
        return False
    return bool(isinstance(claims, dict) and claims.get('iss') == _LAUNCH
                and claims.get('aud') == audience and claims.get('user_ref') == str(owner)
                and type(claims.get('exp')) is int and int(time.time()) < claims['exp'] <= int(time.time()) + 120)


def resource_principal(key: bytes, authorization: str | None,
                       path: str) -> tuple[int, str] | None:
    """Authenticate a read delegation and return only its canonical identity."""
    if not authorization or len(authorization) > 4103 or not authorization.startswith("Bearer "):
        return None
    try:
        payload, signature = authorization[7:].split(".")
        if not _COMPONENT.fullmatch(payload) or not _COMPONENT.fullmatch(signature):
            return None
        if not hmac.compare_digest(_mac(key, _RESOURCE, payload), _decode(signature)):
            return None
        claims = json.loads(_decode(payload))
    except (ValueError, binascii.Error, RecursionError):
        return None
    if not isinstance(claims, dict):
        return None
    expiration = claims.get("exp")
    now = int(time.time())
    if (claims.get("iss") != _RESOURCE or claims.get("aud") != _RESOURCE_AUDIENCE
            or claims.get("scope") != "figures:read" or claims.get("request_path") != path
            or type(expiration) is not int or not now < expiration <= now + 30):
        return None
    actor, project = claims.get("user_ref"), claims.get("project_ref")
    if not isinstance(actor, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", actor):
        return None
    owner = int(actor)
    if owner > 2**63 - 1:
        return None
    if project == "account":
        return owner, ""
    if isinstance(project, str) and re.fullmatch(r"session:" + SESSION_REF, project):
        return owner, project[8:]
    return None
