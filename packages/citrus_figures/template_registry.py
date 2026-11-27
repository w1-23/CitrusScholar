"""The five fixed scientific figure templates, as one typed registry (audit F-04).

Decision recorded here
----------------------
The audit kept ``nature-figure`` and retired the *second execution chain*. The
templates themselves were never the problem: volcano, ROC, dotplot, marginal
and paired are good, validated scientific drawings. What was wrong is that
they lived in their own tool (``run_nature_plot``), their own worker
(``ExecutionLimits(timeout_sec=90)``), their own QA and their own workbench
object model.

So the templates move here, as data plus a compiler, and every execution —
fixed template or arbitrary Matplotlib — goes through
``run_research_figure_script``, the same frozen input snapshot, the same
Worker, the same timeout policy (``citrus_figures.timeouts``), the same output
contract, the same current-QA and the same Figure Workbench project.

What this module must not become
--------------------------------
It is not a tool and it is not a worker. It validates typed parameters and
compiles them into bounded Python source. Only the host calls it; the model
never supplies a path, a command, raw argv or executable code through it, and
column names are embedded with ``repr`` so a label can never become syntax.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TEMPLATE_VERSION = 1
TEMPLATE_IDS: tuple[str, ...] = ("volcano", "roc", "dotplot", "marginal", "paired")
TABLE_NAME = "frame"
FIGURE_NAME = "figure"
MARKER_PREFIX = "# citrus-template/v1 "

_Name = Annotated[str, Field(min_length=1, max_length=160)]


class TemplateError(ValueError):
    """A template request that cannot be compiled; dispatch must not happen."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"[TEMPLATE_{self.code}] {self.message}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    @field_validator("*", mode="before")
    @classmethod
    def _no_control_characters(cls, value: Any) -> Any:
        if isinstance(value, str):
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("template labels and column names must not contain control characters")
        return value


class _Columns(_Strict):
    drop_incomplete: bool = True

    @model_validator(mode="after")
    def _distinct(self):
        values = [value for key, value in self.model_dump().items()
                  if key.endswith("_col") and isinstance(value, str) and value]
        if isinstance(self, RocParams):
            values += list(self.tpr_cols)
        if len(values) != len(set(values)):
            raise ValueError("template mapping columns must be distinct")
        return self

    @property
    def _mapped(self) -> list[str]:
        return [value for key, value in self.model_dump().items()
                if key.endswith("_col") and isinstance(value, str) and value]


class VolcanoParams(_Columns):
    template: Literal["volcano"] = "volcano"
    gene_col: _Name
    effect_col: _Name
    p_col: _Name = Field(description="Adjusted p-value column, never raw p values.")
    effect_label: str = ""
    p_label: str = "-log10(adjusted P)"
    p_threshold: float = Field(default=0.05, gt=0, le=1)
    effect_threshold: float = Field(default=1.0, gt=0)
    top_labels: int = Field(default=10, ge=0, le=30)


class RocParams(_Columns):
    template: Literal["roc"] = "roc"
    fpr_col: _Name
    tpr_cols: list[_Name] = Field(min_length=1, max_length=12)

    @field_validator("tpr_cols")
    @classmethod
    def _exact_names(cls, values: list[str]) -> list[str]:
        if any("," in value or value != value.strip() for value in values):
            raise ValueError("ROC series names must not contain commas or padding")
        return values


class DotplotParams(_Columns):
    template: Literal["dotplot"] = "dotplot"
    row_col: _Name
    column_col: _Name
    size_col: _Name
    color_col: _Name
    size_label: str = ""
    color_label: str = ""


class MarginalParams(_Columns):
    template: Literal["marginal"] = "marginal"
    x_col: _Name
    y_col: _Name
    group_col: str = ""
    x_label: str = ""
    y_label: str = ""
    bins: int = Field(default=24, ge=1, le=100)


class PairedParams(_Columns):
    template: Literal["paired"] = "paired"
    id_col: _Name
    condition_col: _Name
    value_col: _Name
    value_label: str = ""


PARAMS: dict[str, type[_Columns]] = {
    "volcano": VolcanoParams, "roc": RocParams, "dotplot": DotplotParams,
    "marginal": MarginalParams, "paired": PairedParams,
}

