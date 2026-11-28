# SPDX-License-Identifier: AGPL-3.0-only
"""Parse the inert Host frozen-render wire format without unpacking an archive."""
from dataclasses import dataclass, field
import json
import keyword
import re

from .bundle import InputAsset, InvalidInput, MAX_BYTES, digest, validate_bundle

HEADER_LIMIT = 8192
MEDIA_TYPE = "application/vnd.citrus.frozen-script-v1"
STYLE_MEDIA_TYPE = "application/vnd.citrus.frozen-script-v2"
STYLE_LIMIT = 64 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class FrozenInputs:
    entrypoint: InputAsset = field(repr=False)
    dependencies: tuple[InputAsset, ...] = field(repr=False)
    bindings: tuple[str, ...]
    render_version: int = 1
    style_json: bytes | None = field(default=None, repr=False)


def _valid_revision(value):
    return type(value) is int and 1 <= value <= 999_999_999


def _invalid():
    return InvalidInput("冻结渲染的数据包无效或已变更。")


def _decode_json(body):
    def unique_object(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise _invalid()
        return obj

    def reject_constant(_value):
        raise _invalid()

    try:
        return json.loads(body.decode("ascii"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise _invalid() from None


def decode_frozen_style(body):
    if not isinstance(body, bytes) or not 0 < len(body) <= STYLE_LIMIT:
        raise _invalid()
    style = _decode_json(body)
    if not isinstance(style, dict) or set(style) != {"baseline", "overrides", "overrides_sha256"}:
        raise _invalid()
    baseline = style["baseline"]
    hash_names = {"baseline_sha256", "coordinates_sha256", "targets_sha256"}
    def is_hash(value):
        return isinstance(value, str) and bool(_SHA256.fullmatch(value))
    if (not isinstance(baseline, dict) or set(baseline) != hash_names | {"version"}
            or type(baseline["version"]) is not int or baseline["version"] != 1
            or not all(is_hash(baseline[k]) for k in hash_names) or not is_hash(style["overrides_sha256"])
            or not isinstance(style["overrides"], list) or not 1 <= len(style["overrides"]) <= 200
            or any(not isinstance(item, dict) or set(item) != {"gid", "prop", "value"}
                   or not isinstance(item["gid"], str) or not isinstance(item["prop"], str)
                   for item in style["overrides"])):
        raise _invalid()
    return style


def decode_frozen_inputs(body: bytes, *, render_version: int = 1) -> FrozenInputs:
    if (not _valid_revision(render_version) or not isinstance(body, bytes)
            or len(body) < 5 or len(body) > MAX_BYTES + HEADER_LIMIT + 4):
        raise _invalid()
    size = int.from_bytes(body[:4], "big")
    cursor = 4 + size
    if not 0 < size <= HEADER_LIMIT or cursor >= len(body) or len(body) - cursor > MAX_BYTES:
        raise _invalid()
    header = _decode_json(body[4:cursor])
    fields = {"version", "language", "renderer", "inputs"}
    styled = render_version > 1
    if styled:
        fields |= {"style", "render_version"}
    if (not isinstance(header, dict) or set(header) != fields
            or type(header["version"]) is not int or header["version"] != (2 if styled else 1)
            or header["language"] != "python"
            or not isinstance(header["inputs"], list) or not 1 <= len(header["inputs"]) <= 8
            or (styled and (not _valid_revision(header["render_version"])
                            or header["render_version"] != render_version))):
        raise _invalid()

    def snapshot(meta, name, keys):
        nonlocal cursor
        if (not isinstance(meta, dict) or set(meta) != keys or type(meta["size"]) is not int
                or not 0 < meta["size"] <= len(body) - cursor):
            raise _invalid()
        end = cursor + meta["size"]
        asset = InputAsset.snapshot(name, meta["sha256"], body[cursor:end])
        asset.check()
        cursor = end
        return asset

    entrypoint = snapshot(header["renderer"], "render.py", {"size", "sha256"})
    try:
        if not entrypoint.body.decode("utf-8").strip() or b"\x00" in entrypoint.body:
            raise _invalid()
    except UnicodeError:
        raise _invalid() from None
    deps, bindings = [], []
    for i, meta in enumerate(header["inputs"]):
        name = f"input_{i}.json"
        binding = meta.get("binding") if isinstance(meta, dict) else None
        if (not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", binding)
                or keyword.iskeyword(binding) or binding in bindings or meta.get("name") != name):
            raise _invalid()
        deps.append(snapshot(meta, name, {"binding", "name", "size", "sha256"}))
        bindings.append(binding)
    style = None
    if styled:
        meta = header["style"]
        style = body[cursor:]
        if (not isinstance(meta, dict) or set(meta) != {"size", "sha256"} or type(meta["size"]) is not int
                or not 0 < meta["size"] == len(style) <= STYLE_LIMIT or meta["sha256"] != digest(style)):
            raise _invalid()
        decode_frozen_style(style)
        cursor += len(style)
    if cursor != len(body):
        raise _invalid()
    validate_bundle(entrypoint, tuple(deps))
    return FrozenInputs(entrypoint, tuple(deps), tuple(bindings), render_version, style)
