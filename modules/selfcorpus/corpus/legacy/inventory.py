"""Validate the immutable old inventory without discovery or writes."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType


class LegacyUnavailable(ValueError):
    """The trusted legacy inventory cannot satisfy this operation."""


class LegacyChanged(LegacyUnavailable):
    """An input is no longer the incarnation pinned by this snapshot."""


SOURCES = MappingProxyType({
    'cqa_deng_book': 'deng_variety_book', 'cqa_literature': 'literature',
    'cqa_methods': 'experiment_methods', 'cqa_translated': 'literature',
    'cqa_ucr_varieties': 'ucr_varieties',
})
_GENERATION = re.compile(r'g[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}')
_SHA256 = re.compile(r'[a-f0-9]{64}')


def digest(raw):
    return sha256(raw).hexdigest()


def identity(*parts, length=32):
    # Native Citrus QA Agent corpus_registry.deterministic_id (MIT).
    # The legacy implementation maps numeric ordinal 0 to an empty string.
    return digest('\0'.join(str(part or '').strip() for part in parts).encode())[:length]


@dataclass(frozen=True)
class ApprovedInventory:
    manifest_sha256: str
    registry_sha256: str
    chunk_hashes: dict[str, str]

    def __post_init__(self):
        if (self.chunk_hashes.keys() != SOURCES.keys()
                or not all(_SHA256.fullmatch(h) for h in (
                    self.manifest_sha256, self.registry_sha256, *self.chunk_hashes.values()))):
            raise ValueError('invalid_approved_legacy_inventory')
        object.__setattr__(self, 'chunk_hashes', MappingProxyType(dict(self.chunk_hashes)))

    @property
    def fingerprint(self):
        return digest('\n'.join([self.manifest_sha256, self.registry_sha256, *(
            f'{k}:{v}' for k, v in sorted(self.chunk_hashes.items()))]).encode())


PRODUCTION = MappingProxyType({'g20261028T165637Z_851926a9': ApprovedInventory(
    '319c3baf29211472358370a768e75ca80c11d5b8e3dfdf6a3136078e7073a415',
    'da0367d6ff96b50bba78b3855d1190824ba2dfdd932303e767e32b6038a323ee',
    {
        'cqa_deng_book': '6c2c004dc7de41fb5002a5275a4b796d305ec080700ba5f00e2832f80af12c2c',
        'cqa_literature': 'ce7ecec9b51288693df79d0d7564d52ea3fd8fca4f17fbc3fc25278b17e14717',
        'cqa_methods': 'f86d5687b83800b77758e96030a3a408001ddf000bd927688e3f45a0bc0164e3',
        'cqa_translated': '0f5823421b881ef5960f80010aea4892fe6cc718174021624b6c7d5ea155a10d',
        'cqa_ucr_varieties': 'd70013e82b9106fd067fe0bd8e853c6c1d4f330b5e20d172df88e4635e812fd8',
    },
)})


def stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class Mount:
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))

    @contextmanager
    def directory(self, parts):
        with ExitStack() as stack:
            fd = os.open(self.root, _FLAGS | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            for part in parts:
                fd = os.open(part, _FLAGS | os.O_DIRECTORY, dir_fd=fd)
                stack.callback(os.close, fd)
            yield fd

    @contextmanager
    def open(self, relative):
        parts = Path(relative).parts
        with self.directory(parts[:-1]) as parent:
            fd = os.open(parts[-1], _FLAGS, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                current = os.fstat(fd)
                if not stat.S_ISREG(current.st_mode):
                    raise LegacyUnavailable('legacy_not_regular_file')
                yield stream, stamp(current)

    def signature(self, relative):
        with self.open(relative) as (_, signature):
            return signature

    def record(self, relative):
        with self.open(relative) as (stream, signature):
            data = stream.read(1_048_577)
            if len(data) > 1_048_576 or stamp(os.fstat(stream.fileno())) != signature:
                raise LegacyUnavailable('legacy_inventory_unstable')
        try:
            value = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise LegacyUnavailable('legacy_inventory_invalid') from exc
        return value, data, signature


def unique_object(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError('duplicate_legacy_key')
    return row


@dataclass(frozen=True)
class Source:
    source_id: str
    physical: str
    family: str
    path: str
    count: int
    hash: str


def _sources(registry, prefix, allow, implicit_public):
    rows = registry.get('corpora')
    if not isinstance(rows, dict) or len(rows) != len(SOURCES):
        raise LegacyUnavailable('legacy_source_inventory_mismatch')
    access = registry.get('corpus_access', {})
    if not isinstance(access, dict) or not access.keys() <= rows.keys():
        raise LegacyUnavailable('legacy_registry_contract_mismatch')
    sources = {}
    for physical, row in rows.items():
        if not isinstance(row, dict):
            raise LegacyUnavailable('legacy_registry_contract_mismatch')
        source_id = row.get('source_batch')
        family = SOURCES.get(source_id) if isinstance(source_id, str) else None
        if (family is None or source_id in sources or row.get('source') != source_id
                or row.get('family_id') != family
                or physical not in (source_id, 'corpus_' + identity(family, source_id, length=24))
                or row.get('table_name') != physical or row.get('batch') != physical):
            raise LegacyUnavailable('legacy_source_identity_mismatch')
        declared = [record[k] for record, k in ((row, 'access_level'), (access, physical)) if k in record]
        if any(state != 'public' for state in declared) or (not declared and not implicit_public):
            raise LegacyUnavailable('legacy_source_access_mismatch')
        path = f'{prefix}/corpora/{physical}/chunks.jsonl'
        count = row.get('chunk_count')
        if row.get('chunks_path') != path or type(count) is not int or count < 0:
            raise LegacyUnavailable('legacy_source_inventory_mismatch')
        sources[source_id] = Source(source_id, physical, family, path, count, allow.chunk_hashes[source_id])
    return sources


@dataclass
class Inventory:
    mount: Mount
    generation: str
    fingerprint: str
    aliases: dict
    sources: dict[str, Source]
    stamps: dict

    @classmethod
    def load(cls, mount, generation, approved, *, active=False):
        try:
            return cls._load(mount, generation, approved, active)
        except OSError as exc:
            raise LegacyUnavailable('legacy_mount_unavailable') from exc

    @classmethod
    def _load(cls, mount, generation, approved, active):
        if active:
            pointer, raw_pointer, pointer_stamp = mount.record('active_index.json')
            generation = pointer.get('generation')
        if not isinstance(generation, str) or not _GENERATION.fullmatch(generation) or generation not in approved:
            raise LegacyUnavailable('legacy_generation_unapproved')
        allow = approved[generation]
        prefix = f'corpus_generations/{generation}'
        manifest_path = f'{prefix}/index_manifest.json'
        registry_path = f'{prefix}/corpus_registry.json'
        manifest, raw_manifest, manifest_stamp = mount.record(manifest_path)
        registry, raw_registry, registry_stamp = mount.record(registry_path)
        if digest(raw_manifest) != allow.manifest_sha256 or digest(raw_registry) != allow.registry_sha256:
            raise LegacyUnavailable('legacy_inventory_hash_mismatch')
        if (manifest.get('generation') != generation or manifest.get('generation_root') != prefix
                or manifest.get('registry_path') != registry_path or manifest.get('index_schema_version') != 2
                or manifest.get('lancedb_path') != f'lancedb_generations/{generation}'
                or not registry.get('registry_version') or manifest.get('registry_version') != registry['registry_version']
                or registry.get('schema_version') != 1 or manifest.get('table_count') != len(SOURCES)):
            raise LegacyUnavailable('legacy_manifest_contract_mismatch')
        aliases = registry.get('topic_aliases', {})
        if (not isinstance(aliases, dict)
                or not all(k.strip() and isinstance(v, str) and v.strip() for k, v in aliases.items())):
            raise LegacyUnavailable('legacy_registry_contract_mismatch')
        sources = _sources(registry, prefix, allow, allow is PRODUCTION.get(generation))
        if manifest.get('chunk_count') != sum(s.count for s in sources.values()):
            raise LegacyUnavailable('legacy_source_count_mismatch')
        stamps = {manifest_path: manifest_stamp, registry_path: registry_stamp}
        stamps.update((s.path, mount.signature(s.path)) for s in sources.values())
        if active:
            if raw_pointer != raw_manifest:
                raise LegacyUnavailable('legacy_active_pointer_mismatch')
            stamps['active_index.json'] = pointer_stamp
        inventory = cls(mount, generation, allow.fingerprint, aliases, sources, stamps)
        inventory.check_current()
        return inventory

    def check_current(self):
        try:
            unchanged = all(self.mount.signature(p) == s for p, s in self.stamps.items())
        except (OSError, LegacyUnavailable) as exc:
            raise LegacyChanged('legacy_snapshot_changed') from exc
        if not unchanged:
            raise LegacyChanged('legacy_snapshot_changed')
