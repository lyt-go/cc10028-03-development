"""识别区域覆盖图测试：区域状态判定、交叠去重、版本归属与版式重排消费。"""
import os
import sys
import uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services import coverage_engine as engine
from app.services.coverage import CoverageService
from app.services.format_parse import FormatParseService
from app.services.layout_restore import LayoutRestoreService
from app.services.base import DATA_ROOT


def _uid(prefix="DOC"):
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _block(text, x1, y1, x2, y2, confidence=0.9):
    return {"text": text, "bbox": [x1, y1, x2, y2], "confidence": confidence}


class TestBboxMath:
    def test_parse_bbox_formats(self):
        assert engine.parse_bbox("(1, 2, 30, 40)") == [1.0, 2.0, 30.0, 40.0]
        assert engine.parse_bbox([10, 20, 30, 40]) == [10.0, 20.0, 30.0, 40.0]
        assert engine.parse_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4}) == [1.0, 2.0, 3.0, 4.0]

    def test_overlap_ratio_tracks_containment(self):
        outer = [0, 0, 100, 100]
        assert engine.overlap_ratio([10, 10, 20, 20], outer) == pytest.approx(1.0)
        assert engine.overlap_ratio([50, 50, 150, 150], outer) == pytest.approx(0.25)
        assert engine.overlap_ratio([200, 200, 300, 300], outer) == 0.0

    def test_version_key_sorts_numerically(self):
        assert sorted(["v10", "v2", "v1"], key=engine.version_key) == ["v1", "v2", "v10"]


class TestRegionStateClassification:
    def test_partial_old_recognition_is_pending_not_reuse(self):
        regions = [{"region_id": "R1", "page": 1, "bbox": [240, 120, 560, 420]}]
        # 旧识别的框只覆盖新区域左侧 1/3
        ocr = [{
            "region_id": "R1", "page": 1, "layout_version": "v1", "confidence": 0.9,
            "bbox": [240, 120, 340, 420],
            "blocks": [_block("左侧词", 250, 150, 330, 180)],
        }]
        view = engine.build_view("D", regions, ocr, "v2")
        region = view["pages"][0]["regions"][0]
        assert region["state"] == engine.STATE_PENDING
        assert "仍需补识别" in region["reason"]
        assert view["pages"][0]["reading_order"] == []
        assert view["pages"][0]["pending_regions"][0]["bbox"] == [240.0, 120.0, 560.0, 420.0]

    def test_full_old_recognition_is_reused(self):
        regions = [{"region_id": "L1", "page": 1, "bbox": [40, 120, 340, 420]}]
        ocr = [{
            "region_id": "L1", "page": 1, "layout_version": "v1", "confidence": 0.94,
            "bbox": [40, 120, 340, 420],
            "blocks": [_block("甲方名称", 55, 140, 180, 162)],
        }]
        view = engine.build_view("D", regions, ocr, "v2")
        region = view["pages"][0]["regions"][0]
        assert region["state"] == engine.STATE_RECOGNIZED
        assert region["reused"] is True
        assert region["source_version"] == "v1"
        assert [b["text"] for b in view["pages"][0]["reading_order"]] == ["甲方名称"]

    def test_low_confidence_is_unconfirmed(self):
        regions = [{"region_id": "E1", "page": 2, "bbox": [40, 100, 560, 300]}]
        ocr = [{
            "region_id": "E1", "page": 2, "layout_version": "v2", "confidence": 0.42,
            "bbox": [40, 100, 560, 300],
            "blocks": [_block("模糊落款", 60, 140, 220, 170, confidence=0.42)],
        }]
        view = engine.build_view("D", regions, ocr, "v2")
        region = view["pages"][0]["regions"][0]
        assert region["state"] == engine.STATE_UNCONFIRMED
        assert view["pages"][0]["reading_order"] == []
        assert view["summary"]["unconfirmed"] == 1
        assert view["summary"]["pending"] == 0

    def test_no_text_region_is_distinct_from_unconfirmed(self):
        regions = [
            {"region_id": "N1", "page": 1, "kind": "decoration", "bbox": [0, 0, 100, 40]},
            {"region_id": "U1", "page": 1, "bbox": [0, 60, 100, 100]},
        ]
        ocr = [{
            "region_id": "U1", "page": 1, "layout_version": "v1", "confidence": 0.3,
            "bbox": [0, 60, 100, 100],
            "blocks": [_block("低置信", 5, 65, 90, 90, confidence=0.3)],
        }]
        view = engine.build_view("D", regions, ocr, "v1")
        states = {r["region_id"]: r["state"] for r in view["pages"][0]["regions"]}
        assert states["N1"] == engine.STATE_NO_TEXT
        assert states["U1"] == engine.STATE_UNCONFIRMED
        assert view["summary"]["no_text"] == 1
        assert view["summary"]["unconfirmed"] == 1

    def test_region_without_any_recognition_is_pending(self):
        regions = [{"region_id": "R1", "page": 1, "bbox": [0, 0, 100, 100]}]
        view = engine.build_view("D", regions, [], "v1")
        region = view["pages"][0]["regions"][0]
        assert region["state"] == engine.STATE_PENDING
        assert view["pages"][0]["reading_order"] == []