BINDING_KINDS = ("csv", "tsv", "excel", "dataset")


@dataclass(frozen=True)
class CompiledTemplate:
    """Bounded source + contract produced from typed parameters."""

    template_id: str
    version: int
    parameters: dict[str, Any]
    binding: str
    binding_kind: str
    title: str
    formats: tuple[str, ...]
    main_script: str
    render_script: str
    output_contract: dict[str, Any]

    def marker(self) -> dict[str, Any]:
        return {"template_id": self.template_id, "version": self.version,
                "parameters": self.parameters, "binding": self.binding,
                "binding_kind": self.binding_kind, "formats": list(self.formats)}

    def receipt(self) -> dict[str, Any]:
        return {"template_id": self.template_id, "version": self.version,
                "parameters": self.parameters, "binding": self.binding,
                "binding_kind": self.binding_kind}


def parse(template_id: str, parameters: Any) -> _Columns:
    """Validate one typed request; raise :class:`TemplateError` before dispatch."""
    key = str(template_id or "").strip().lower()
    if key not in PARAMS:
        raise TemplateError("UNKNOWN", f"未知模板 {template_id!r}；可用：{', '.join(TEMPLATE_IDS)}。")
    raw = {} if parameters in (None, "") else parameters
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise TemplateError("PARAMETERS", "template_parameters_json 需要 JSON 对象。") from None
    if not isinstance(raw, dict):
        raise TemplateError("PARAMETERS", "template_parameters_json 需要 JSON 对象。")
    raw = {**raw}
    if "template" in raw and raw["template"] != key:
        raise TemplateError("PARAMETERS", "参数中的 template 与 template_id 不一致。")
    raw["template"] = key
    try:
        return PARAMS[key].model_validate(raw)
    except ValueError as exc:
        raise TemplateError("PARAMETERS", f"{key} 参数未通过校验：{exc}") from None


def _embed(value: Any) -> str:
    """Paste a user-visible string as a Python literal, never as syntax."""
    return json.dumps(str(value), ensure_ascii=True)


def _csv_reader(binding: str, kind: str) -> str:
    if kind == "dataset":
        return f"{binding}_frame = load_dataset({binding})\n"
    if kind == "tsv":
        return f"{binding}_frame = pd.read_csv({binding}, sep='\\t')\n"
    if kind == "excel":
        return f"{binding}_frame = pd.read_excel({binding})\n"
    return f"{binding}_frame = pd.read_csv({binding})\n"


def main_source(compiled: CompiledTemplate) -> str:
    return (
        MARKER_PREFIX + json.dumps(compiled.marker(), ensure_ascii=True, sort_keys=True) + "\n"
        "# Generated by citrus_figures.template_registry: the fixed template is data, not free code.\n"
        "import pandas as pd\n"
        + _csv_reader(compiled.binding, compiled.binding_kind)
        + f"frame = {compiled.binding}_frame\n"
        f"emit.table({_embed(TABLE_NAME)}, frame, parents=[{_embed(compiled.binding)}])\n"
    )


def _header(compiled: CompiledTemplate, imports: str) -> str:
    return (f"# citrus-template-render/v{compiled.version} {compiled.template_id}\n"
            "import matplotlib.pyplot as plt\n"
            "import numpy as np\n"
            f"import pandas as pd\n"
            f"frame = data[{_embed(TABLE_NAME)}]\n"
            + imports)


def _drop(params: dict[str, Any], columns: list[str]) -> str:
    if not params.get("drop_incomplete", True):
        return ""
    return f"frame = frame.dropna(subset={columns!r})\n"


