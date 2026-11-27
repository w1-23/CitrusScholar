"""Immutable v1 release and bytes-only protocol for host-owned fusion jobs."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from src.code_execution.protocol import identifier, logical_path
from .fusion_worker_entry import ENTRYPOINT, REQUIREMENTS, SKILLS, locked_dependencies
from .installed_skills import list_installed, read_approved_script
from .research_skill_catalog import _MAX_RESOURCE_BYTES, _bytes, _path

BUNDLE_ROOT = "/input/fusion/"
INPUT_ROOT = "/input/fusion-data/"
REQUEST = "/input/fusion-request.json"
OUTPUT = "/outputs/result.json"
LAUNCHER = "/workspace/fusion-run.py"
SCOPE = "citrus.fusion-worker/1"
MAX_REQUEST = 1 << 20
MAX_INPUT = 32 << 20
MAX_TOTAL_INPUT = 96 << 20
MAX_BUNDLE = 8 << 20
MAX_MEMBERS = 94
REPORT_KEYS = frozenset({"findings", "evidence", "locations", "coverage", "limitations", "artifacts"})
OPTION_KEYS = {
    "literature-verification": frozenset({"references", "strict"}),
    "data-audit": frozenset({"regions", "experimental_design", "csv_table"}),
    "research-integrity": frozenset({"checks", "numeric_columns", "shared_control_columns", "digit_screen",
                                     "shared_image_pairs", "tolerance", "gel_lanes"}),
}
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
WORKBOOK_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
CSV_TABLE_EXTENSIONS = frozenset({".csv"})
CSV_TABLE_ROLE = "csv_table"
WORKBOOK_ROLE = "workbook"
# The two formal data-audit input protocols. A CSV source is described as a
# table with logical records and fields; it never invents a worksheet name and
# is never wrapped into a workbook to satisfy a workbook-only contract.
DATA_AUDIT_INPUT_KINDS = {"excel_workbook": WORKBOOK_ROLE, "csv_table": CSV_TABLE_ROLE}
# A pinned data-audit analyzer version accepts the csv_table role only when the
# release actually ships that adapter. Empty means "not installed": the host
# must route the exact table contract to the generic audit path instead of
# starting a Worker job the analyzer cannot read.
CSV_TABLE_ANALYZER_VERSIONS = {"data-audit": frozenset()}
CSV_DELIMITERS = (",", "\t", ";", "|")
MAX_CSV_RECORDS = 5001
MAX_CSV_CELLS = 250_000
EXCEL_WORKBOOK_FORMULA_POLICY = {"formula_values": "cached", "recalculate": False,
                                 "missing_cache": "coverage_finding",
                                 "macro_safety": "reject_signed_activex_embedded"}
EXTENSIONS = {"bibliography": {".pdf", ".bib"}, "workbook": WORKBOOK_EXTENSIONS,
              CSV_TABLE_ROLE: CSV_TABLE_EXTENSIONS,
              "table": {".csv"}, "source": {".csv", ".tsv", ".xlsx"},
              "reported": {".csv", ".tsv", ".xlsx"}, "gel": IMAGE_EXTS}
ROLES = {"literature-verification": {"bibliography"},
         "data-audit": {WORKBOOK_ROLE, CSV_TABLE_ROLE},
         "research-integrity": {"table", "source", "reported", "gel"}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def decode(data):
    def unique(pairs):
        out = {}
        for key, val in pairs:
            if key in out:
                raise ValueError("fusion_duplicate_json_key")
            out[key] = val
        return out

    def nonfinite(_):
        raise ValueError("fusion_nonfinite_json")

    return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class Bundle:
    skill: str
    installed_version: str
    version: str
    release_sha256: str
    sha256: str
    launcher: bytes
    members: dict[str, bytes]
    lock_sha256: str
    pins: dict[str, str]


def load_bundle(release: Path, skill: str) -> Bundle:
    if not isinstance(skill, str) or skill not in SKILLS:
        raise ValueError("fusion_skill_unknown")
    installed = next((item for item in list_installed(release) if item["id"] == skill), None)
    if not installed:
        raise ValueError("fusion_skill_not_installed")
    launcher, _ = read_approved_script(release, skill, installed["version"], "scripts/run.py")
    trusted = Path(__file__).with_name("fusion_worker_entry.py").read_bytes()
    if launcher != f"# Citrus approved fusion skill: {skill}\n".encode() + trusted:
        raise ValueError("fusion_launcher_not_supported")
    inventory_raw = _bytes(_path(release, "inventory.json"), _MAX_RESOURCE_BYTES)
    raw = _bytes(_path(release, "fusion/bundle.json"), _MAX_RESOURCE_BYTES)
    inventory = decode(inventory_raw)
    bundle_digest = sha(raw)
    if type(inventory) is not dict or inventory.get("fusion/bundle.json") != bundle_digest:
        raise ValueError("fusion_bundle_inventory_mismatch")
    manifest = decode(raw)
    if (type(manifest) is not dict or set(manifest) != {"schema", "entrypoint", "skills", "files", "dependencies"}
            or manifest["schema"] != "citrus.fusion-bundle/1" or manifest["entrypoint"] != ENTRYPOINT
            or type(manifest["skills"]) is not dict or not set(manifest["skills"]) <= SKILLS
            or any(version not in {
                "data-audit": {"0.1.0", "0.1.1", "0.1.2", "0.1.3"},
                "literature-verification": {"0.1.0", "0.1.1", "0.1.2"},
                "research-integrity": {"0.1.0", "0.1.1"},
            }[name] for name, version in manifest["skills"].items())
            or skill not in manifest["skills"]):
        raise ValueError("fusion_bundle_schema_mismatch")
    members = manifest["files"]
    manifest_path = f"03_skill_fusion/{skill}/manifest.json"
    minimum = {ENTRYPOINT, REQUIREMENTS, "03_skill_fusion/contract.py", "03_skill_fusion/sandbox.py",
               "upstreams/sources.json", manifest_path, f"03_skill_fusion/{skill}/scripts/adapter.py"}
    if type(members) is not dict or not minimum <= members.keys() or len(members) > MAX_MEMBERS:
        raise ValueError("fusion_bundle_members_invalid")
    payloads, total = {"bundle.json": raw}, len(raw)
    for name, expected in sorted(members.items()):
        logical_path(BUNDLE_ROOT + name)
        release_path = "fusion/" + name
        if name == "bundle.json" or not is_hash(expected) or inventory.get(release_path) != expected:
            raise ValueError("fusion_bundle_inventory_mismatch")
        content = _bytes(_path(release, release_path), _MAX_RESOURCE_BYTES)
        total += len(content)
        if total > MAX_BUNDLE or sha(content) != expected:
            raise ValueError("fusion_bundle_member_integrity_failed")
        payloads[name] = content
    dep = manifest["dependencies"]
    lock_digest = sha(payloads[REQUIREMENTS])
    if dep != {"python": "3.12", "platform": "linux-x86_64", "system": ["libseccomp.so.2"],
               "requirements": REQUIREMENTS, "sha256": lock_digest}:
        raise ValueError("fusion_bundle_dependency_mismatch")
    analyzer = decode(payloads[manifest_path])
    if (type(analyzer) is not dict or analyzer.get("name") != skill
            or type(analyzer.get("interface_version")) is not int or analyzer["interface_version"] != 1
            or analyzer.get("version") != manifest["skills"][skill]):
        raise ValueError("fusion_analyzer_manifest_mismatch")
    return Bundle(skill, installed["version"], analyzer["version"], sha(inventory_raw), bundle_digest,
                  launcher, payloads, lock_digest, locked_dependencies(payloads[REQUIREMENTS].decode()))


def input_path(skill, role, filename):
    if type(role) is not str:
        raise ValueError("fusion_input_role_invalid")
    ext = Path(filename).suffix.lower()
    if role in ROLES[skill]:
        allowed = EXTENSIONS[role]
    elif skill == "research-integrity" and re.fullmatch(r"image_[A-Za-z0-9_-]{1,50}", role):
        allowed = IMAGE_EXTS
    else:
        raise ValueError("fusion_input_role_invalid")
    if ext not in allowed:
        raise ValueError("fusion_input_format_invalid")
    return INPUT_ROOT + role + ext


def data_audit_input_kind(filename):
    """The formal data-audit protocol this authorized file belongs to.

    ``None`` means neither protocol: the caller must report an unsupported
    format instead of guessing a worksheet or wrapping the file.
    """
    suffix = Path(str(filename)).suffix.lower()
    if suffix in WORKBOOK_EXTENSIONS:
        return "excel_workbook"
    if suffix in CSV_TABLE_EXTENSIONS:
        return "csv_table"
    return None


def _csv_records(data):
    """Deterministic, bounded parse of one logical CSV table."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("fusion_csv_table_encoding_invalid") from exc
    if "\x00" in text:
        raise ValueError("fusion_csv_table_binary_content")
    lines = text.splitlines()
    best = None
    for delimiter in CSV_DELIMITERS:
        try:
            rows = [row for row in csv.reader(lines, delimiter=delimiter, strict=True) if row]
        except csv.Error:
            continue
        if not rows:
            continue
        widths = [len(row) for row in rows[:20]]
        # The widest consistent table wins: a single-column file cannot outbid
        # the delimiter that actually splits its header.
        modal = max(sorted(set(widths)), key=lambda width: (widths.count(width), width))
        score = (modal, widths.count(modal))
        if best is None or score > best[0]:
            best = (score, delimiter, rows)
    if best is None:
        raise ValueError("fusion_csv_table_invalid")
    return best[1], best[2]