class TestOverlapDeduplication:
    def test_shared_text_in_overlapping_regions_counted_once(self):
        regions = [
            {"region_id": "L1", "page": 1, "bbox": [40, 120, 340, 420]},
            {"region_id": "R1", "page": 1, "bbox": [240, 120, 560, 420]},
        ]
        shared = _block("统一社会信用代码", 250, 230, 335, 254)
        ocr = [
            {"region_id": "L1", "page": 1, "layout_version": "v2", "confidence": 0.9,
             "bbox": [40, 120, 340, 420], "blocks": [_block("甲方名称", 55, 140, 180, 162), shared]},
            {"region_id": "R1", "page": 1, "layout_version": "v2", "confidence": 0.9,
             "bbox": [240, 120, 560, 420],
             "blocks": [dict(shared), _block("乙方名称", 350, 140, 470, 162)]},
        ]
        view = engine.build_view("D", regions, ocr, "v2")
        page = view["pages"][0]
        texts = [b["text"] for b in page["reading_order"]]
        assert texts.count("统一社会信用代码") == 1
        assert len(page["duplicates"]) == 1
        assert page["duplicates"][0]["kept_in_region_id"] == "L1"
        assert page["duplicates"][0]["region_id"] == "R1"
        assert page["confirmed_text"].count("统一社会信用代码") == 1
        assert view["summary"]["overlap_pairs"] == 1

    def test_same_text_in_non_overlapping_regions_not_deduplicated(self):
        regions = [
            {"region_id": "A", "page": 1, "bbox": [0, 0, 100, 100]},
            {"region_id": "B", "page": 1, "bbox": [200, 0, 300, 100]},
        ]
        ocr = [
            {"region_id": "A", "page": 1, "layout_version": "v1", "confidence": 0.9,
             "bbox": [0, 0, 100, 100], "blocks": [_block("编号", 10, 10, 90, 40)]},
            {"region_id": "B", "page": 1, "layout_version": "v1", "confidence": 0.9,
             "bbox": [200, 0, 300, 100], "blocks": [_block("编号", 210, 10, 290, 40)]},
        ]
        view = engine.build_view("D", regions, ocr, "v1")
        assert len(view["pages"][0]["duplicates"]) == 0
        assert len(view["pages"][0]["reading_order"]) == 2


