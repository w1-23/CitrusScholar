"""F-05: a verified SIF is an immutable environment, so Apptainer previews cache.

The old rule only allowed reuse for Docker digest tags, so every Apptainer
preview re-ran the script. These tests pin the new identity: the SIF file's
SHA-256, accepted only when it is verified (file digest, or an explicit
operator declaration), and never when it disagrees with the configured digest.
"""
import hashlib

from src.figures import preview_pipeline


def _sif(tmp_path, blob=b"apptainer-sif-bytes"):
    image = tmp_path / "figure.sif"
    image.write_bytes(blob)
    return image, hashlib.sha256(blob).hexdigest()


def test_verified_file_digest_is_an_immutable_identity(tmp_path):
    image, digest = _sif(tmp_path)
    identity, verification = preview_pipeline.apptainer_identity(
        image=str(image), declared_sha256=digest)
    assert identity == {"identity": "sif_sha256", "sha256": digest, "verified": "file_digest"}
    assert verification["verified"] == "file_digest"


def test_mismatched_sif_disables_reuse(tmp_path):
    image, _digest = _sif(tmp_path)
    identity, verification = preview_pipeline.apptainer_identity(
        image=str(image), declared_sha256="a" * 64)
    assert identity == {}
    assert verification["verified"] == "digest_mismatch"
    assert verification["actual_sha256"]


def test_unreadable_sif_needs_an_explicit_operator_declaration(monkeypatch):
    monkeypatch.delenv("CITRUS_APPTAINER_SIF_VERIFIED", raising=False)
    identity, verification = preview_pipeline.apptainer_identity(
        image="/missing/figure.sif", declared_sha256="b" * 64)
    assert identity == {}
    assert verification["identity"] == "unverified_sif"
    monkeypatch.setenv("CITRUS_APPTAINER_SIF_VERIFIED", "1")
    identity, verification = preview_pipeline.apptainer_identity(
        image="/missing/figure.sif", declared_sha256="b" * 64)
    assert identity["verified"] == "operator_declaration"
    assert verification["verified"] == "operator_declaration"


def test_a_non_sif_or_unpinned_image_is_not_an_identity(tmp_path):
    identity, verification = preview_pipeline.apptainer_identity(
        image=str(tmp_path / "figure.img"), declared_sha256="c" * 64)
    assert identity == {} and verification["identity"] == "not_a_sif"
    identity, verification = preview_pipeline.apptainer_identity(
        image=str(tmp_path / "figure.sif"), declared_sha256="")
    assert identity == {} and verification["identity"] == "no_pinned_digest"


def _project():
    return {"figure_id": "f" * 32, "render_version": 2,
            "source_json": {"kind": "script", "script": "pass\n"},
            "data_refs_json": [], "overrides_json": []}


def test_preview_is_reused_only_for_a_verified_environment(monkeypatch):
    calls = []

    def render():
        calls.append(1)
        return {"figure_id": _project()["figure_id"], "svg": "<svg/>"}

    def validate():
        return None

    monkeypatch.setattr(preview_pipeline, "environment_key",
                        lambda: {"immutable_image": True, "sif": {"sha256": "d" * 64},
                                 "FIGURES_SANDBOX_BACKEND": "apptainer"})
    first = preview_pipeline.render_preview(_project(), 7, {"color": "#111"},
                                            validate=validate, render=render, is_script=True)
    second = preview_pipeline.render_preview(_project(), 7, {"color": "#111"},
                                             validate=validate, render=render, is_script=True)
    assert len(calls) == 1
    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert second["cache"]["profile"]["mode"] == "preview"
    assert second["cache"]["environment"]["immutable_image"] is True
    # The profile is part of the key: publication is never a cached preview.
    assert preview_pipeline.render_profile(preview=False)["dpi"] != second["cache"]["profile"]["dpi"]


def test_unverified_environment_never_reuses_bytes(monkeypatch):
    calls = []

    def render():
        calls.append(1)
        return {"figure_id": _project()["figure_id"], "svg": "<svg/>"}

    monkeypatch.setattr(preview_pipeline, "environment_key",
                        lambda: {"immutable_image": False, "sif": {},
                                 "sif_verification": {"identity": "unverified_sif"},
                                 "FIGURES_SANDBOX_BACKEND": "apptainer"})
    for _ in range(2):
        result = preview_pipeline.render_preview(_project(), 7, [], validate=lambda: None,
                                                 render=render, is_script=True)
        assert result["cache"]["hit"] is False
    assert len(calls) == 2
    assert result["cache"]["environment"]["sif_verification"]["identity"] == "unverified_sif"


def test_preview_dpi_is_low_and_bounded(monkeypatch):
    assert preview_pipeline.preview_dpi() == preview_pipeline.PREVIEW_DPI
    monkeypatch.setenv("CITRUS_FIGURES_PREVIEW_DPI", "40")
    assert preview_pipeline.preview_dpi() == 48
    monkeypatch.setenv("CITRUS_FIGURES_PREVIEW_DPI", "512")
    assert preview_pipeline.preview_dpi() == 300
    monkeypatch.setenv("CITRUS_FIGURES_PREVIEW_DPI", "not-a-number")
    assert preview_pipeline.preview_dpi() == preview_pipeline.PREVIEW_DPI
