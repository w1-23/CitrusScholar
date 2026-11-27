"""Wire contracts for the standalone workbench."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Ref = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_:.-]{1,160}$")]
OutputFormat = Literal["png", "svg", "pdf"]
Editability = Literal["semantic", "partial", "layout_only", "preview_only"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Strict):
    kind: Literal["script", "rendered_asset", "project_bundle"]
    language: Literal["python", "r", "unknown"]
    entrypoint_ref: Ref
    assets: list[Ref] = Field(default_factory=list, max_length=32)


class ImportOptions(Strict):
    parse_mode: Literal["auto", "editable", "preview_only"] = "auto"


class ImportRequest(Strict):
    source: Source
    options: ImportOptions = Field(default_factory=ImportOptions)


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


class SourceInfo(Strict):
    language: Literal["python", "r", "vector", "raster"]
    source_ref: Ref
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    library_asset_ref: Ref | None = None
    library_version: str | None = Field(default=None, max_length=120)


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