class TestCoverageServiceDocuments:
    def _doc(self):
        doc_id = _uid("COV")
        svc = CoverageService()
        svc.register_revision(doc_id, "v1", [
            {"region_id": "L1", "page": 1, "bbox": "(40, 120, 340, 420)"},
            {"region_id": "N1", "page": 1, "kind": "decoration", "bbox": "(40, 620, 560, 700)"},
        ])
        svc.register_revision(doc_id, "v2", [
            {"region_id": "L1", "page": 1, "bbox": "(40, 120, 340, 420)"},
            {"region_id": "R1", "page": 1, "bbox": "(240, 120, 560, 420)"},
            {"region_id": "N1", "page": 1, "kind": "decoration", "bbox": "(40, 620, 560, 700)"},
        ])
        svc._write_jsonl(DATA_ROOT / "ocr" / f"{doc_id}_v1_ocr.jsonl", [{
            "region_id": "L1", "page": 1, "layout_version": "v1", "confidence": 0.93,
            "bbox": "(40, 120, 340, 420)",
            "blocks": [
                {"text": "甲方名称", "bbox": [55, 140, 180, 162], "confidence": 0.95},
                {"text": "统一社会信用代码", "bbox": [250, 230, 335, 254], "confidence": 0.9},
            ],
        }])
        return doc_id, svc

    def test_stage_sequence_matches_expected_transitions(self):
        doc_id, svc = self._doc()
        stages = svc.build_all_stages(doc_id)
        views = {item["key"]: item["view"] for item in stages["stages"]}
        assert [item["key"] for item in stages["stages"]] == ["original", "revised", "supplemented"]

        original = views["original"]
        revised = views["revised"]

        # 旧布局：左侧已识别，右侧区域还不存在
        assert original["version"] == "v1"
        assert [r["region_id"] for r in original["pages"][0]["regions"]] == ["L1", "N1"]

        # 改版后：右侧新区域待识别，左侧沿用旧识别
        assert revised["version"] == "v2"
        states = {r["region_id"]: r["state"] for r in revised["pages"][0]["regions"]}
        assert states["L1"] == engine.STATE_RECOGNIZED
        assert states["R1"] == engine.STATE_PENDING
        assert revised["pages"][0]["pending_regions"][0]["bbox"] == [240.0, 120.0, 560.0, 420.0]

        # 补识别后：右侧按实际识别结果更新，交叠文字只计一次
        svc.supplementary_recognize(doc_id, "v2")
        supplemented = svc.build_stage(doc_id, "supplemented")
        states = {r["region_id"]: r["state"] for r in supplemented["pages"][0]["regions"]}
        assert states["R1"] == engine.STATE_RECOGNIZED
        texts = [b["text"] for b in supplemented["pages"][0]["reading_order"]]
        assert texts.count("统一社会信用代码") == 1

    def test_ocr_upto_hides_supplementary_results(self):
        doc_id, svc = self._doc()
        svc.supplementary_recognize(doc_id, "v2")
        before = svc.build(doc_id, "v2", ocr_upto="v1")
        after = svc.build(doc_id, "v2", ocr_upto="v2")
        states_before = {r["region_id"]: r["state"] for r in before["pages"][0]["regions"]}
        states_after = {r["region_id"]: r["state"] for r in after["pages"][0]["regions"]}
        assert states_before["R1"] == engine.STATE_PENDING
        assert states_after["R1"] == engine.STATE_RECOGNIZED

    def test_supplement_only_targets_pending_regions(self):
        doc_id, svc = self._doc()
        result = svc.supplementary_recognize(doc_id, "v2")
        assert result["recognized_regions"] == 1
        assert result["view"]["summary"]["pending_regions"] == 0
        again = svc.supplementary_recognize(doc_id, "v2")
        assert again["recognized_regions"] == 0

    def test_reorganize_keeps_pending_coordinates(self):
        doc_id, svc = self._doc()
        result = svc.reorganize(doc_id, "v2")
        assert result["confirmed_blocks"] == 2
        assert result["pending_regions"] == 1
        rows = svc._read_jsonl(DATA_ROOT / "layout" / f"{doc_id}_v2_reorder.jsonl")
        page = rows[0]
        assert page["confirmed_text"].count("统一社会信用代码") == 1
        assert page["pending_bboxes"] == [[240.0, 120.0, 560.0, 420.0]]


