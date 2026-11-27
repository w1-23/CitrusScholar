# Research Integrity E4 offline runtime

The server-side integrator binds `launcher.py` as an exact, release-trusted
snapshot at `/workspace/numeric-integrity/launcher.py`. Submit through the
existing `ExecutionService` and pinned `apptainer-v1` Code Worker, with
`network_policy="offline"`, base environment, no script arguments, and
`/outputs/numeric-integrity/report.json` as the only declared output. The
launcher is not an Agent Skill, generic execute command, or Fusion Skill adapter.
Keep old generic skill and execute entries undiscoverable. Only the new typed
numeric operation should submit this script. No host or online fallback.

## Snapshot and receipt handoff

Resolve every snapshot ID and version under the authenticated run and account,
including reserved ranges, before submission. Never accept a model-provided
path, arbitrary shell or launcher bytes. Bind trusted launcher, request and
exact user-authorized data into a dedicated Workspace revision; bind sources
read-only as `/input/numeric-integrity/source-01.csv` (or `.xlsx`) and so on,
in request order. Under `/input/numeric-integrity/request.json`, emit:

```json
{
  "schema": "citrus.numeric-integrity-request/1",
  "sources": [{"id": "opaque-id", "version": 1, "sha256": "64 lowercase hex characters", "format": "csv", "size_bytes": 123, "benford_columns": []}],
  "enable_benford": false
}
```

`sha256` authenticates the bytes being scanned; if stored DatasetRef bytes are
not an intact CSV/XLSX, do not reuse the reference hash for a transformed export.
This version accepts only fully authorized whole CSV/XLSX snapshots, up to four.
Do not pass a restricted workbook as an unrestricted input. A future selection
materializer needs a trusted original-coordinate mapping to claim original A1
positions. CSV outlines refer to logical records, including the header. XLSX
positions use 1-based sheet row/column indexes. A sheet-only upstream locator
stays sheet-only. Enable Benford only with confirmed 1-based CSV columns from a
scientifically suitable measured distribution. GRIM, last digits and repetition
are not repeated by Geng. A zero-hit report is not an integrity verdict.

Accept only the existing Worker's authenticated, offline, exact-request output
receipt with the sandbox process stopped and the expected result bytes. Check
schema `citrus.numeric-integrity-report/1`, `request_sha256`, `image_review`,
source identity and release identity. A transport success may still contain a
failed scan. A caller must not publish `partial` or `not_applicable` as complete.
No raw PaperConan JSON, sample values, absolute paths, or renderer output leaves
execution. The internal Host adapter is `src.agent.numeric_integrity_worker`.
`schedule_numeric_integrity(..., operation_id=..., sources=...)` checks each
source against the run admission pin and returns a submission-only execution
ID. Keep the default bound to the current turn's admitted supported references;
never list all files in the session. For the typed submit tool, emit an internal
`_code_execution_id` and a completed submission outcome to the existing trusted
wait allowlist. On terminal wake `collect_numeric_integrity(..., execution_id=...)`
accepts only the same run's output. Neither API polls or reruns on completion.
The explicit regions parameter fails closed. If generic workspace or execute
surfaces are ever enabled again, hide the `citrus.numeric-integrity/1` scope.
Current deployment must keep old generic entries disabled.

## Build and seal

Prepare `02_config/integrity/wheelhouse/` (untracked) before building
`01_src/apptainer/sandbox.def`. Download the official SciPy, NumPy and reader
versions as already pinned for the base image. The add-on installs just the two
hashed wheels in `worker-addon.requirements.lock`, offline. The native calamine
wheel cannot use the existing pure-Python overlay builder. Do not substitute the
PyPI PaperConan 0.9.0 wheel: its `_audit.py` does not match the commit.

- Download the pinned PaperConan tarball from `https://codeload.github.com/zixixr/paperconan/tar.gz/8d116455b23e858651250d8ed23ba41c1a87ab0d` and verify the hash in `upstream-lock.json`.
- Extract only `src/`, `README.md`, `LICENSE` and `pyproject.toml` to an empty staging directory, stripping the archive's outer directory. In the existing locked cp312 Linux image with the build-tool versions in `upstream-lock.json`, run `SOURCE_DATE_EPOCH=1792335704 python -I -m pip --isolated wheel --no-index --no-deps --no-build-isolation --wheel-dir <wheelhouse> <exact-source-dir>`. Check the produced wheel hash against the lock.
- Add the exact cp312 manylinux x86_64 Python Calamine wheel from PyPI, verifying its hash. Install no optional upstream extras. Bake both wheels and the numerical worker into the newly sealed sandbox image, then record/verify the image digest through the existing Worker release mechanism. A mutable fallback on the host is not acceptable.
- Run the synthetic real-Worker tests in the sealed image and exercise authorized submit/accept before enabling the new route. Test disabled and enabled Geng and confirm an image request is rejected without dispatch. The previous sealed image remains the rollback point.
