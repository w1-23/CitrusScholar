"""Short-lived, audience-bound Host handoff and Workbench session cookies."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


class InvalidTicket(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    user: str
    project: str
    return_to: str
    source_assets: tuple[str, ...] = ()
    frozen_project: str = ""
    frozen_render_version: int = 1

    @property
    def owner(self):
        return hashlib.sha256(f"{self.user}\n{self.project}".encode()).hexdigest()


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def issue(key, purpose, audience, claims, *, lifetime=60):
    token = _b64(json.dumps({**claims, "iss": purpose, "aud": audience,
                             "exp": int(time.time()) + lifetime}, separators=(",", ":")).encode())
    signature = hmac.digest(key, (purpose + "." + token).encode("ascii"), "sha256")
    return token + "." + _b64(signature)


def verify(key, purpose, audience, ticket, *, max_lifetime):
    if not key or not ticket or len(ticket) > 4096:
        raise InvalidTicket
    try:
        payload, sig = ticket.split(".")
        expected = hmac.digest(key, (purpose + "." + payload).encode("ascii"), "sha256")
        if not hmac.compare_digest(expected, _decode(sig)):
            raise InvalidTicket
        claims = json.loads(_decode(payload))
        now = int(time.time())
        if (not isinstance(claims, dict) or claims.get("iss") != purpose or claims.get("aud") != audience
                or type(claims.get("exp")) is not int or not now < claims["exp"] <= now + max_lifetime):
            raise InvalidTicket
        return claims
    except (ValueError, TypeError, OverflowError, UnicodeError, binascii.Error):
        raise InvalidTicket from None


_REF = re.compile(r"[a-zA-Z0-9_.:-]{1,160}\Z")


def parse_session(claims, *, from_host=False):
    user, project, return_to = (claims.get(key) for key in ("user_ref", "project_ref", "return_to"))
    assets = claims.get("source_assets", [])
    if not all(isinstance(v, str) and _REF.fullmatch(v) for v in (user, project)):
        raise InvalidTicket
    if not isinstance(return_to, str) or len(return_to) > 512 or return_to[:2] == "//" or "\\" in return_to:
        raise InvalidTicket
    if return_to and (not return_to.startswith("/") or urlsplit(return_to).netloc or any(ord(c) < 32 for c in return_to)):
        raise InvalidTicket
    if not isinstance(assets, list) or len(assets) > 32 or any(not isinstance(a, str) or not _REF.fullmatch(a) for a in assets):
        raise InvalidTicket
    frozen = claims.get("frozen_project", "")
    if (not isinstance(frozen, str) or frozen and (not re.fullmatch(r"[a-f0-9]{32}", frozen)
            or not project.startswith("session:") or not (from_host or user.startswith("host:")))):
        raise InvalidTicket
    revision = claims.get("frozen_render_version", 1)
    if type(revision) is not int or not 1 <= revision <= 999_999_999 or (revision > 1 and not frozen):
        raise InvalidTicket
    return Session("host:" + user if from_host else user, project, return_to, tuple(assets), frozen, revision)


def session_key():
    key = os.getenv("WORKBENCH_SESSION_KEY")
    if key is not None and len(key.encode()) < 32:
        raise ValueError("WORKBENCH_SESSION_KEY must be at least 32 bytes")
    return key.encode() if key else secrets.token_bytes(32)
