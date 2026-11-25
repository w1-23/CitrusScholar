"""Prepare bounded, trusted registry observations for the external matcher.

This module contains transport and receipt shaping only. It neither adjudicates
matches nor accepts addresses supplied by a user or model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from hashlib import sha256
import re
import time
from urllib.parse import quote, urlencode

from src.core.cancel import raise_if_cancelled
from src.core.capability_policy import allows
from src.core.tracing import web_search_enabled
from src.research import literature

_DOI = re.compile(r"10\.[0-9]{4,9}/[!-~]{1,240}", re.I)
_RECORDS = 24
_MAX_LOOKUPS = 40
_MAX_RESPONSE = 24 * 1024
_MAX_OBSERVATIONS = 512 * 1024


def _policy_check():
    raise_if_cancelled("external_literature_observations")
    if not (allows("bibliographic_lookup") and web_search_enabled()):
        raise PermissionError("bibliographic_lookup_disabled")


def _doi_routes(doi):
    value = quote(doi, safe="/")
    if any(segment in {"", ".", ".."} for segment in doi.split("/")):
        return ()
    return (("crossref", f"https://api.crossref.org/works/{value}"),
            ("datacite", f"https://api.datacite.org/dois/{value}"),
            ("openalex", f"https://api.openalex.org/works/doi:{value}"),
            ("openalex", f"https://api.openalex.org/works/doi:{value}?select=is_retracted"))


def _title_routes(title, author):
    # Match URL identities expected by the pinned upstream observation parser.
    cross = urlencode({"query.bibliographic": f"{title} {author}".strip(), "rows": 3,
                      "select": "DOI,title,author,issued,container-title,type"})
    oa = urlencode({"search": title, "per-page": 3,
                    "select": "id,doi,title,publication_year,authorships,is_retracted"})
    return (("crossref", "https://api.crossref.org/works?" + cross),
            ("openalex", "https://api.openalex.org/works?" + oa))


def _minimal_body(provider, data, searching):
    # Drop abstracts, unrelated embedded records and provider diagnostics.
    def keep(row, keys):
        return {key: row[key] for key in keys if key in row} if isinstance(row, dict) else {}
    if provider == "crossref":
        fields = ("DOI", "title", "author", "issued", "container-title", "type", "updated-by", "relation", "URL")
        msg = data.get("message")
        if not isinstance(msg, dict):
            return None
        if searching:
            return {"message": {"items": [keep(row, fields) for row in (msg.get("items") or [])[:3]]}}
        return {"message": keep(msg, fields)}
    if provider == "datacite":
        record = data.get("data")
        if not isinstance(record, dict) or not isinstance(record.get("attributes"), dict):
            return None
        return {"data": {"attributes": keep(record["attributes"],
            ("doi", "titles", "creators", "publicationYear", "publisher"))}}
    fields = ("id", "doi", "title", "display_name", "publication_year", "authorships", "is_retracted", "primary_location")
    if searching:
        return {"results": [keep(row, fields) for row in (data.get("results") or [])[:3]]}
    return keep(data, fields)


def _first_surname(value):
    # URL parameter formatting only, mirroring Kalaris `first_surname`.
    import unicodedata

    first = re.split(r"\s+and\s+", value, maxsplit=1, flags=re.I)[0]
    first = first.split(",", 1)[0] if "," in first else (first.split() or [""])[-1]
    first = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9 ]+", " ", first).strip()


class RegistryReceipts:
    """Job-scoped transport receipts. A dispatch without proof is never repeated."""

    def __init__(self, identity):
        self.identity = identity

    def send(self, key, operation):
        from src.core.task_events import transaction

        digest = sha256(key.encode()).hexdigest()
        identity = self.identity
        with transaction() as conn:
            job = conn.execute("SELECT owner_user_id,session_id,request_id,status FROM task_jobs WHERE job_id=?",
                               (identity.run_id,)).fetchone()
            if (not job or tuple(job)[:3] != (identity.owner_user_id, identity.session_id, identity.user_turn_id)
                    or job[3] not in ('running', 'detached')):
                raise PermissionError("bibliographic_scope_changed")
            conn.execute("""CREATE TABLE IF NOT EXISTS external_registry_receipts (
                job_id TEXT NOT NULL REFERENCES task_jobs(job_id) ON DELETE CASCADE,
                key TEXT NOT NULL, receipt TEXT, PRIMARY KEY(job_id,key))""")
            row = conn.execute("SELECT receipt FROM external_registry_receipts WHERE job_id=? AND key=?",
                               (identity.run_id, digest)).fetchone()
            if row:
                return json.loads(row[0]) if row[0] else None
            count = conn.execute("SELECT COUNT(*) FROM external_registry_receipts WHERE job_id=?",
                                 (identity.run_id,)).fetchone()[0]
            if count >= _MAX_LOOKUPS:
                return None
            conn.execute("INSERT INTO external_registry_receipts VALUES (?,?,NULL)", (identity.run_id, digest))
        receipt = operation()
        encoded = json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > _MAX_RESPONSE:
            return None
        with transaction() as conn:
            conn.execute("UPDATE external_registry_receipts SET receipt=? WHERE job_id=? AND key=? AND receipt IS NULL",
                         (encoded, identity.run_id, digest))
        return receipt


def collect_observations(report, *, identity=None, deadline=None):
    """Only accept parsed, authorized Worker report inputs. No URL replay."""
    _policy_check()
    deadline = min(time.monotonic() + 30, deadline) if deadline else time.monotonic() + 30
    cassette, candidates = {}, []
    remaining = _MAX_OBSERVATIONS
    records = report.get("records") or []
    durable = RegistryReceipts(identity) if identity else None
    send = durable.send if durable else lambda key, operation: operation()

    def query(provider, url, searching):
        status, data, _ = literature._get(url, source=provider)
        body = _minimal_body(provider, data, searching) if status == 200 and isinstance(data, dict) else None
        if status == 200 and not body:
            status = 0
        return {"status": status if type(status) is int else 0, "body": body,
                "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    with literature.bounded_lookup(deadline=deadline, check=_policy_check):
        for record in records[:_RECORDS]:
            if record.get("status") in {"verified", "mismatch"}:
                continue
            submitted = record.get("submitted") or {}
            doi = str(submitted.get("doi") or "").casefold()
            title = " ".join(str(submitted.get("title") or "").split())[:350]
            author = str(submitted.get("author") or "")[:250]
            if doi:
                if not _DOI.fullmatch(doi):
                    continue
                routes = _doi_routes(doi)
            elif title:
                routes = _title_routes(title, _first_surname(author))
            else:
                continue
            for index, (provider, url) in enumerate(routes):
                if doi:
                    cross = cassette.get(routes[0][1], {}).get("status")
                    datacite = cassette.get(routes[1][1], {}).get("status")
                    if index == 1 and cross != 404 or index == 2 and datacite != 404 or index == 3 and cross != 200:
                        continue
                if url in cassette:
                    continue
                if len(cassette) >= _MAX_LOOKUPS or time.monotonic() >= deadline or remaining <= 0:
                    break
                _policy_check()
                receipt = send(url, lambda: query(provider, url, not doi)) or {"status": 0, "body": None, "queried_at": None}
                size = len(json.dumps(receipt, ensure_ascii=False).encode())
                if size <= min(_MAX_RESPONSE, remaining):
                    remaining -= size
                    cassette[url] = receipt
            if title and not doi and len(cassette) < _MAX_LOOKUPS - 2 and time.monotonic() < deadline:
                # Provider parsers, not model text, produce RefChecker candidates.
                _policy_check()
                def supplement():
                    status, rows, _ = literature._crossref_search(title, rows=3)
                    return [{k: row[k] for k in ("doi", "title", "year", "authors") if k in row}
                            for row in rows[:3]] if status == 200 else []
                for row in send("refchecker-candidates:" + title, supplement) or []:
                    received = str(row.get("doi") or "")
                    if not _DOI.fullmatch(received):
                        continue
                    authors = row.get("authors") or []
                    candidates.append({"record_index": record["record_index"], "doi": received,
                        "title": str(row.get("title") or "")[:500], "year": str(row.get("year") or ""),
                        "author": str(authors[0] if isinstance(authors, list) and authors else authors)[:250],
                        "provider": "Crossref", "record_id": received, "status": 200,
                        "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "url": "https://doi.org/" + received})
    return {"http": cassette, "refchecker_candidates": candidates}
