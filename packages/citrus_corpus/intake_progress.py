"""Project durable intake, rights and generation receipts for administrators."""
import json

from .rights import PMC_CLOUD_EVIDENCE


def project_progress(conn, jobs, *, active, retrieval):
    pairs = {(job["document_id"], job["version_id"]) for job in jobs if job["version_id"] is not None}
    generations, grants = _version_receipts(conn, pairs) if pairs else ({}, {})
    return [_progress(job, generations, grants, active, retrieval) for job in jobs]


def _version_receipts(conn, pairs):
    selected = json.dumps(sorted(pairs))
    generations = {}
    for record in conn.execute("""SELECT publication.generation, publication.state, publication.published,
            json_extract(member.value, '$.document_id') AS document_id,
            json_extract(member.value, '$.version_id') AS version_id
            FROM publications AS publication, json_each(publication.selection) AS member
            JOIN json_each(?) AS pair
            ON document_id=json_extract(pair.value, '$[0]') AND version_id=json_extract(pair.value, '$[1]')
            WHERE publication.state IN ('ready', 'published', 'obsolete')
            ORDER BY publication.seq DESC""", (selected,)):
        generations.setdefault((record["document_id"], record["version_id"]), []).append(record)
    grants = {}
    for record in conn.execute("""SELECT event.seq, event.document_id, event.version_id, event.basis,
            json_extract(event.evidence, '$.license') AS license FROM rights_events AS event
            JOIN json_each(?) AS pair ON event.document_id=json_extract(pair.value, '$[0]')
            AND event.version_id=json_extract(pair.value, '$[1]')
            WHERE event.decision='granted' ORDER BY event.seq DESC""", (selected,)):
        grants.setdefault((record["document_id"], record["version_id"]),
                          {"event_id": record["seq"], "basis": record["basis"], "license": record["license"]})
    return generations, grants


def _progress(job, generations, grants, active, retrieval):
    pair = job["document_id"], job["version_id"]
    records = generations.get(pair, ())
    published = {r["generation"] for r in records if r["published"] is not None
                 or (r["generation"] == active and r["state"] == "ready")}
    latest_published = next((r["generation"] for r in records if r["generation"] in published), None)
    request = json.loads(job["request"])
    foreground = request.get("kind") == "automatic" and request.get("path") == "foreground"
    # Old foreground receipts also archived exact bytes at submit, before raw_sha256 was populated.
    original = job["raw_sha256"] or (job["expected_sha256"] if foreground else None)
    corroborated = request.get("rights_basis") == PMC_CLOUD_EVIDENCE
    proof = foreground or corroborated
    indexed = records[0]["generation"] if records else None
    return {
        "bibliography_verified": job["state"] == "metadata_only" or corroborated,
        "original_sha256": original,
        "source_rights": {"basis": PMC_CLOUD_EVIDENCE, "license": request["license"]} if original and proof else None,
        "rights_obtained": grants.get(pair),
        "rights": job["rights"],
        "prepared": job["state"] == "prepared" and pair[1] is not None,
        # The generation is the first committed evidence of both encoding and indexing.
        "encoded_generation": indexed,
        "indexed_generation": indexed,
        "published_generation": latest_published,
        "active_generation": active if active in published else None,
        "visible_generation": retrieval if job["rights"] == "granted" and retrieval in published else None,
        "published": job["rights"] == "granted" and active in published,
    }
