"""F-05: one Worker timeout policy replaces the 120 s / 90 s pair."""
from citrus_figures import timeouts


def test_the_two_legacy_numbers_are_gone():
    receipt = timeouts.policy()
    assert 120 not in receipt["base_seconds"].values()
    assert 90 not in receipt["base_seconds"].values()
    assert receipt["legacy_defaults_ignored"] == [90, 120]
    assert timeouts.resolve_timeout(kind="script") == timeouts.BASE_SECONDS["script"]
    assert timeouts.resolve_timeout(kind="template") == timeouts.BASE_SECONDS["template"]


def test_budget_scales_with_input_size_and_figure_count():
    base = timeouts.resolve_timeout(kind="script")
    larger = timeouts.resolve_timeout(kind="script", input_bytes=8 << 20)
    two_figures = timeouts.resolve_timeout(kind="script", figures=2)
    assert larger == base + 8 * timeouts.PER_MIB_SECONDS
    assert two_figures == base + timeouts.PER_FIGURE_SECONDS
    assert timeouts.resolve_timeout(kind="script", input_bytes=64 << 20) == base + timeouts.MAX_SIZE_SECONDS


def test_template_complexity_is_part_of_the_decision():
    plain = timeouts.resolve_timeout(kind="template", template_id="volcano")
    layered = timeouts.resolve_timeout(kind="template", template_id="marginal")
    assert layered > plain


def test_a_deadline_cannot_widen_the_job_and_keeps_a_real_floor():
    assert timeouts.resolve_timeout(kind="script", deadline_sec=40) == timeouts.MINIMUM_SECONDS
    assert timeouts.resolve_timeout(kind="script", deadline_sec=10) == timeouts.MINIMUM_SECONDS
    assert timeouts.resolve_timeout(kind="script", deadline_sec=10_000) == timeouts.BASE_SECONDS["script"]


def test_operator_ceiling_caps_but_legacy_values_are_ignored():
    assert timeouts.resolve_timeout(kind="script", configured_sec=120) == timeouts.BASE_SECONDS["script"]
    assert timeouts.resolve_timeout(kind="script", configured_sec=90) == timeouts.BASE_SECONDS["script"]
    assert timeouts.resolve_timeout(kind="script", configured_sec=75) == 75
    assert timeouts.resolve_timeout(kind="script", configured_sec=100_000) == timeouts.BASE_SECONDS["script"]


def test_budget_stays_within_published_bounds():
    assert timeouts.resolve_timeout(kind="analysis") == timeouts.BASE_SECONDS["analysis"]
    assert timeouts.resolve_timeout(kind="script", input_bytes=1 << 30, figures=99,
                                    template_id="marginal") <= timeouts.MAXIMUM_SECONDS
    for kind in timeouts.BASE_SECONDS:
        granted = timeouts.resolve_timeout(kind=kind, input_bytes=1 << 30)
        assert timeouts.MINIMUM_SECONDS <= granted <= timeouts.MAXIMUM_SECONDS


def test_kind_classification_never_trusts_an_unknown_label():
    assert timeouts.execution_kind("template") == "template"
    assert timeouts.execution_kind("", template_id="roc") == "template"
    assert timeouts.execution_kind("script") == "script"
    assert timeouts.execution_kind("whatever") == "script"
    assert timeouts.execution_kind("whatever", figures=0) == "analysis"


def test_receipt_states_that_this_is_worker_time_not_model_budget():
    described = timeouts.describe(kind="template", timeout_sec=180, input_bytes=2048,
                                  figures=1, template_id="volcano")
    assert described["unit"] == "worker_wall_clock_seconds"
    assert described["policy"] == timeouts.POLICY_VERSION
    assert described["timeout_sec"] == 180
    assert "model" not in described["unit"]


def test_non_integer_or_negative_inputs_are_normalized():
    base = timeouts.BASE_SECONDS["script"]
    for value in (-1, None, "8", 2.5):
        assert timeouts.resolve_timeout(kind="script", input_bytes=value) == base
    assert timeouts.resolve_timeout(kind="unknown-kind") == base
    assert timeouts.resolve_timeout(kind="script", figures=0) == base
