# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve only caller-approved references and verify Host asset snapshots."""
from urllib.parse import unquote

from .bundle import InputAsset, InvalidInput, MAX_BYTES, validate_name
from .library import LibraryMissing, LibraryUnavailable

ASSET_NS = "library:"


def filename(raw):
    return validate_name(unquote(raw, errors="strict"))


def resolve_inputs(session, store, library, source):
    refs = (source.entrypoint_ref, *source.assets)
    if len(set(refs)) != len(refs) or not set(source.hashes) <= set(refs):
        raise InvalidInput("提供了重复或不匹配的导入引用。")
    if source.assets and source.kind != "script":
        raise InvalidInput("仅绘图脚本可以附加依赖。")

    def resolve(ref, role):
        if not ref.startswith(ASSET_NS):
            asset = store.input_asset(session.owner, ref)
            asset.check(source.hashes.get(ref))
            return asset, None
        raw_ref = ref[len(ASSET_NS):]
        if not raw_ref:
            raise LibraryMissing
        raw, headers = library.asset(session, raw_ref, role)
        sha = headers.get("x-asset-sha256", "")
        version = headers.get("x-asset-version", sha)
        try:
            asset = InputAsset.snapshot(filename(headers.get("x-asset-filename", "")), sha, raw)
            asset.check()
            if not 1 <= len(version) <= 120 or (role == "dependency" and version != sha):
                raise InvalidInput("无效的资料版本。")
        except ValueError:
            raise LibraryUnavailable from None
        if ref in source.hashes and source.hashes[ref] != sha:
            raise InvalidInput("所选资料版本已变更，请刷新资料库后重试。")
        return asset, version

    entry, version = resolve(source.entrypoint_ref, "source")
    dependencies = []
    total = len(entry.body)
    for ref in source.assets:
        dep, _ = resolve(ref, "dependency")
        total += len(dep.body)
        if total > MAX_BYTES:
            raise InvalidInput("脚本及其全部依赖不能超过 12 MiB。")
        dependencies.append(dep)
    return entry, tuple(dependencies), source.entrypoint_ref if version is not None else None, version
