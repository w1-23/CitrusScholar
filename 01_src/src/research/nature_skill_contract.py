"""Typed, pinned Nature Figure template requests, without executing any source."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agent.research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

SCOPE = "citrus.nature-plot/1"
PRODUCER = "run_nature_plot"
SKILL_ID = "nature-figure"
SCRIPT_REL = "skills/nature-figure/scripts/plot_templates.py"
SCRIPT = "/workspace/plot_templates.py"
CSV = "/input/observations.csv"
EXPORTS = {"svg": "image/svg+xml", "pdf": "application/pdf", "tiff": "image/tiff"}
QA_MAX_BYTES = 128 << 10
MAX_CSV_BYTES = 32 << 20


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CsvRef(_Strict):
    file_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


Text = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]


class _Template(_Strict):
    drop_incomplete: bool = False

    @model_validator(mode="after")
    def distinct_columns(self):
        values = [value for key, value in self.model_dump().items()
                  if key.endswith("_col") and value is not None]
        if isinstance(self, Roc):
            values += self.tpr_cols
        if len(values) != len(set(values)):
            raise ValueError("nature_plot_mapping_columns_must_be_distinct")
        return self


class Volcano(_Template):
    template: Literal["volcano"]
    gene_col: Text
    effect_col: Text
    p_col: Text = Field(description="Adjusted p-value column, not raw p values.")
    effect_label: Text | None = None
    p_threshold: float = Field(default=0.05, gt=0, le=1, allow_inf_nan=False)
    effect_threshold: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    top_labels: int = Field(default=10, ge=0, le=30)


class Roc(_Template):
    template: Literal["roc"]
    fpr_col: Text
    tpr_cols: list[Text] = Field(min_length=1, max_length=12)

    @field_validator("tpr_cols")
    @classmethod
    def exact_cli_columns(cls, values):
        # The upstream CLI expects a comma-separated list and strips its tokens.
        if any("," in value or value != value.strip() for value in values):
            raise ValueError("nature_plot_roc_tpr_names_not_representable_by_upstream")
        return values


class Dotplot(_Template):
    template: Literal["dotplot"]
    row_col: Text
    column_col: Text
    size_col: Text
    color_col: Text
    size_label: Text | None = None
    color_label: Text | None = None


class Marginal(_Template):
    template: Literal["marginal"]
    x_col: Text
    y_col: Text
    group_col: Text | None = None
    x_label: Text | None = None
    y_label: Text | None = None
    bins: int = Field(default=24, ge=1, le=100)


class Paired(_Template):
    template: Literal["paired"]
    id_col: Text
    condition_col: Text
    value_col: Text
    value_label: Text | None = None


Plot = Annotated[Volcano | Roc | Dotplot | Marginal | Paired, Field(discriminator="template")]


class RunArgs(_Strict):
    backend: Literal["python", "r"]
    input: CsvRef
    plot: Plot
    title: Text | None = None


class CollectArgs(_Strict):
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def digest(raw):
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class PinnedTemplate:
    content: bytes
    sha256: str
    commit: str
    release_sha256: str
    vendor_root: Path


def pinned_template(release: Path, installed_version: str) -> PinnedTemplate:
    """Authenticate active script bytes against complete independently pinned vendor inventory."""
    from citrus_figures.nature_runtime import RELEASE_VENDOR_COMMIT, RELEASE_VENDOR_PATH, validate_vendor

    if installed_version != RELEASE_VENDOR_COMMIT:
        raise ValueError("nature_plot_release_not_supported")
    root = _path(release, RELEASE_VENDOR_PATH)
    provenance = validate_vendor(root, verify_files=False, expected_commit=RELEASE_VENDOR_COMMIT)
    expected = next(row["sha256"] for row in provenance["files"] if row["path"] == SCRIPT_REL)
    candidate = _bytes(_path(release, SCRIPT_REL), 200000)
    canonical = _bytes(_path(root, SCRIPT_REL), 200000)
    raw = _bytes(_path(release, "inventory.json"), _MAX_RESOURCE_BYTES)
    inventory = json.loads(raw)
    if (type(inventory) is not dict or inventory.get(SCRIPT_REL) != expected
            or inventory.get(RELEASE_VENDOR_PATH + "/" + SCRIPT_REL) != expected
            or digest(candidate) != expected or candidate != canonical):
        raise ValueError("nature_plot_script_pin_mismatch")
    return PinnedTemplate(candidate, expected, RELEASE_VENDOR_COMMIT, digest(raw), root)


def template_request(spec: RunArgs) -> tuple[str, dict]:
    """Registry template id and typed parameters for one fixed-plot request.

    The deprecated Agent tools use this to forward into the unified figure
    execution. Nothing is executed here: the registry re-validates every field
    before dispatch, so an unknown or inconsistent mapping fails as ``not_run``
    instead of becoming an uncertain worker call.
    """
    payload = {key: value for key, value in spec.plot.model_dump().items()
               if key != "template" and value is not None and value != ""}
    return spec.plot.template, payload


def output_names(template, operation):
    stem = f"nature-{template}-{operation[-12:]}"
    return stem, tuple(f"/outputs/{stem}.{suffix}" for suffix in (*EXPORTS, "qa.json"))


def argv(spec: RunArgs, stem: str) -> tuple[str, ...]:
    """No model-provided paths, executable, flags or raw argv cross this boundary."""
    if spec.backend == "r":
        from .nature_skill_r import arguments
        return arguments(spec, stem)
    options = spec.plot.model_dump(exclude={"template", "drop_incomplete"}, exclude_none=True)
    if isinstance(spec.plot, Marginal) and spec.plot.group_col is None:
        options["group_col"] = ""  # Upstream defaults to a field named group.
    options["title"] = spec.title
    arguments = [spec.plot.template, "--input=" + CSV, "--output=/outputs/" + stem]
    for key, value in options.items():
        if value is not None:
            encoded = ",".join(value) if isinstance(value, list) else str(value)
            arguments.append("--" + key.replace("_", "-") + "=" + encoded)
    if spec.plot.drop_incomplete:
        arguments.append("--drop-incomplete")
    return tuple(arguments)


_MAPPINGS = {
    "volcano": {"gene": "gene_col", "effect": "effect_col", "adjusted_p": "p_col"},
    "roc": {"fpr": "fpr_col", "tpr_series": "tpr_cols"},
    "dotplot": {"row_category": "row_col", "column_category": "column_col",
                "dot_size": "size_col", "dot_color": "color_col"},
    "marginal": {"x": "x_col", "y": "y_col", "group": "group_col"},
    "paired": {"pair_id": "id_col", "condition": "condition_col", "value": "value_col"},
}


def template_qa(raw: bytes, spec: RunArgs, stem: str, *, pdf_sha256=None) -> dict:
    qa = json.loads(raw)
    template = spec.plot.template
    mapping = {role: getattr(spec.plot, key) for role, key in _MAPPINGS[template].items()}
    if isinstance(spec.plot, Marginal) and spec.plot.group_col is None:
        mapping["group"] = ""
    if (type(qa) is not dict or qa.get("template") != template or qa.get("backend") != spec.backend
            or qa.get("demo") is not False or qa.get("input") != Path(CSV).name
            or qa.get("output_prefix") != stem or qa.get("mapping") != mapping):
        raise ValueError("nature_plot_upstream_qa_identity_mismatch")
    counts = [qa.get(key) for key in ("rows_input", "rows_plotted", "excluded_rows", "excluded_entities")]
    if (any(type(value) is not int or value < 0 for value in counts)
            or counts[0] != sum(counts[1:]) or not counts[1]
            or (any(counts[2:]) and not spec.plot.drop_incomplete)):
        raise ValueError("nature_plot_upstream_qa_counts_invalid")
    result = {key: qa[key] for key in ("rows_input", "rows_plotted", "excluded_rows", "excluded_entities")}
    if spec.backend == "r":
        from .nature_skill_r import figure_qa
        result.update(figure_qa(qa, pdf_sha256))
    return result
