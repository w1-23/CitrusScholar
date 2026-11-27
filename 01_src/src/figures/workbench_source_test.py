from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.figures import store
from src.figures.dataset_codec import encode_frame
from src.figures.projects import ProjectError
from src.figures.workbench_source import prepare_frozen_bundle
from src.research.script_execution import _accept_frozen_render
from src.session import files


def test_workbench_bundle_reads_only_the_frozen_source_and_exact_registered_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.sqlite3")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    import hashlib
    from contextlib import nullcontext
    from src.core import account_leases
    from src.research import analysis_results
    monkeypatch.setattr(account_leases, "account_lease", lambda owner: nullcontext())
    owner, sid = 97011, "frozen-workbench"
    text = "fig, ax = plt.subplots()\nax.bar(data['tidy']['x'], data['tidy']['y'])\n"
    payload = json.dumps(encode_frame(pd.DataFrame({"x": [1, 2], "y": [3, 4]})), ensure_ascii=False).encode()
    entry = files.register_file(session_id=sid, owner_user_id=owner, filename="table.json",
                                content=payload, mime="application/json", visibility="internal")
    digest = hashlib.sha256(payload).hexdigest()
    script_sha = hashlib.sha256(text.encode()).hexdigest()

    class Dataset:
        ref = {"dataset_id": "fixture"}

        def to_dict(self):
            return {"session_id": sid, "payload_file_ref": entry["file_id"]}

    monkeypatch.setattr(analysis_results, "load_dataset_snapshot", lambda *args: Dataset())
    figure = {"name": "bars", "title": "Bars", "formats": ["png"], "parents": ["tidy"], "render_script": text}
    contract = {"version": 2, "tables": [{"name": "tidy", "parents": ["raw"]}], "figures": [figure]}
    receipt = {"version": 1, "renderer_sha256": script_sha, "data_sha256": {"tidy": digest},
               "geometry_status": "supported", "geometry_sha256": "1" * 64, "geometry_unsupported": []}
    frozen, _ = _accept_frozen_render(figure, {"frozen_render_receipt": receipt}, {"tidy": Dataset.ref},
                                      {"tidy": digest}, owner=owner, session_id=sid)
    record = store.create_project(owner_user_id=owner, source_kind="script", session_id=sid, source={
        "output_contract": contract, "output_key": "bars", "frozen_render": frozen})
    key = record["figure_id"]
    preview = files.register_file(session_id=sid, owner_user_id=owner, filename="preview.png",
                                  content=b"fixture static preview", mime="image/png")
    assert store.update_project(key, owner, expected_revision=0, render_version=1, exports_json={"png": {
        "file_id": preview["file_id"]}})
    bundle = prepare_frozen_bundle(figure_id=key, owner=owner, session=sid)
    assert bundle.renderer == text and bundle.renderer_sha256 == script_sha
    [table] = bundle.inputs
    assert (table.binding, table.filename, table.sha256, table.content) == ("tidy", "input_0.json", digest, payload)
    for wrong_owner, wrong_sid in ((owner + 1, sid), (owner, "other"), (owner, "")):
        with pytest.raises(ProjectError) as error:
            prepare_frozen_bundle(figure_id=key, owner=wrong_owner, session=wrong_sid)
        assert error.value.status == 404
    ref = frozen["input_snapshots"][0]["file_id"]
    stored = files.get_account_file(ref, owner, allow_internal=True, touch=False)
    Path(stored["path"]).write_bytes(b"!" + payload[1:])
    with pytest.raises(ProjectError) as error:
        prepare_frozen_bundle(figure_id=key, owner=owner, session=sid)
    assert error.value.status == 409
