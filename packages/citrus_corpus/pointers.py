"""Durable atomic generation pointers."""
import json
import os
from pathlib import Path
import tempfile

from .generations import valid_generation


def _sync_dir(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_pointer(root, name="active"):
    path = root / (name + "_index.json")
    try:
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or not valid_generation(record.get("generation")):
        raise ValueError("shared_index_pointer_invalid")
    return record["generation"]


def write_pointer(root, generation, name="active"):
    path = root / (name + "_index.json")
    if generation is None:
        path.unlink(missing_ok=True)
        _sync_dir(root)
        return
    if not valid_generation(generation):
        raise ValueError("shared_generation_invalid")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
        staging = Path(stream.name)
        try:
            json.dump({"generation": generation}, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(staging, path)
            _sync_dir(root)
        finally:
            staging.unlink(missing_ok=True)
