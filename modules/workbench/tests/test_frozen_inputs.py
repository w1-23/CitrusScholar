import hashlib
import json

import pytest

from wb.bundle import InvalidInput
from wb.frozen_inputs import decode_frozen_inputs


def envelope(*, binding="matrix", name="input_0.json", source=b"fig = None\n", extra=b""):
    data = b'{"matrix":[1,2]}'

    def record(body):
        return {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}

    header = json.dumps({"version": 1, "language": "python", "renderer": record(source),
                         "inputs": [record(data) | {"binding": binding, "name": name}]}).encode("ascii")
    return len(header).to_bytes(4, "big") + header + source + data + extra


def test_valid_bundle_is_inert_and_preserves_bindings():
    frozen = decode_frozen_inputs(envelope())
    assert frozen.entrypoint.name == "render.py"
    assert frozen.entrypoint.body == b"fig = None\n"
    assert frozen.bindings == ("matrix",)
    assert frozen.dependencies[0].body == b'{"matrix":[1,2]}'
    assert "fig = None" not in repr(frozen)


@pytest.mark.parametrize("body", [envelope(name="../input_0.json"), envelope(binding="import"),
                                  envelope(source=b"\xff"), envelope(extra=b"unexpected"),
                                  envelope()[:-1], envelope()[:-1] + b"!"])
def test_invalid_bundle_is_rejected_before_import(body):
    with pytest.raises(InvalidInput):
        decode_frozen_inputs(body)


def styled_envelope(*, revision=7, style=None):
    original = envelope()
    end = 4 + int.from_bytes(original[:4], "big")
    header = json.loads(original[4:end])
    if style is None:
        style = json.dumps({"baseline": {"version": 1, "baseline_sha256": "a" * 64,
             "coordinates_sha256": "b" * 64, "targets_sha256": "c" * 64}, "overrides_sha256": "d" * 64,
             "overrides": [{"gid": "axes_0.title", "prop": "text", "value": "New title"}]}).encode()
    header.update(version=2, render_version=revision, style={"size": len(style),
                  "sha256": hashlib.sha256(style).hexdigest()})
    raw = json.dumps(header).encode()
    return len(raw).to_bytes(4, "big") + raw + original[end:] + style


def test_styled_bundle_preserves_exact_receipt_and_expected_version():
    payload = styled_envelope()
    result = decode_frozen_inputs(payload, render_version=7)
    assert result.render_version == 7 and result.bindings == ("matrix",)
    assert json.loads(result.style_json)["overrides"][0]["value"] == "New title"
    assert "New title" not in repr(result)
    for raw, version in ((payload, 1), (payload, 8), (payload, True),
                         (payload[:-2] + b"!}", 7), (payload+b" ", 7),
                         (styled_envelope(style=b'{"baseline":NaN}'), 7),
                         (styled_envelope(style=b'{"baseline":{},"baseline":{}}'), 7),
                         (styled_envelope(style=b" " * 65537), 7)):
        with pytest.raises(InvalidInput):
            decode_frozen_inputs(raw, render_version=version)


def test_library_requests_one_exact_resource_path_and_requires_matching_media(monkeypatch):
    from wb.frozen_inputs import MEDIA_TYPE, STYLE_MEDIA_TYPE
    from wb.library import Library, LibraryUnavailable

    library = Library("http://127.0.0.1:8060", b"x" * 32)
    seen = []
    media = STYLE_MEDIA_TYPE
    def get(path, session, *, limit):
        seen.append((path, session, limit))
        return styled_envelope(), {"content-type": media}
    monkeypatch.setattr(library, "_get", get)
    ref = "1" * 32
    assert library.frozen_inputs("fixture-session", ref, 7).render_version == 7
    assert [path for path, _, _ in seen] == [f"/_internal/workbench/frozen-bundles/{ref}/v2/7"]
    media = MEDIA_TYPE
    with pytest.raises(LibraryUnavailable):
        library.frozen_inputs("fixture-session", ref, 7)