def csv_table_contract(data, filename):
    """Describe one authorized CSV source as a single table, never a workbook.

    Columns, record positions and the source hash are reported exactly as they
    appear. Blank or duplicate header fields are recorded as defects; none of
    them is renamed and no worksheet name is manufactured.
    """
    from .research_skill_catalog import _MAX_RESOURCE_BYTES

    if not isinstance(data, bytes) or not data.strip():
        raise ValueError("fusion_csv_table_empty")
    if Path(str(filename)).suffix.lower() not in CSV_TABLE_EXTENSIONS:
        raise ValueError("fusion_csv_table_format_invalid")
    if data[:2] == b"PK":
        raise ValueError("fusion_csv_table_is_a_workbook")
    if len(data) > _MAX_RESOURCE_BYTES:
        raise ValueError("fusion_csv_table_size_limit")
    delimiter, records = _csv_records(data)
    header, body = records[0], records[1:]
    if len(body) > MAX_CSV_RECORDS:
        raise ValueError("fusion_csv_table_record_limit")
    width = max([len(header), *(len(row) for row in body)])
    if width < 1 or width > 16384 or (len(body) + 1) * width > MAX_CSV_CELLS:
        raise ValueError("fusion_csv_table_cell_limit")
    columns = [str(name) for name in header]
    return {
        "kind": "csv_table",
        "source_sha256": sha(data),
        "filename": Path(str(filename)).name,
        "table": "single",
        "encoding": "utf-8-sig",
        "delimiter": "tab" if delimiter == "\t" else delimiter,
        "header_rows": [1],
        "column_count": len(columns),
        "column_names": columns,
        "record_count": len(body),
        "data_range": {"first_row": 2, "last_row": len(body) + 1,
                       "first_field": 1, "last_field": len(columns)},
        "positions": "logical records and fields",
        "sheet_name": None,
        "header_defects": {"blank_fields": [index + 1 for index, name in enumerate(columns) if not name.strip()],
                           "duplicate_names": sorted({name for name in columns
                                                      if name.strip() and columns.count(name) > 1})},
        "ragged_records": sum(1 for row in body if len(row) != len(columns)),
    }


