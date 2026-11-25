"""Publication-right policy for shared-corpus proposals.

A document version may be published only when either

* an administrator independently grants it through ``Catalog.grant``, or
* the *original candidate archive* carries explicit, verified open-license
  metadata from a trusted source.

Retention approval in ``src.core.corpus_review`` is a separate concern and is
never treated as a publication right.  Upload consent is sharing consent, not
a licence.  When in doubt this module returns ``needs_review`` (fail closed).
"""
from __future__ import annotations

import re
from typing import Any

PMC_CLOUD_EVIDENCE = "ncbi_pmc_cloud"
PMC_CLOUD_METADATA_URL = re.compile(
    r"https://pmc-oa-opendata\.s3\.amazonaws\.com/metadata/PMC[0-9]+\.[1-9][0-9]*\.json")

TRUSTED_LICENSES = frozenset({
    "cc0",
    "public-domain",
    "cc-by",
    "cc-by-sa",
})

TRUSTED_PERMISSION_SOURCES = frozenset({
    "crossref",
    "doaj",
    "openalex",
    "publisher",
    "repository",
    "trusted-source",
    "unpaywall",
})

TRUSTED_LICENSE_MARKERS = frozenset({
    "open-license",
    "trusted-source",
    "verified-license",
})

_LICENSE_KEYS = (
    "license",
    "license_id",
    "license_url",
    "reuse_license",
    "rights",
    "access_rights",
)

_CC_RE = re.compile(
    r"creativecommons\.org/(?:licenses|publicdomain)/([a-z0-9-]+)")
_SLUG_RE = re.compile(r"[^a-z0-9.-]+")
_VERSION_RE = re.compile(r"-(?:\d+(?:\.\d+)*)(?:-[a-z]+)?$")

_ALIASES = {
    "by": "cc-by",
    "by-sa": "cc-by-sa",
    "cc0-1-0": "cc0",
    "cc-zero": "cc0",
    "publicdomain": "public-domain",
    "public-domain-mark": "public-domain",
    "pd": "public-domain",
    "zero": "cc0",
}


def _normalize_marker(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = _SLUG_RE.sub("-", text).strip("-")
    text = re.sub(r"-+", "-", text)
    return _ALIASES.get(text, text)


def license_token(value: object) -> str:
    """Normalize one licence value into a comparison token.

    Unknown values remain normalized strings so callers can record them
    without ever trusting them.
    """
    if isinstance(value, dict):
        value = (value.get("URL") or value.get("url") or value.get("id")
                 or value.get("name") or "")
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    creative = _CC_RE.search(text)
    if creative:
        text = creative.group(1)
    else:
        text = text.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        text = _SLUG_RE.sub("-", text).strip("-")
        text = re.sub(r"-+", "-", text)
        text = _VERSION_RE.sub("", text)
    token = _ALIASES.get(text, text)
    if token.startswith("cc-") and token.count("-") > 1:
        # Strip a trailing version that survived odd spellings.
        token = _VERSION_RE.sub("", token)
    return token


def _values(value: Any):
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield item
        return
    yield value


def evaluate_source_rights(meta: Any) -> dict[str, Any]:
    """Return the publication-right decision derived from archive metadata."""
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("publish_permission") is True:
        source = _normalize_marker(meta.get("publish_permission_source"))
        if source in TRUSTED_PERMISSION_SOURCES:
            return {
                "status": "trusted",
                "basis": "trusted_metadata",
                "license": "",
                "reason": "trusted_publish_permission",
            }
    marker = _normalize_marker(
        meta.get("rights_basis") or meta.get("license_basis") or "")
    verified = meta.get("license_verified") is True or marker in TRUSTED_LICENSE_MARKERS
    if verified:
        for key in _LICENSE_KEYS:
            for value in _values(meta.get(key)):
                token = license_token(value)
                if token in TRUSTED_LICENSES:
                    return {
                        "status": "trusted",
                        "basis": "trusted_metadata",
                        "license": token,
                        "reason": "trusted_source_license",
                    }
    return {
        "status": "needs_review",
        "basis": "needs_review",
        "license": "",
        "reason": "license_not_trusted",
    }


def decision_evidence(decision, evidence, license_id):
    """Structure an operator-reviewed, version-bound publication decision."""
    if decision == "revoked":
        return "admin_revoke", "", ""
    if (not isinstance(evidence, str) or not 1 <= len(evidence) <= 1000
            or any(ord(char) < 32 for char in evidence)):
        raise ValueError("rights_evidence_required")
    if license_id:
        checked = evaluate_source_rights({"license_verified": True, "license": license_id})
        if checked["status"] != "trusted":
            raise ValueError("rights_license_inadmissible")
        return "operator_verified_open_license", checked["license"], evidence
    return "admin_grant", "", evidence


def automatic_evidence(pmcid, license_id, evidence_url, text_url):
    """Check locator consistency; the trusted caller authenticates their contents."""
    if (not isinstance(evidence_url, str) or len(evidence_url) > 2048
            or not PMC_CLOUD_METADATA_URL.fullmatch(evidence_url)):
        raise ValueError("automatic_pmc_evidence_invalid")
    version = evidence_url.rsplit("/", 1)[1].removesuffix(".json")
    prefix = evidence_url.split("/metadata/", 1)[0]
    if pmcid != version.split(".", 1)[0] or text_url != f"{prefix}/{version}/{version}.txt":
        raise ValueError("automatic_pmc_version_mismatch")
    if not isinstance(license_id, str) or len(license_id) > 256:
        raise ValueError("rights_license_inadmissible")
    value = license_id.strip().casefold()
    if value in TRUSTED_LICENSES:
        token = value
    elif re.fullmatch(r"cc-by(?:-sa)?-(?:[1-4]\.0)", value) or re.fullmatch(
            r"https://creativecommons\.org/(?:licenses/by(?:-sa)?/[1-4]\.0|publicdomain/(?:zero|mark)/1\.0)/?",
            value):
        token = license_token(value)
        if value.rstrip("/").endswith("/mark/1.0"):
            token = "public-domain"
    else:
        raise ValueError("rights_license_inadmissible")
    if token not in TRUSTED_LICENSES:
        raise ValueError("rights_license_inadmissible")
    return token, {"locator": evidence_url, "license": token, "text_url": text_url, "pmc_version": version}
