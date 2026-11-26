"""F-04: the five fixed templates are one typed registry and one contract.

These tests never need a sandbox: the registry compiles bounded source from
typed parameters, and the source is checked by actually drawing one figure in
process. That is exactly the property the execution chain depends on.
"""
import json

import matplotlib
import numpy as np
import pandas as pd
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from citrus_figures import template_registry as registry  # noqa: E402

PARAMETERS = {
    "volcano": {"gene_col": "gene", "effect_col": "log2fc", "p_col": "padj", "top_labels": 3},
    "roc": {"fpr_col": "fpr", "tpr_cols": ["model_a", "model_b"]},
    "dotplot": {"row_col": "cell", "column_col": "marker", "size_col": "pct", "color_col": "avg"},
    "marginal": {"x_col": "x", "y_col": "y", "group_col": "group", "bins": 10},
    "paired": {"id_col": "pair_id", "condition_col": "condition", "value_col": "value"},
}

_RANDOM = np.random.RandomState(7)
FRAMES = {
    "volcano": pd.DataFrame({"gene": [f"g{i}" for i in range(30)],
                             "log2fc": np.linspace(-3, 3, 30),
                             "padj": np.geomspace(1e-6, 0.9, 30)}),
    "roc": pd.DataFrame({"fpr": np.linspace(0, 1, 20),
                         "model_a": np.linspace(0, 1, 20) ** 0.5,
                         "model_b": np.linspace(0, 1, 20) ** 0.8}),
    "dotplot": pd.DataFrame({"cell": ["a", "a", "b", "b"], "marker": ["m1", "m2", "m1", "m2"],
                             "pct": [10, 40, 30, 70], "avg": [1.0, 2.0, 1.5, 3.0]}),
    "marginal": pd.DataFrame({"x": _RANDOM.normal(size=40), "y": _RANDOM.normal(size=40),
                              "group": ["a"] * 20 + ["b"] * 20}),
    "paired": pd.DataFrame({"pair_id": sum([[f"p{i}"] * 2 for i in range(8)], []),
                            "condition": ["ck", "t1"] * 8,
                            "value": _RANDOM.normal(size=16).cumsum()}),
}


def test_registry_declares_exactly_the_five_published_templates():
    assert registry.TEMPLATE_IDS == ("volcano", "roc", "dotplot", "marginal", "paired")
    receipt = registry.registry_receipt()
    assert receipt["version"] == registry.TEMPLATE_VERSION
    assert receipt["templates"] == list(registry.TEMPLATE_IDS)
    assert all(receipt["parameters"][name] for name in registry.TEMPLATE_IDS)


def test_unsafe_or_unknown_parameters_fail_before_dispatch():
    for template_id, parameters in (
            ("heatmap", {}),
            ("volcano", {"gene_col": "gene"}),
            ("volcano", {**PARAMETERS["volcano"], "unexpected": 1}),
            ("volcano", {**PARAMETERS["volcano"], "template": "roc"}),
            ("marginal", {"x_col": "same", "y_col": "same"}),
            ("marginal", {**PARAMETERS["marginal"], "group_col": "y"}),
            ("volcano", {**PARAMETERS["volcano"], "gene_col": "a\nb"}),
            ("roc", {"fpr_col": "fpr", "tpr_cols": ["a,b"]}),
            ("paired", {**PARAMETERS["paired"], "value_col": ""}),
    ):
        with pytest.raises(registry.TemplateError):
            registry.parse(template_id, parameters)


def test_compiled_request_shares_the_unified_snapshot_and_contract():
    compiled = registry.compile_request("volcano", PARAMETERS["volcano"],
                                        binding="tidy", binding_kind="csv", title="Volcano")
    contract = compiled.output_contract
    assert contract["version"] == 2
    assert contract["tables"] == [{"name": "frame", "parents": ["tidy"]}]
    figure = contract["figures"][0]
    assert figure["parents"] == ["frame"]
    assert figure["render_script"] == compiled.render_script
    assert figure["formats"] == ["svg"]
    marker = registry.parse_marker(compiled.main_script)
    assert marker == {"template_id": "volcano", "version": registry.TEMPLATE_VERSION,
                      "parameters": compiled.parameters, "binding": "tidy",
                      "binding_kind": "csv", "formats": ["svg"]}
    assert "emit.table" in compiled.main_script
    assert "load_dataset" not in compiled.main_script


def test_dataset_binding_reads_the_typed_internal_table():
    compiled = registry.compile_request("paired", PARAMETERS["paired"],
                                        binding="tidy", binding_kind="dataset")
    assert "load_dataset(tidy)" in compiled.main_script
    assert registry.parse_marker(compiled.main_script)["binding_kind"] == "dataset"


def test_binding_name_must_be_an_identifier():
    with pytest.raises(registry.TemplateError):
        registry.compile_request("volcano", PARAMETERS["volcano"], binding="not a name")


@pytest.mark.parametrize("template_id", registry.TEMPLATE_IDS)
def test_every_template_draws_one_figure_without_emit(template_id):
    compiled = registry.compile_request(template_id, PARAMETERS[template_id], title="Figure")
    assert "emit" not in compiled.render_script
    namespace = {"data": {"frame": FRAMES[template_id]}}
    plt.close("all")
    exec(compile(compiled.render_script, f"<{template_id}>", "exec"), namespace)
    figures = [value for value in namespace.values() if isinstance(value, matplotlib.figure.Figure)]
    assert len(figures) == 1
    assert figures[0].axes
    plt.close("all")


def test_column_names_are_data_and_never_become_syntax():
    hostile = "x']) ; raise SystemExit('no') #"
    parameters = {**PARAMETERS["volcano"], "gene_col": hostile, "p_col": "padj"}
    compiled = registry.compile_request("volcano", parameters, title="t")
    compile(compiled.render_script, "<render>", "exec")
    assert "SystemExit" in compiled.render_script  # it is a literal, not a call
    namespace = {"data": {"frame": FRAMES["volcano"].rename(columns={"gene": hostile})}}
    plt.close("all")
    exec(compile(compiled.render_script, "<hostile>", "exec"), namespace)
    assert any(isinstance(value, matplotlib.figure.Figure) for value in namespace.values())
    plt.close("all")


def test_compiled_source_is_json_round_trippable_metadata():
    compiled = registry.compile_request("marginal", PARAMETERS["marginal"], title="边缘分布")
    assert json.loads(json.dumps(compiled.parameters, ensure_ascii=False)) == compiled.parameters
    assert compiled.receipt()["template_id"] == "marginal"
    assert compiled.formats == ("svg",)
