"""Server-to-server Host resource adapter, never a filesystem adapter."""
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .auth import Session, issue
from .bundle import DEPENDENCY_FORMATS, MAX_BYTES
from .models import Editability, Ref

SCRIPT_FORMATS = frozenset({"py", "r"})
STATIC_FORMATS = frozenset({"png", "svg", "pdf", "jpeg", "jpg"})


class LibraryUnavailable(Exception):
    pass


class LibraryMissing(Exception):
    pass


class Asset(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    asset_ref: Ref
    name: str = Field(max_length=255)
    format: str = Field(max_length=20)
    version: str = Field(max_length=120)
    source: str = Field(max_length=120)
    editability: Editability


Inventory = TypeAdapter(list[Asset])


class Library:
    def __init__(self, origin="", key=None, client=None):
        self.origin = origin.rstrip("/")
        self.key = key
        self.client = client
        if self.origin:
            parsed = urlsplit(self.origin)
            if (not key or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                    or "@" in parsed.netloc or (parsed.scheme != "https" and
                    (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}))):
                raise ValueError("Invalid Host resource origin or assertion key")

    def _get(self, path, session: Session, *, params=None, limit=MAX_BYTES):
        if not self.origin or not session.user.startswith("host:"):
            raise LibraryMissing
        ticket = issue(self.key, "workbench-resource", "citrus-resource-api", {
            "user_ref": session.user[5:], "project_ref": session.project,
            "scope": "figures:read", "request_path": path,
        }, lifetime=25)
        own = self.client is None
        client = self.client or httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
        try:
            with client.stream("GET", self.origin + path, headers={"Authorization": "Bearer " + ticket},
                               params=params, follow_redirects=False) as response:
                if response.status_code in (403, 404):
                    raise LibraryMissing
                if response.status_code != 200:
                    raise LibraryUnavailable
                result = bytearray()
                for chunk in response.iter_bytes(65536):
                    if len(result) + len(chunk) > limit:
                        raise LibraryUnavailable
                    result.extend(chunk)
                return bytes(result), response.headers
        except httpx.HTTPError as exc:
            raise LibraryUnavailable from exc
        finally:
            if own:
                client.close()

    def list(self, session, fmt, *, kind=""):
        if not self.origin or not session.user.startswith("host:"):
            return []
        allowed = {"script": SCRIPT_FORMATS, "static": STATIC_FORMATS, "dependency": DEPENDENCY_FORMATS,
                   "": SCRIPT_FORMATS | STATIC_FORMATS}[kind]
        if fmt and fmt not in allowed:
            return []
        query = {"type": "dependency" if kind == "dependency" else "figure", "format": fmt, "limit": 100}
        if kind == "static":
            query["rendered_only"] = "true"
        if not kind:
            query["include_rendered"] = "true"
        data, _ = self._get("/_internal/workbench/assets", session, params=query, limit=256 * 1024)
        try:
            items = Inventory.validate_json(data)
        except ValidationError as exc:
            raise LibraryUnavailable from exc
        return [item for item in items[:100] if item.format.lower() in allowed and (not fmt or item.format.lower() == fmt)]

    def asset(self, session, ref, role):
        if role not in {"source", "preview", "dependency"}:
            raise ValueError("Invalid asset role")
        if not ref or ref in {".", ".."}:
            raise LibraryMissing
        return self._get(f"/_internal/workbench/assets/{quote(ref, safe='')}/{role}", session)