def excel_workbook_contract(sheet_names, regions):
    """Describe an authorized workbook's reviewed A1 rectangles.

    Only the confirmed rectangles are read; every other sheet and every cell
    outside them is named as unreviewed instead of being claimed as checked.
    """
    names = [str(name) for name in sheet_names]
    reviewed = {}
    for region in regions:
        reviewed.setdefault(region["sheet"], []).append({"range": region["bounds"],
                                                         "header_rows": list(region["header_rows"])})
    return {"kind": "excel_workbook", "sheets": names, "coordinates": "A1",
            "reviewed_sheets": [name for name in names if name in reviewed],
            "unreviewed_sheets": [name for name in names if name not in reviewed],
            "regions": [{"sheet": name, "range": item["range"], "header_rows": item["header_rows"]}
                        for name in names for item in reviewed.get(name, [])],
            "unreviewed_scope": "以确认矩形之外的单元格和未列出的工作表均未审查",
            "formula_policy": dict(EXCEL_WORKBOOK_FORMULA_POLICY)}


def analyzer_version(release, skill):
    """Read the pinned analyzer version of one installed Skill release."""
    from .research_skill_catalog import _MAX_RESOURCE_BYTES, _bytes, _path

    raw = decode(_bytes(_path(Path(release), "fusion/bundle.json"), _MAX_RESOURCE_BYTES))
    if type(raw) is not dict or type(raw.get("skills")) is not dict or type(raw["skills"].get(skill)) is not str:
        raise ValueError("fusion_bundle_schema_mismatch")
    return raw["skills"][skill]