def render_source(compiled: CompiledTemplate) -> str:
    params = dict(compiled.parameters)
    template = compiled.template_id
    title = _embed(compiled.title or template.title())
    if template == "volcano":
        columns = [params["gene_col"], params["effect_col"], params["p_col"]]
        body = (
            _header(compiled, "")
            + _drop(params, columns)
            + f"gene = frame[{_embed(params['gene_col'])}].astype(str)\n"
            f"effect = pd.to_numeric(frame[{_embed(params['effect_col'])}], errors='coerce')\n"
            f"pvalue = pd.to_numeric(frame[{_embed(params['p_col'])}], errors='coerce')\n"
            "keep = effect.notna() & pvalue.notna() & (pvalue > 0)\n"
            "gene, effect, pvalue = gene[keep], effect[keep].astype(float), pvalue[keep].astype(float)\n"
            "magnitude = -np.log10(np.clip(pvalue.to_numpy(), 1e-300, 1.0))\n"
            f"significant = (pvalue.to_numpy() <= {params['p_threshold']!r}) & (np.abs(effect.to_numpy()) >= {params['effect_threshold']!r})\n"
            "fig, ax = plt.subplots(figsize=(3.5, 3.0))\n"
            "ax.scatter(effect.to_numpy()[~significant], magnitude[~significant], s=6, color='#9AA3AD', linewidths=0, label='not significant')\n"
            "ax.scatter(effect.to_numpy()[significant], magnitude[significant], s=8, color='#0F4D92', linewidths=0, label='significant')\n"
            f"ax.axhline(-np.log10({params['p_threshold']!r}), color='#B23A48', linewidth=0.6, linestyle='--')\n"
            f"ax.axvline({params['effect_threshold']!r}, color='#9AA3AD', linewidth=0.6, linestyle='--')\n"
            f"ax.axvline(-{params['effect_threshold']!r}, color='#9AA3AD', linewidth=0.6, linestyle='--')\n"
            f"if {int(params['top_labels'])} and len(pvalue):\n"
            f"    for index in np.argsort(pvalue.to_numpy())[:{int(params['top_labels'])}]:\n"
            "        ax.annotate(str(gene.iloc[index]), (float(effect.iloc[index]), float(magnitude[index])),\n"
            "                    fontsize=5, xytext=(2, 2), textcoords='offset points')\n"
            f"ax.set_xlabel({_embed(params['effect_label'] or 'log2 fold change')})\n"
            f"ax.set_ylabel({_embed(params['p_label'])})\n"
            f"ax.set_title({title})\n"
            "if significant.any():\n"
            "    ax.legend(loc='upper right', fontsize=5, frameon=False)\n"
        )
    elif template == "roc":
        columns = [params["fpr_col"], *params["tpr_cols"]]
        series = repr(list(params["tpr_cols"]))
        body = (
            _header(compiled, "")
            + _drop(params, columns)
            + "fig, ax = plt.subplots(figsize=(3.3, 3.0))\n"
            "ax.plot([0.0, 1.0], [0.0, 1.0], color='#9AA3AD', linewidth=0.6, linestyle='--')\n"
            f"for name in {series}:\n"
            f"    ax.plot(pd.to_numeric(frame[{_embed(params['fpr_col'])}], errors='coerce'),\n"
            "            pd.to_numeric(frame[name], errors='coerce'), linewidth=0.9, label=name)\n"
            "ax.set_xlim(0.0, 1.0)\nax.set_ylim(0.0, 1.0)\n"
            "ax.set_xlabel('False positive rate')\nax.set_ylabel('True positive rate')\n"
            f"ax.set_title({title})\n"
            "ax.legend(loc='lower right', fontsize=5, frameon=False)\n"
        )
    elif template == "dotplot":
        columns = [params["row_col"], params["column_col"], params["size_col"], params["color_col"]]
        row_code = f"frame[{_embed(params['row_col'])}].astype(str)"
        column_code = f"frame[{_embed(params['column_col'])}].astype(str)"
        body = (
            _header(compiled, "")
            + _drop(params, columns)
            + f"rows = list(dict.fromkeys(frame[{_embed(params['row_col'])}].astype(str)))\n"
            f"columns = list(dict.fromkeys(frame[{_embed(params['column_col'])}].astype(str)))\n"
            "row_index = {name: index for index, name in enumerate(rows)}\n"
            "column_index = {name: index for index, name in enumerate(columns)}\n"
            f"sizes = pd.to_numeric(frame[{_embed(params['size_col'])}], errors='coerce')\n"
            f"colors = pd.to_numeric(frame[{_embed(params['color_col'])}], errors='coerce')\n"
            "usable = sizes.notna() & colors.notna()\n"
            "sizes = sizes[usable]\ncolors = colors[usable]\n"
            "dots = (sizes - sizes.min()) / (sizes.max() - sizes.min()) if sizes.max() > sizes.min() else sizes * 0 + 0.5\n"
            "fig, ax = plt.subplots(figsize=(3.2, 3.2))\n"
            f"column_positions = [column_index[name] for name in {column_code}[usable]]\n"
            f"row_positions = [row_index[name] for name in {row_code}[usable]]\n"
            "artist = ax.scatter(column_positions, row_positions, s=8 + 60 * dots.to_numpy(),\n"
            "                    c=colors.to_numpy(), cmap='viridis', linewidths=0)\n"
            "ax.set_xticks(range(len(columns)))\nax.set_xticklabels(columns, rotation=90)\n"
            "ax.set_yticks(range(len(rows)))\nax.set_yticklabels(rows)\n"
            "figure_colorbar = fig.colorbar(artist, ax=ax, shrink=0.7, pad=0.02)\n"
            f"figure_colorbar.set_label({_embed(params['color_label'] or params['color_col'])})\n"
            f"ax.set_title({title})\n"
        )
    elif template == "marginal":
        columns = [params["x_col"], params["y_col"]]
        group = params.get("group_col") or ""
        if group:
            columns.append(group)
        group_code = (f"groups = frame[{_embed(group)}].astype(str).to_numpy()\n"
                      "labels = list(dict.fromkeys(groups))\n"
                      "for label in labels:\n"
                      "    mask = groups == label\n"
                      "    ax.scatter(x[mask], y[mask], s=7, linewidths=0, label=label)\n"
                      "    ax_top.hist(x[mask], bins=bins, linewidth=0, alpha=0.65)\n"
                      "    ax_right.hist(y[mask], bins=bins, orientation='horizontal', linewidth=0, alpha=0.65)\n"
                      "ax.legend(loc='upper left', fontsize=5, frameon=False)\n") if group else (
                      "ax.scatter(x, y, s=7, linewidths=0, color='#0F4D92')\n"
                      "ax_top.hist(x, bins=bins, linewidth=0, color='#9AA3AD')\n"
                      "ax_right.hist(y, bins=bins, orientation='horizontal', linewidth=0, color='#9AA3AD')\n")
        body = (
            _header(compiled, "from matplotlib.gridspec import GridSpec\n")
            + _drop(params, columns)
            + f"x = pd.to_numeric(frame[{_embed(params['x_col'])}], errors='coerce').to_numpy()\n"
            f"y = pd.to_numeric(frame[{_embed(params['y_col'])}], errors='coerce').to_numpy()\n"
            "usable = ~(np.isnan(x) | np.isnan(y))\n"
            "x, y = x[usable], y[usable]\n"
            f"bins = {int(params['bins'])}\n"
            "fig = plt.figure(figsize=(3.6, 3.4))\n"
            "grid = GridSpec(4, 4, figure=fig, hspace=0.08, wspace=0.08)\n"
            "ax = fig.add_subplot(grid[1:, :-1])\n"
            "ax_top = fig.add_subplot(grid[0, :-1], sharex=ax)\n"
            "ax_right = fig.add_subplot(grid[1:, -1], sharey=ax)\n"
            + group_code +
            "ax_top.tick_params(labelbottom=False)\nax_right.tick_params(labelleft=False)\n"
            f"ax.set_xlabel({_embed(params['x_label'] or params['x_col'])})\n"
            f"ax.set_ylabel({_embed(params['y_label'] or params['y_col'])})\n"
            f"ax_top.set_title({title})\n"
        )
    elif template == "paired":
        columns = [params["id_col"], params["condition_col"], params["value_col"]]
        body = (
            _header(compiled, "")
            + _drop(params, columns)
            + f"pair = frame[{_embed(params['id_col'])}].astype(str)\n"
            f"condition = frame[{_embed(params['condition_col'])}].astype(str)\n"
            f"value = pd.to_numeric(frame[{_embed(params['value_col'])}], errors='coerce')\n"
            "usable = value.notna()\n"
            "pair, condition, value = pair[usable], condition[usable].to_numpy(), value[usable]\n"
            "conditions = list(dict.fromkeys(condition))\n"
            "positions = {name: index for index, name in enumerate(conditions)}\n"
            "fig, ax = plt.subplots(figsize=(3.2, 3.0))\n"
            "for identifier in dict.fromkeys(pair.to_numpy()):\n"
            "    mask = (pair == identifier).to_numpy()\n"
            "    if len(set(condition[mask])) != len(conditions):\n"
            "        continue\n"
            "    ax.plot([positions[name] for name in condition[mask]], value.to_numpy()[mask],\n"
            "            color='#6B7280', linewidth=0.6, marker='o', markersize=2)\n"
            "means = [value.to_numpy()[(condition == name)].mean() for name in conditions]\n"
            "ax.plot(range(len(conditions)), means, color='#0F4D92', linewidth=1.4, marker='s', markersize=4, label='mean')\n"
            "ax.set_xticks(range(len(conditions)))\nax.set_xticklabels(conditions)\n"
            f"ax.set_ylabel({_embed(params['value_label'] or params['value_col'])})\n"
            f"ax.set_title({title})\n"
            "ax.legend(loc='upper left', fontsize=5, frameon=False)\n"
        )
    else:  # pragma: no cover - parse() rejects unknown ids first
        raise TemplateError("UNKNOWN", f"未知模板 {template!r}。")
    return body


