"""Offline checks for the PDF presentation/verification cutover boundary."""
import base64
from contextlib import nullcontext
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import fitz
import pytest

from src import auth, config
from src.agent import installed_skills
from src.api import main, reference_router
from src.core import account_leases, capability_policy
from src.research import artifacts, pdf_citations, reference_audit, reference_extraction


OWNER, SESSION = 7, "fixture-session"
BASE = "/api/v2/session/" + SESSION
HEADERS = {"Origin": "http://testserver"}


def _no_legacy(*args, **kwargs):
    pytest.fail("new fusion work reached a legacy verification path")


def _pdf(text):
    with fitz.open() as doc:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
        return base64.b64encode(doc.tobytes()).decode()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    root = tmp_path / "release"
    skills = {name: "a" * 40 for name in ("literature-verification", "data-audit", "research-integrity")}
    for name in skills:
        path = root / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: offline fixture\n---\n")
    (root / "release.json").write_text(json.dumps(skills))
    (root / "fusion").mkdir()
    bundle = root / "fusion" / "bundle.json"
    bundle.write_text("{}")
    monkeypatch.setattr(installed_skills, "release_path", lambda: root)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(auth, "check_rate_limit", lambda *args: True)

    def assert_scope(sid, user):
        if (sid, user["user_id"]) != (SESSION, OWNER):
            raise HTTPException(404, "not yours")
        return OWNER

    def service_scope(owner, sid):
        if (sid, owner) != (SESSION, OWNER):
            raise PermissionError("not yours")

    async def invoke(_lease, operation, *args, **kwargs):
        return operation(*args, **kwargs)

    user = {"user_id": OWNER}
    monkeypatch.setattr(main, "current_user", lambda _: user)
    monkeypatch.setattr(main, "require_owned_session", assert_scope)
    monkeypatch.setattr(reference_router, "_user", lambda _: user)
    monkeypatch.setattr(reference_router, "_owned_session", assert_scope)
    monkeypatch.setattr(account_leases, "account_lease", lambda owner: nullcontext())
    monkeypatch.setattr(account_leases, "run_account_thread", invoke)
    monkeypatch.setattr(reference_router, "_lease", lambda owner: nullcontext())
    monkeypatch.setattr(reference_router, "run_account_thread", invoke)
    monkeypatch.setattr(reference_audit, "_scope", service_scope)
    monkeypatch.setattr(reference_audit, "_lease", lambda owner: nullcontext())
    app = FastAPI()
    app.include_router(reference_router.router)
    app.post("/api/v2/session/{session_id}/paper-review")(main.paper_review_pdf)
    app.post("/api/v2/session/{session_id}/citation-check")(main.citation_metadata_check)
    return TestClient(app), bundle, user


def test_fusion_rejects_new_direct_verification_and_only_marks_pdf(runtime, monkeypatch):
    client, _, _ = runtime
    monkeypatch.setattr(reference_extraction, "extract_inventory", _no_legacy)
    monkeypatch.setattr(reference_audit, "_create_inventory", _no_legacy)
    monkeypatch.setattr(pdf_citations, "verify_reference", _no_legacy)
    published = []

    def publish(**values):
        published.append(values)
        return {"artifact_url": "/api/v2/session/fixture-session/artifact/test-id"}

    monkeypatch.setattr(artifacts, "publish_artifact", publish)
    assert client.post(BASE + "/reference-audits", json={"text": "Example"}, headers=HEADERS).status_code == 409
    assert client.post(BASE + "/citation-check", json={"doi": "10.1234/example"}).status_code == 409
    response = client.post(BASE + "/paper-review", json={"name": "paper.pdf",
        "data": _pdf("Sample [2]\nReferences\n[3] bibliography only")})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verification_mode"] == "external" and result["status"] == "presentation_only"
    assert result["highlighted_pdf_url"].endswith("test-id")
    assert not {"references", "audit_id", "audit", "extraction"} & set(result)
    assert (published[0]["owner"], published[0]["session_id"]) == (OWNER, SESSION)
    with fitz.open(stream=published[0]["content"], filetype="pdf") as check:
        page = check[0]
        assert len(list(page.annots())) == 1
    response = client.post(BASE + "/paper-review", json={"name": "scan.pdf", "data": _pdf("")})
    assert response.json()["status"] == "incomplete"
    assert response.json()["verification_mode"] == "external"
    assert len(published) == 1


def test_exact_historical_start_proof_and_saved_views(runtime, monkeypatch):
    client, bundle, user = runtime
    # Legacy catalog without a fused bundle retains its existing create path.
    raw_bundle = bundle.read_bytes()
    bundle.unlink()
    legacy = {"references": [{"title": "Example", "doi": "10.1234/example"}]}
    first = client.post(BASE + "/reference-audits", json=legacy, headers=HEADERS)
    second = client.post(BASE + "/reference-audits", json=legacy, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    started_id, draft_id = first.json()["audit_id"], second.json()["audit_id"]
    with reference_audit._connect() as conn:
        conn.execute("UPDATE reference_audits SET requests_used=1 WHERE id=?", (started_id,))
    bundle.write_bytes(raw_bundle)
    monkeypatch.setattr(capability_policy, "allows", lambda _: False)
    base = BASE + "/reference-audits/"
    assert client.get(base.rstrip("/")).status_code == 200
    assert client.get(base + draft_id).status_code == 200
    assert client.get(base + started_id + "/export?format=json").status_code == 200
    assert client.post(base + draft_id + "/run", json={"resume": True}, headers=HEADERS).status_code == 409
    run = client.post(base + started_id + "/run", json={"resume": True}, headers=HEADERS)
    assert run.status_code == 200 and run.json()["run_reason"] == "network_disabled"
    assert client.post(base + draft_id + "/cancel", headers=HEADERS).status_code == 200
    user["user_id"] = OWNER + 1
    assert client.post(base + started_id + "/run", json={"resume": True}, headers=HEADERS).status_code == 404


def test_private_library_accepts_svg_without_expanding_chat_attachment_list(monkeypatch):
    from src.api import turn_envelope

    app = FastAPI()
    app.post("/api/v2/session/{session_id}/files/upload", status_code=201)(main.upload_account_library_file)
    monkeypatch.setattr(main, "current_user", lambda _: {"user_id": OWNER})
    monkeypatch.setattr(main, "_require_active_turn_session", lambda sid, user: None)
    received = []

    def store(**values):
        received.append(values)
        return dict(file_id="a" * 32, filename=values["name"], size_bytes=len(values["content"]), sha256="b" * 64)

    monkeypatch.setattr(turn_envelope, "_register_upload", store)
    raw = b'<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>'
    client = TestClient(app)
    assert client.post(BASE + "/files/upload?filename=panels.svg", content=raw).status_code == 201
    assert received[0]["content"] == raw and received[0]["mime"] == "image/svg+xml"
    assert received[0]["owner_user_id"] == OWNER and received[0]["session_id"] == SESSION
    assert "svg" not in turn_envelope._ALLOWED_UPLOAD_SUFFIXES
    assert client.post(BASE + "/files/upload?filename=panels.exe", content=raw).status_code == 400
