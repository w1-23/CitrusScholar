"""Delivery evidence for artifacts the General Agent produced with Code.

The Code pipeline is a generic delivery tool: it declares no output kinds.
A verified, request-bound artifact plus the format the user asked for is the
evidence; the host must not invent an obligation the user never stated.
"""
import unittest

from src.graph.export_contract import (
    artifact_matches_requirement,
    deliverable_contract,
    deliverable_match_groups,
)


def _artifact(name, mime, producer="code_deliver"):
    return {"link": f"/api/v2/session/s/artifacts/{name}", "artifact_id": name,
            "filename": name, "mime": mime, "producer_tool": producer,
            "request_matches": True, "valid": True, "exists": True,
            "owner_matches": True, "hash_matches": True, "format_matches": True}


class GenericDeliveryEvidenceTest(unittest.TestCase):
    def test_code_deliver_satisfies_stated_format(self):
        requirement = {"kinds": ["figure"], "formats": ["png"]}
        self.assertTrue(artifact_matches_requirement(
            _artifact("fig.png", "image/png"), requirement))

    def test_code_deliver_cannot_satisfy_unstated_kind(self):
        requirement = {"kinds": ["figure"], "formats": []}
        self.assertFalse(artifact_matches_requirement(
            _artifact("fig.png", "image/png"), requirement))

    def test_unknown_producer_is_still_rejected(self):
        requirement = {"kinds": ["figure"], "formats": ["png"]}
        self.assertFalse(artifact_matches_requirement(
            _artifact("fig.png", "image/png", producer="mystery_tool"), requirement))

    def test_format_mismatch_is_still_rejected(self):
        requirement = {"kinds": ["figure"], "formats": ["pdf"]}
        self.assertFalse(artifact_matches_requirement(
            _artifact("fig.png", "image/png"), requirement))

    def test_one_figure_in_two_formats_is_one_product(self):
        artifacts = [_artifact("fig.png", "image/png"),
                     _artifact("fig.pdf", "application/pdf")]
        query = ("画分组柱状图：x 轴按 genotype×treatment 分组。同时输出 PNG(300dpi) 与 PDF "
                 "两个文件，文件名 fig.png / fig.pdf。")
        requirements = [dict(item, all_formats=True) for item in deliverable_contract(query)]
        self.assertEqual(1, len(requirements))
        self.assertEqual(["pdf", "png"], sorted(requirements[0]["formats"]))
        self.assertEqual({0: (0, 1)}, deliverable_match_groups(artifacts, requirements))

    def test_no_svg_obligation_when_user_states_other_formats(self):
        query = "画一张柱状图，导出 PNG 和 PDF。"
        requirements = [dict(item, all_formats=True) for item in deliverable_contract(query)]
        self.assertFalse(any("svg" in (item.get("formats") or ()) for item in requirements))


if __name__ == "__main__":
    unittest.main()



def test_compact_drawing_prohibition_is_not_a_missing_deliverable():
    from src.graph.export_contract import deliverable_contract
    from src.graph.task_contract import contract_for_turn

    for query in ("只检查附件数值区块是否重复，给出证据和未覆盖范围，不绘图、不联网。",
                  "逐条核验附件中的文献 DOI 和题名，优先正式登记来源，不绘图。"):
        assert not deliverable_contract(query)
        assert not contract_for_turn(query, has_tables=True).deliverables
    assert [item["formats"] for item in deliverable_contract("不绘图，只给我一个 HTML 文件。") ] == [["html"]]
    assert deliverable_contract("不绘图，但请导出 SVG 文件。")[0]["formats"] == ["svg"]