def compile_request(template_id: str, parameters: Any, *, binding: str = "data",
                    binding_kind: str = "csv", title: str = "",
                    formats: tuple[str, ...] = ("svg",), version: int = TEMPLATE_VERSION,
                    with_style: bool = True) -> CompiledTemplate:
    """Compile typed parameters into source + the v2 output contract."""
    model = parse(template_id, parameters)
    key = str(template_id).strip().lower()
    binding_name = str(binding or "data")
    if not binding_name.isidentifier():
        raise TemplateError("BINDING", "数据绑定名必须是合法标识符。")
    kind = str(binding_kind or "csv").strip().lower()
    if kind not in BINDING_KINDS:
        raise TemplateError("BINDING", f"不支持的数据绑定类型 {binding_kind!r}。")
    ordered = tuple(dict.fromkeys(str(fmt).lower() for fmt in formats)) or ("svg",)
    compiled = CompiledTemplate(
        template_id=key, version=int(version), parameters=model.model_dump(),
        binding=binding_name, binding_kind=kind, title=str(title or ""),
        formats=ordered, main_script="", render_script="", output_contract={})
    render = render_source(compiled)
    if with_style:
        from citrus_figures.typography import inject
        render, _style = inject(render)
    compiled = CompiledTemplate(
        template_id=key, version=int(version), parameters=model.model_dump(),
        binding=binding_name, binding_kind=kind, title=str(title or ""),
        formats=ordered, main_script="", render_script=render, output_contract={})
    contract = {"version": 2,
                "tables": [{"name": TABLE_NAME, "parents": [binding_name]}],
                "figures": [{"name": FIGURE_NAME, "title": str(title or ""),
                             "formats": list(ordered), "parents": [TABLE_NAME],
                             "render_script": render}]}
    return CompiledTemplate(**{**compiled.__dict__, "main_script": main_source(compiled),
                               "output_contract": contract})


def parse_marker(source: str) -> dict[str, Any] | None:
    """Read the compiled-template provenance line out of a script, if present."""
    text = str(source or "")
    for line in text.splitlines()[:40]:
        if line.startswith(MARKER_PREFIX):
            try:
                value = json.loads(line[len(MARKER_PREFIX):])
            except ValueError:
                return None
            if isinstance(value, dict) and value.get("template_id") in TEMPLATE_IDS:
                return value
            return None
    return None


def registry_receipt() -> dict[str, Any]:
    """Printable identity of the registry, for tool responses and docs."""
    return {"version": TEMPLATE_VERSION, "templates": list(TEMPLATE_IDS),
            "parameters": {key: sorted(model.model_fields) for key, model in PARAMS.items()}}


__all__ = ["BINDING_KINDS", "CompiledTemplate", "FIGURE_NAME", "MARKER_PREFIX", "PARAMS",
           "TABLE_NAME", "TEMPLATE_IDS", "TEMPLATE_VERSION", "TemplateError",
           "compile_request", "main_source", "parse", "parse_marker", "registry_receipt",
           "render_source"]