class TestLayoutRestoreConsumesCoverage:
    def _revised_doc(self):
        """只登记图片区域和旧版识别结果，模拟“改版后、尚未补识别”的文档。"""
        doc_id = _uid("LAY")
        svc = CoverageService()
        svc.register_revision(doc_id, "v1", [
            {"region_id": "L1", "page": 1, "bbox": "(40, 120, 340, 420)"},
            {"region_id": "N1", "page": 1, "kind": "decoration", "bbox": "(40, 620, 560, 700)"},
        ])
        svc.register_revision(doc_id, "v2", [
            {"region_id": "L1", "page": 1, "bbox": "(40, 120, 340, 420)"},
            {"region_id": "R1", "page": 1, "bbox": "(240, 120, 560, 420)"},
            {"region_id": "N1", "page": 1, "kind": "decoration", "bbox": "(40, 620, 560, 700)"},
        ])
        svc._write_jsonl(DATA_ROOT / "ocr" / f"{doc_id}_v1_ocr.jsonl", [{
            "region_id": "L1", "page": 1, "layout_version": "v1", "confidence": 0.93,
            "bbox": "(40, 120, 340, 420)",
            "blocks": [
                {"text": "甲方名称", "bbox": [55, 140, 180, 162], "confidence": 0.95},
                {"text": "统一社会信用代码", "bbox": [250, 230, 335, 254], "confidence": 0.9},
            ],
        }])
        return doc_id, svc

    def test_layout_records_carry_confirmed_text_and_pending_bboxes(self):
        doc_id, _ = self._revised_doc()

        # 直接写入解析产物，避免 execute_parse 自动触发 OCR 把待识别区域补掉
        parsed = [{
            "doc_id": doc_id, "page_num": page, "paragraphs": 3,
            "text_length": 120, "has_tables": False, "has_images": True,
            "parser": "PDF", "content_hash": "test",
        } for page in (1, 2)]
        CoverageService()._write_jsonl(
            DATA_ROOT / "parsed" / f"{doc_id}_parsed.jsonl", parsed)

        result = LayoutRestoreService().restore_from_document(doc_id)
        assert "error" not in result, result.get("error", "")
        assert result["coverage"]["pending_regions"] == 1

        records = CoverageService()._read_jsonl(DATA_ROOT / "layout" / f"{doc_id}_layout.jsonl")
        page1 = next(r for r in records if r["page"] == 1)
        assert page1["coverage_version"] == "v2"
        assert page1["confirmed_text"] == "甲方名称统一社会信用代码"
        # 待识别坐标保留到对应版式页
        assert page1["pending_bboxes"] == [[240.0, 120.0, 560.0, 420.0]]
        assert page1["coverage_counts"]["no_text"] == 1

    def test_demo_stage_coverage_summary(self):
        svc = CoverageService()
        svc.seed_demo()
        doc_id = "DEMO-COVERAGE"

        parse_svc = FormatParseService()
        task, _ = parse_svc.create_entry({
            "任务编号": _uid("PR"), "文档编号": doc_id, "解析器类型": "PDF",
        })
        parse_svc.execute_parse(task["id"])
        result = LayoutRestoreService().restore_from_document(doc_id)
        assert "error" not in result, result.get("error", "")
        assert result["coverage"]["regions"] == 5

        records = CoverageService()._read_jsonl(DATA_ROOT / "layout" / f"{doc_id}_layout.jsonl")
        page1 = next(r for r in records if r["page"] == 1)
        assert page1["coverage_version"] == "v2"
        assert "甲方名称" in page1["confirmed_text"]
        # 交叠文字只出现一次
        assert page1["confirmed_text"].count("统一社会信用代码") == 1
        assert page1["coverage_counts"]["no_text"] == 1

    def test_demo_stages_left_not_duplicated_right_updates(self):
        svc = CoverageService()
        # 重新生成演示文档，避免同一用例库内其它用例补识别或重排后污染阶段视角
        svc.seed_demo()
        stages = svc.build_all_stages("DEMO-COVERAGE")
        views = {item["key"]: item["view"] for item in stages["stages"]}
        for key in ("original", "revised", "supplemented"):
            texts = [
                b["text"]
                for page in views[key]["pages"]
                for b in page["reading_order"]
            ]
            assert texts.count("甲方名称") == 1, key
            assert texts.count("统一社会信用代码") == 1, key
        revised_right = [
            r for r in views["revised"]["pages"][0]["regions"] if r["region_id"] == "R1"
        ][0]
        supplemented_right = [
            r for r in views["supplemented"]["pages"][0]["regions"] if r["region_id"] == "R1"
        ][0]
        assert revised_right["state"] == engine.STATE_PENDING
        assert supplemented_right["state"] == engine.STATE_RECOGNIZED
        assert "乙方名称" in supplemented_right["recognized_text"]
        # 交叠处的文字由左侧区域先认领，右侧同一段文字判为重复
        assert supplemented_right["blocks"][-1]["duplicate"] is True
        assert views["supplemented"]["summary"]["duplicate_blocks_removed"] == 1