def csv_table_supported(skill, version):
    """Whether the pinned analyzer accepts the formal csv_table input role."""
    return str(version) in CSV_TABLE_ANALYZER_VERSIONS.get(skill, frozenset())


def csv_data_audit_route(skill, release):
    """One contract for both the tool dispatch and the runtime capability table."""
    if skill != "data-audit" or not release:
        return "unavailable"
    return "worker" if csv_table_supported(skill, analyzer_version(Path(release), skill)) else "generic"


def csv_table_options(options, refs):
    """Validate csv_table options and bind them to the authorized file hash."""
    table = options.get("csv_table")
    if type(table) is not dict or not table.keys() <= {"header_rows", "column_names", "source_sha256"}:
        raise ValueError("fusion_csv_table_options_invalid")
    if "regions" in options:
        raise ValueError("fusion_csv_table_regions_not_allowed")
    if table.get("header_rows") not in (None, [1]):
        raise ValueError("fusion_csv_table_header_invalid")
    names = table.get("column_names")
    if names is not None and (type(names) is not list or not 1 <= len(names) <= 16384
                              or any(type(name) is not str for name in names)):
        raise ValueError("fusion_csv_table_columns_invalid")
    digest = table.get("source_sha256")
    if digest is not None and (not is_hash(digest) or digest != refs[CSV_TABLE_ROLE]["sha256"]):
        raise ValueError("fusion_csv_table_source_mismatch")
    return table


def validate_reference(ref):
    if type(ref) is not dict or set(ref) != {"file_id", "sha256"} or not is_hash(ref["sha256"]):
        raise ValueError("fusion_input_reference_invalid")
    identifier(ref["file_id"])


def workbook_regions(regions):
    """Check the same bounded A1 rectangles at the tool and worker boundary."""
    from citrus_audit.readers import _region_box, normalize_regions

    if type(regions) is not list or not 1 <= len(regions) <= 16:
        raise ValueError("fusion_workbook_region_count_invalid")
    mapped, ids = [], set()
    for region in regions:
        if type(region) is not dict or not {"id", "sheet", "range"} <= region.keys() or not region.keys() <= {
                "id", "sheet", "range", "header_row", "rules", "experimental_design"}:
            raise ValueError("fusion_workbook_region_shape_invalid")
        key, sheet = region["id"], region["sheet"]
        if not isinstance(key, str) or not key or len(key) > 160 or key != key.strip() or key in ids:
            raise ValueError("fusion_workbook_region_id_invalid")
        if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > 31:
            raise ValueError("fusion_workbook_sheet_invalid")
        ids.add(key)
        rules = region.get("rules", [])
        if type(rules) is not list or len(rules) > 64:
            raise ValueError("fusion_workbook_rule_count_invalid")
        left, top, right, bottom = _region_box(region["range"])
        if (right - left + 1) * (bottom - top + 1) > 50_000:
            raise ValueError("fusion_workbook_region_size_limit")
        mapped.append({"sheet": sheet, "bounds": region["range"], "region_id": key,
                       "header_rows": [region.get("header_row", top)]})
    return normalize_regions(mapped, limit=16)


def confirmed_workbook_regions(regions, query):
    """An offered/guessed region is not authority; require the user's exact coordinates."""
    from citrus_audit.readers import _region_box
    from src.graph.intent_text import mask_literal_payload

    normalized = workbook_regions(regions)
    text = mask_literal_payload(query)
    clauses = re.split(r"[。！？?;；\n]", text)
    next_region = re.compile(r"!\s*\$?[A-Z]{1,3}\$?[1-9]\d{0,6}", re.I | re.ASCII)
    for original, region in zip(regions, normalized):
        sheet = original["sheet"]
        bounds = re.sub(r"([A-Z]+)([0-9]+)", lambda m: rf"\$?{m[1]}\$?{m[2]}", region["bounds"])
        reference = re.compile(r"(?<![\w.'])" + r"(?:'" + re.escape(sheet.replace("'", "''")) + r"'|"
                               + re.escape(sheet) + r")\s*!\s*(?i:" + bounds + r")(?![\w:$])")
        candidates = [part for part in clauses if reference.search(part)]
        exclusions = (r"不(?:要|检查|审查|使用|选)|排除|剔除|比如|例如|举例|候选|是否|能否|还是|"
                      r"\b(?:not|exclude|except|example|candidate|whether|or)\b")
        if not candidates or any(re.search(exclusions, reference.sub("", part), re.I) for part in candidates):
            raise ValueError("fusion_workbook_region_unconfirmed")
        if region["header_rows"][0] != _region_box(region["bounds"])[1]:
            header = region["header_rows"][0]
            marker = rf"(?:表头(?:行)?\s*(?:[:：=]|是|为|在)?\s*第?\s*{header}\s*行?(?!\d)|\bheader\s+row\s*[:=]?\s*{header}\b)"
            confirmed = False
            for part in candidates:
                for match in reference.finditer(part):
                    tail = part[match.end():]
                    following = next_region.search(tail)
                    if re.search(marker, tail[:following.start()] if following else tail, re.I):
                        confirmed = True
            if not confirmed:
                raise ValueError("fusion_workbook_header_unconfirmed")
    return normalized


