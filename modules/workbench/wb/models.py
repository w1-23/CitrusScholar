"""Wire contracts for the standalone workbench."""
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator

Ref = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_:.-]{1,160}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
OutputFormat = Literal["png", "svg", "pdf"]
Editability = Literal["semantic", "partial", "layout_only", "preview_only"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Strict):
    kind: Literal["script", "rendered_asset", "project_bundle"]
    language: Literal["python", "r", "unknown"]
    entrypoint_ref: Ref
    assets: list[Ref] = Field(default_factory=list, max_length=32)
    hashes: dict[Ref, Sha256] = Field(default_factory=dict, max_length=33)


class ImportOptions(Strict):
    parse_mode: Literal["auto", "editable", "preview_only"] = "auto"


class ImportRequest(Strict):
    source: Source
    options: ImportOptions = Field(default_factory=ImportOptions)


class PreviewRun(Strict):
    source: Source


class PreviewCard(Strict):
    preview_ref: Ref
    preview_version: int = Field(ge=1)
    figure_ordinal: int = Field(ge=0, le=7)
    label: str
    thumbnail_url: str
    preview_sha256: Sha256
    capability: Literal["native", "semantic", "preview_only"]
    source_state: Literal["verified", "unavailable", "not_applicable"]
    bundle_sha256: Sha256 | None = None
    runtime_ref: Sha256 | None = None


class PreviewCards(Strict):
    cards: list[PreviewCard]


class PreviewOperation(PreviewCards):
    operation_id: Ref
    status: Literal["running", "ready", "failed"]
    created_at: str
    error: Literal["source_missing", "library_unavailable", "invalid_source_or_capture",
                   "isolated_render_failed", "renderer_unavailable", "interrupted", "internal_error"] | None = None


class PreviewImport(Strict):
    preview_ref: Ref
    preview_version: int = Field(ge=1)
    options: ImportOptions = Field(default_factory=lambda: ImportOptions(parse_mode="editable"))


class Capture(Strict):
    ordinal: int = Field(ge=0, le=7)
    count: int = Field(ge=1, le=8)
    signature: Sha256


class SourceReceipt(Strict):
    preview_ref: Ref
    preview_version: int = Field(ge=1)
    bundle_sha256: Sha256
    runtime_ref: Sha256
    capture: Capture


class Override(Strict):
    text: str = Field(max_length=500)


class Edit(Strict):
    expected_revision: int = Field(ge=1)
    overrides: dict[Ref, Override] = Field(min_length=1, max_length=64)


class NativePatch(Strict):
    gid: Ref
    prop: Ref
    value: JsonValue
    identity: str | None = Field(default=None, max_length=300)


class NativeEdit(Strict):
    expected_revision: int = Field(ge=1)
    patches: list[NativePatch] = Field(min_length=1, max_length=128)


class Pick(Strict):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    locked_gids: list[Ref] = Field(default_factory=list, max_length=1200)
    current_gid: Ref | None = None
    revision: int | None = Field(default=None, ge=1)


class GroupSelection(Strict):
    gids: list[Ref] = Field(min_length=1, max_length=120)
    locked_gids: list[Ref] = Field(default_factory=list, max_length=1200)
    revision: int | None = Field(default=None, ge=1)


class GroupMove(Strict):
    expected_revision: int = Field(ge=1)
    dx_mm: float = Field(ge=-400, le=400)
    dy_mm: float = Field(ge=-400, le=400)
    locked_gids: list[Ref] = Field(default_factory=list, max_length=1200)


class GroupScale(Strict):
    expected_revision: int = Field(ge=1)
    factor: float = Field(ge=0.05, le=10)
    locked_gids: list[Ref] = Field(default_factory=list, max_length=1200)


class RevisionName(Strict):
    name: str = Field(max_length=80)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} or
               (unicodedata.category(char) == "Cf" and char not in "\u200c\u200d") for char in value):
            raise ValueError("name must be single-line text without control characters")
        return value.strip()


class RevisionSummary(Strict):
    revision: int
    created_at: str
    name: str | None = None


class RevisionHistory(Strict):
    versions: list[RevisionSummary]


class Restore(Strict):
    expected_revision: int = Field(ge=1)
    revision: int = Field(ge=1)


class Export(Strict):
    format: OutputFormat
    width_mm: float | None = Field(default=None, ge=15, le=400)
    height_mm: float | None = Field(default=None, ge=15, le=400)
    dpi: int = Field(default=150, ge=72, le=450)


class Panel(Strict):
    id: Ref
    label: str


class FigureObject(Strict):
    id: Ref
    panel_id: Ref | None = None
    kind: str
    label: str
    properties: dict[str, str | int | float | bool | None]
    editable: list[Literal["text"]]


class Renderer(Strict):
    id: Ref
    version: str


class EngineDocument(Strict):
    renderer: Renderer
    editability: Editability
    panels: list[Panel] = Field(max_length=100)
    objects: list[FigureObject] = Field(max_length=1200)
    limitations: list[str] = Field(max_length=100)
    native_manifest: dict[str, JsonValue] | None = None
    native_editability: Editability | None = None
    capture: Capture | None = None


class SourceInfo(Strict):
    language: Literal["python", "r", "vector", "raster"]
    source_ref: Ref
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    library_asset_ref: Ref | None = None
    library_version: str | None = Field(default=None, max_length=120)
    receipt: SourceReceipt | None = None


class FigureDocument(EngineDocument):
    schema_version: Literal["figure-document/1"] = "figure-document/1"
    figure_id: Ref
    name: str
    revision: int
    source: SourceInfo
    layout: dict[str, float] = Field(default_factory=dict)
    preview_format: Literal["png", "svg", "pdf"] = "png"


class Launch(Strict):
    ticket: str = Field(min_length=16, max_length=4096)
