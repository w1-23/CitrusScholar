# 8043 release staging gate

`04_scripts/stage_8043_release.py` stages a new immutable release directory. It does not launch Apptainer, change a service, rewrite a symlink, migrate runtime data, or replace a prior release. Run it as the unprivileged release owner from the Linux host. Use canonical absolute paths, a new name below the stable releases directory, a full committed Git revision, and independently recorded artifact SHA-256 pins.

```bash
python3 -B 04_scripts/stage_8043_release.py baseline-check \
  --baseline-deployment "$previous/deployment.json"

python3 -B 04_scripts/stage_8043_release.py stage \
  --repository "$repo" --commit "$commit" \
  --baseline-deployment "$previous/deployment.json" \
  --sandbox-sif "$sandbox_sif" --sandbox-sha256 "$sandbox_sha256" \
  --skill-release "$skills" --skill-release-sha256 "$skills_sha256" \
  --output "$release"

python3 -B 04_scripts/stage_8043_release.py verify \
  --release "$release" --repository "$repo" --manifest-sha256 "$manifest_sha256"

python3 -B "$release/launch.py" web --preflight
python3 -B "$release/launch.py" worker --preflight
```

Save the reported `manifest_sha256` and `source_archive_sha256` outside the candidate release with the accepted Git revision. Checking the separately recorded manifest pin and recreating the **whole** Git archive prevents two self-consistent but different artifacts from being treated as the same candidate. Sealed paths are deliberate, so do not rename or move the release directory. The wrapper preflights build both commands with existing config and verify the image pins without actually running them. Use an independently configured isolated deployment for integration tests that launch Web or Worker. Nothing in this gate authorizes a switch.

## What the gate proves

- `source.tar` is uncompressed `git archive` for the exact full revision. Git file names and executable modes must agree with the archive. The live checkout's dirty or untracked bytes do not enter the release.
- `source/` is the exact archive tree except `LINUX-SOURCE-MANIFEST.json`, regenerated from the archived members and candidate revision. Its hashes, byte counts, and executable flags are verified. `source-sha256.json` separately includes the regenerated manifest. The release manifest pins the whole archive, full inventory, deployment configuration, and generated launcher.
- The verified App, R sandbox, and namespace SIFs are independent copies under release-owned `images/`. The active Skill tree is copied under `skills-release/` and checked against its pinned full inventory. The 14 shared Nature references must match their aliases byte for byte, and the helper tree must not appear as another Skill.
- Prior Skill records keep their exact physical paths and original read-only mount points. The previously active Skill tree is added to that list at its original path. Do not delete or move any historical or mounted tree, including `skills-final-r2`, even after new staging. Existing reports and parked jobs can depend on these paths.
- Runtime, job staging, model, browse, and corpus configuration are inherited from the baseline. No origin model file contents are included in the release. The Web command retains the two specific read-only origin-file mounts for model configuration and provider keys, and Worker does not receive those mounts.
- Candidate configuration requires the external Fusion Skill bundle. A fresh output path is mandatory. If staging fails after creation, the output is not an accepted release. Keep failure evidence separate and retry with a new path. Do not clean the rollback or any currently mounted directory.

The production switch and rollback are independent operational steps. Keep the old deployment and its launcher intact. This gate pins their files but neither changes a live unit nor resets runtime data. A real rollback uses the previously retained launch and service settings, not a copy of a current database or index.

## What public CI covers

The candidate branch and pull requests run the Git archive and release contract fixtures with synthetic images, source trees, and model placeholders. The Python and Node gates check local artifact, session, rendering, and UI contracts without production state. Public CI does not fetch private SIFs or use production credentials. Run the staging fixtures on the host with Git available, not inside the App SIF, which intentionally does not include Git. Run release and launcher preflights on the authorized Linux server before any production switch.

The standalone Skill lock job stages its pinned packages, then compares their exact contents to `packages/`. The sole byte divergence is `citrus_audit/workbook.py`, with upstream SHA-256 `93aa851610d0ce557917eaec298374bd98cedc311c5b7b456ab824144c093070` and host SHA-256 `67514b1f021f4869e1c4b850a93a4d5d5ea4ee05b214ba674da1004d19541df9`. The host-version review exporter validates XLSX ZIP bytes and preserves exact annotation locations, prior notes, cross-target summary entries, and correctly quoted sheet links. The gate rejects drift on both sides of this overlay, as well as unapproved files or byte differences in either package. Any overlay change must review the exact diff and update the relevant SHA deliberately.