def make_request(skill, refs, options, observations):
    if type(options) is not dict or not options.keys() <= OPTION_KEYS[skill] or "observations" in options:
        raise ValueError("fusion_options_invalid")
    if skill == "data-audit":
        roles = set(refs)
        if roles == {WORKBOOK_ROLE}:
            workbook_regions(options.get("regions"))
        elif roles == {CSV_TABLE_ROLE}:
            csv_table_options(options, refs)
        else:
            raise ValueError("fusion_workbook_input_required")
    if len(refs) + (observations is not None) > 32:
        raise ValueError("fusion_input_count_limit")
    cassette = None
    inputs = [{"role": role, "path": item["path"].removeprefix(INPUT_ROOT), "sha256": item["sha256"]}
              for role, item in sorted(refs.items())]
    if observations is not None:
        if (skill != "literature-verification" or type(observations) is not dict
                or not observations.keys() <= {"http", "refchecker_candidates", "reference_batch"}
                or type(observations.get("http", {})) is not dict
                or type(observations.get("refchecker_candidates", [])) is not list
                or ("reference_batch" in observations and (set(observations) != {"reference_batch"}
                    or type(observations["reference_batch"]) is not dict))):
            raise ValueError("fusion_host_observations_invalid")
        cassette = encode(observations)
        if len(cassette) > MAX_REQUEST:
            raise ValueError("fusion_observations_size_limit")
        inputs.append({"role": "observations", "path": "observations.json", "sha256": sha(cassette)})
    raw = encode({"interface_version": 1, "inputs": inputs, "options": options})
    if len(raw) > MAX_REQUEST:
        raise ValueError("fusion_request_size_limit")
    return raw, cassette


def validate_report(report, payload, bundle, request_hash):
    if (type(report) is not dict or report.get("status") not in {"complete", "partial", "failed", "unsupported"}
            or any(type(report.get(key)) is not list for key in REPORT_KEYS) or report["artifacts"]):
        raise ValueError("fusion_report_shape_invalid")
    execution, worker = report.get("execution"), report.get("worker")
    if (type(execution) is not dict or execution.get("skill") != bundle.skill
            or execution.get("version") != bundle.version
            or execution.get("inputs") != payload["inputs"] or execution.get("options") != payload["options"]
            or type(worker) is not dict or worker.get("schema") != SCOPE
            or worker.get("request_sha256") != request_hash or worker.get("bundle_sha256") != bundle.sha256):
        raise ValueError("fusion_report_identity_mismatch")
    runtime = worker.get("environment")
    if (type(runtime) is not dict or runtime.get("lock_sha256") != bundle.lock_sha256
            or type(runtime.get("dependencies")) is not dict or runtime["dependencies"].keys() != bundle.pins.keys()):
        raise ValueError("fusion_report_environment_mismatch")
    unavailable = report["status"] == "unsupported" and report["limitations"] == ["fusion_worker_environment_mismatch"]
    if unavailable:
        if any(report[key] for key in REPORT_KEYS - {"limitations"}):
            raise ValueError("fusion_report_environment_mismatch")
    elif (runtime.get("dependencies") != bundle.pins or runtime.get("platform") != "linux-x86_64"
          or not str(runtime.get("python", "")).startswith("3.12.") or runtime.get("libseccomp_fchmodat2") is not True
          or execution.get("python") != runtime.get("python")):
        raise ValueError("fusion_report_environment_mismatch")
    return runtime
