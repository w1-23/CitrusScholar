"""Stored renderer identities, separate from model-visible tool capabilities.

Old artifact producer names remain immutable and readable after consolidation.
The unified entrypoint's union of possible outputs is never evidence of what
one particular file contains.
"""
RENDERER_OUTPUT_KINDS = {
    "export_table": frozenset({"table_file"}),
    "export_literature_evidence": frozenset({"bibliography"}),
    "export_paper_analysis": frozenset({"report_html"}),
    "export_experiment_design": frozenset({"report_html"}),
    "export_research_notes": frozenset({"report_html"}),
    "export_corpus_gap_report": frozenset({"gap_csv"}),
    "render_research_report": frozenset({"report_html"}),
    "figure_workspace": frozenset({"figure", "figure_package"}),
    "run_nature_plot": frozenset({"figure", "figure_package"}),
    "collect_external_skill": frozenset({"annotation_table"}),  # Historical immutable renderer ID.
    "fusion_annotated_workbook": frozenset({"annotation_table", "table_file"}),
    "fusion_report_table": frozenset({"table_file"}),
    "fusion_report_html": frozenset({"report_html"}),
    "fusion_report_json": frozenset({"report_json"}),
    "fusion_report_docx": frozenset({"report_docx"}),
    "fusion_report_pdf": frozenset({"report_html"}),
    "fusion_reference_table": frozenset({"verification_sheet", "bibliography", "table_file"}),
    "fusion_reference_json": frozenset({"bibliography", "report_json"}),
    "fusion_reference_workbook": frozenset({"verification_sheet", "bibliography", "table_file"}),
}
