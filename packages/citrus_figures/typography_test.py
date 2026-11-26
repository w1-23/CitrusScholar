"""F-08: one versioned manuscript style, honest font evidence, verified menu."""
import matplotlib
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from citrus_figures import typography  # noqa: E402


def test_injected_style_is_versioned_and_runs_standalone():
    source, record = typography.inject("import matplotlib.pyplot as plt\n")
    assert record["policy"] == "injected"
    assert record["style_version"] == typography.STYLE_VERSION
    assert source.startswith(typography.STYLE_MARKER)
    namespace = {}
    exec(compile(source, "<styled>", "exec"), namespace)
    applied = namespace["CITRUS_STYLE"]
    assert applied["style_version"] == typography.STYLE_VERSION
    assert applied["requested_stack"] == list(typography.LATIN_FAMILIES)
    assert "primary_missing" in applied
    # The prelude must not duplicate itself when a script is re-executed.
    again, second = typography.inject(source)
    assert again == source and second["policy"] == "already_styled"


def test_a_user_typography_choice_is_respected():
    for script in ("import matplotlib as mpl\nmpl.rcParams['font.family'] = 'serif'\n",
                   "from my_style import configure_style\nconfigure_style()\n",
                   "# CITRUS_STYLE_OVERRIDE\n"):
        source, record = typography.inject(script)
        assert record["policy"] == "user_override"
        assert source == script
        assert typography.STYLE_MARKER + "_BEGIN" not in source


def test_chinese_text_gets_a_separate_font_stack():
    latin, _ = typography.inject("pass\n")
    chinese, record = typography.inject("pass\n", chinese=True)
    assert record["chinese"] is True
    assert typography.CJK_FAMILIES[0] in chinese
    assert typography.CJK_FAMILIES[0] not in latin


def test_apply_style_reports_missing_family_with_the_fallback():
    report = typography.apply_style()
    assert report["style_version"] == typography.STYLE_VERSION
    assert report["resolved_family"]
    assert isinstance(report["warnings"], list)
    assert matplotlib.rcParams["pdf.fonttype"] == 42
    assert matplotlib.rcParams["font.sans-serif"][0] == typography.LATIN_FAMILIES[0]


def test_font_evidence_names_the_actual_file_and_embedding():
    figure, axis = plt.subplots()
    axis.set_title("表达量")
    axis.set_xlabel("处理")
    evidence = typography.font_evidence(figure, formats=("svg", "pdf"),
                                        pdf_bytes=b"%PDF-1.7 /FontFile2 12 0 R")
    assert evidence["style_version"] == typography.STYLE_VERSION
    assert evidence["text_objects"] >= 2
    assert evidence["resolved_family"]
    assert evidence["font_file"]
    assert evidence["embedding"]["pdf"] == "font_program_embedded"
    assert evidence["embedding"]["svg"] == "family_reference_only"
    missing = typography.font_evidence(figure, requested=["NotARealFamily"], formats=("svg",))
    assert missing["missing_family"] == ["NotARealFamily"]
    assert missing["warnings"] and "NotARealFamily" in missing["warnings"][0]
    plt.close(figure)


def test_embedding_report_is_honest_when_bytes_are_absent():
    assert typography.embedding_report(formats=("pdf",), pdf_bytes=None)["pdf"] == "not_provided"
    assert typography.embedding_report(formats=("svg",))["svg"] == "family_reference_only"
    assert typography.embedding_report(formats=("png",))["raster"] == "embedded_at_render_time"


def test_workbench_menu_is_the_verified_set_not_the_host_list():
    installed = ["DejaVu Sans", "Random Host Font", "Liberation Sans", "Arial"]
    menu = typography.verified_families(installed)
    assert set(menu) == {"DejaVu Sans", "Liberation Sans", "Arial"}
    assert "Random Host Font" not in menu
    assert typography.verified_families(["Only Some Host Font"]) == ["Only Some Host Font"]


def test_resolve_font_names_the_fallback_instead_of_hiding_it():
    resolved = typography.resolve_font("Arial", available=["DejaVu Sans"])
    assert resolved["missing"] is True
    assert resolved["resolved"] == "DejaVu Sans"
    assert resolved["font_file"]
    known = typography.resolve_font("DejaVu Sans", available=["DejaVu Sans"])
    assert known["missing"] is False and known["resolved"] == "DejaVu Sans"


def test_style_preamble_is_syntactically_valid_python():
    for chinese in (False, True):
        source = typography.style_preamble(chinese=chinese)
        compile(source, "<preamble>", "exec")
        assert source.count(typography.STYLE_MARKER + "_BEGIN") == 1


@pytest.mark.parametrize("text", ("", "   \n"))
def test_empty_source_is_not_styled(text):
    source, record = typography.inject(text)
    assert source == text
    assert record["policy"] == "empty"
