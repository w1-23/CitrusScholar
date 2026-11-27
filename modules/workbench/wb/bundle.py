# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable, bounded script inputs. Never extract archives or resolve client paths."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath, PureWindowsPath
import unicodedata
from uuid import uuid4

MAX_BYTES = 12 * 1024 * 1024
MAX_DEPENDENCIES = 32
DEPENDENCY_FORMATS = frozenset({"csv", "tsv", "json", "txt", "xlsx", "parquet", "npy"})


class InvalidInput(ValueError):
    pass


def digest(body: bytes) -> str:
    return sha256(body).hexdigest()


def validate_name(name: str) -> str:
    if (not isinstance(name, str) or not 1 <= len(name) <= 240 or len(name.encode("utf-8")) > 240
            or name != name.strip() or name in {".", ".."} or name.endswith(".")
            or any(ch in "/\\:" or unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in name)
            or PureWindowsPath(name).is_reserved()):
        raise InvalidInput("文件名无效：请使用单个普通文件名，不含目录。")
    return name


@dataclass(frozen=True, slots=True)
class InputAsset:
    ref: str
    name: str
    sha: str
    body: bytes
    external: bool = False

    @classmethod
    def snapshot(cls, name, sha, body):
        return cls(f"asset_{uuid4().hex}", name, sha, body, external=True)

    def check(self, expected=None):
        validate_name(self.name)
        if (not isinstance(self.body, bytes) or not 0 < len(self.body) <= MAX_BYTES
                or self.sha != digest(self.body) or (expected is not None and self.sha != expected)):
            raise InvalidInput("文件大小、内容校验值或选择版本不符。")

    def pin(self):
        return {"ref": self.ref, "name": self.name, "sha": self.sha, "size": len(self.body)}


def validate_bundle(source: InputAsset, dependencies: tuple[InputAsset, ...]):
    source.check()
    if not dependencies:
        return
    if len(dependencies) > MAX_DEPENDENCIES:
        raise InvalidInput("最多可指定 32 个依赖文件。")
    names = {unicodedata.normalize("NFC", source.name.casefold())}
    refs = {source.ref}
    size = len(source.body)
    for asset in dependencies:
        asset.check()
        if PurePosixPath(asset.name).suffix[1:].lower() not in DEPENDENCY_FORMATS:
            raise InvalidInput("脚本依赖须是支持的数据文件。")
        key = unicodedata.normalize("NFC", asset.name.casefold())
        if key in names or asset.ref in refs:
            raise InvalidInput("脚本和依赖的文件名或引用不能重复。")
        names.add(key)
        refs.add(asset.ref)
        size += len(asset.body)
        if size > MAX_BYTES:
            raise InvalidInput("脚本及其全部依赖不能超过 12 MiB。")


def bundle_digest(assets, language, runtime):
    inputs = [(a.name, a.sha, len(a.body)) for a in assets]
    payload = ["workbench-source-bundle/1", language, runtime, inputs[0], sorted(inputs[1:])]
    return digest(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode())
