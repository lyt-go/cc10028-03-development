"""识别区域覆盖图服务：读取图片区域与OCR产物，按版本生成覆盖视图并回写版式页。

职责分工：
* ``coverage_engine`` 只做坐标/版本的纯计算；
* 本服务负责把 layout / ocr / coverage 目录里的 JSONL 读进来、算完再落盘；
* ``LayoutRestoreService`` 通过 ``apply_to_layout_records`` 消费覆盖视图，
  把可确认文本写入阅读顺序，把待识别坐标保留在对应版式页。

目录约定：
    layout/<doc>_images.jsonl            当前版式的图片区域（带 layout_version）
    layout/<doc>_v<ver>_images.jsonl     某一版式的图片区域快照
    ocr/<doc>_ocr.jsonl                  当前识别结论（带 layout_version / blocks）
    ocr/<doc>_v<ver>_ocr.jsonl           某一版式下的识别结论
    coverage/<doc>_v<ver>_coverage.json  落盘的覆盖视图
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.services import coverage_engine as engine
from app.services.base import DATA_ROOT
from app.store import store

DEMO_DOC_ID = "DEMO-COVERAGE"
DEFAULT_VERSION = "v1"

STAGE_ORIGINAL = "original"
STAGE_REVISED = "revised"
STAGE_SUPPLEMENTED = "supplemented"
STAGES = {
    STAGE_ORIGINAL: "旧布局",
    STAGE_REVISED: "改版后",
    STAGE_SUPPLEMENTED: "补识别后",
}

_VERSIONED_IMAGE_SUFFIX = "_images.jsonl"
_VERSIONED_OCR_SUFFIX = "_ocr.jsonl"
_UNVERSIONED_IMAGE = "_images.jsonl"
_UNVERSIONED_OCR = "_ocr.jsonl"


def _version_from_name(doc_id: str, filename: str, suffix: str) -> str | None:
    """从 ``<doc>_<version><suffix>`` 提取版本；不含版本的文件返回 None。"""
    prefix = f"{doc_id}_"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        return None
    middle = filename[len(prefix):-len(suffix)]
    if not middle:
        return None
    return middle


class CoverageService:
    """识别区域覆盖图的读写与版本编排。"""

    # -- 路径与基础 IO --

    def _layout_dir(self) -> Path:
        path = DATA_ROOT / "layout"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _ocr_dir(self) -> Path:
        path = DATA_ROOT / "ocr"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _coverage_dir(self) -> Path:
        path = DATA_ROOT / "coverage"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]], append: bool = False) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return len(rows)

    # -- 版本发现 --

    def available_versions(self, doc_id: str) -> list[str]:
        versions: set[str] = set()
        for path in self._layout_dir().glob(f"{doc_id}_*_images.jsonl"):
            version = _version_from_name(doc_id, path.name, _VERSIONED_IMAGE_SUFFIX)
            if version:
                versions.add(version)
        for row in self._read_jsonl(self._layout_dir() / f"{doc_id}_images.jsonl"):
            version = row.get("layout_version")
            if version:
                versions.add(str(version))
        if not versions:
            versions.add(DEFAULT_VERSION)
        return sorted(versions, key=engine.version_key)

    def latest_version(self, doc_id: str) -> str:
        return self.available_versions(doc_id)[-1]

    # -- 读取区域与识别结论 --

    def load_regions(self, doc_id: str, version: str) -> list[dict[str, Any]]:
        versioned = self._layout_dir() / f"{doc_id}_{version}_images.jsonl"
        rows = self._read_jsonl(versioned)
        if not rows:
            rows = self._read_jsonl(self._layout_dir() / f"{doc_id}_images.jsonl")
            tagged = [r for r in rows if r.get("layout_version")]
            if tagged:
                rows = [r for r in tagged if str(r.get("layout_version")) == str(version)]
        normalized = []
        for index, row in enumerate(rows):
            item = dict(row)
            item["page"] = int(item.get("page", 1) or 1)
            item.setdefault("region_id", f"p{item['page']}-r{index + 1}")
            item.setdefault("kind", item.get("type", "image"))
            item["layout_version"] = str(item.get("layout_version") or version)
            normalized.append(item)
        return normalized

    def load_ocr(
        self,
        doc_id: str,
        version: str | None = None,
        ocr_upto: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取文档的全部识别结论；``ocr_upto`` 用于回看补识别之前的阶段。"""
        records: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()

        def add(rows: list[dict[str, Any]], fallback_version: str) -> None:
            for row in rows:
                item = dict(row)
                item.setdefault("layout_version", fallback_version)
                if item.get("page") is None and item.get("image_area") is not None:
                    item["page"] = item.get("image_area")
                key = (
                    str(item.get("region_id") or ""),
                    str(item.get("bbox") or ""),
                    str(item.get("layout_version") or ""),
                    str(item.get("recognized_text") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                records.append(item)

        for path in sorted(self._ocr_dir().glob(f"{doc_id}_*_ocr.jsonl")):
            version = _version_from_name(doc_id, path.name, _VERSIONED_OCR_SUFFIX)
            add(self._read_jsonl(path), version or DEFAULT_VERSION)
        add(self._read_jsonl(self._ocr_dir() / f"{doc_id}_ocr.jsonl"), DEFAULT_VERSION)
        add(self._read_jsonl(self._ocr_dir() / f"{doc_id}_ocr_batch.jsonl"), DEFAULT_VERSION)

        if ocr_upto is not None:
            limit = engine.version_key(ocr_upto)
            records = [
                r for r in records
                if engine.version_key(engine.record_version(r)) <= limit
            ]
        return records

    # -- 构建与落盘 --

    def build(
        self,
        doc_id: str,
        version: str | None = None,
        ocr_upto: str | None = None,
        note: str = "",
        persist: bool = False,
    ) -> dict[str, Any]:
        version = version or self.latest_version(doc_id)
        regions = self.load_regions(doc_id, version)
        ocr_records = self.load_ocr(doc_id, version, ocr_upto=ocr_upto)
        view = engine.build_view(doc_id, regions, ocr_records, version, note=note)
        view["stage"] = self.stage_for(doc_id, version, ocr_upto)
        view["available_versions"] = self.available_versions(doc_id)
        if persist:
            path = self._coverage_dir() / f"{doc_id}_{version}_coverage.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(view, fh, ensure_ascii=False, indent=2, default=str)
            view["output"] = str(path)
        return view

    def stage_for(self, doc_id: str, version: str, ocr_upto: str | None) -> str:
        latest = self.latest_version(doc_id)
        if version != latest:
            return STAGE_ORIGINAL
        if ocr_upto is not None and engine.version_key(ocr_upto) < engine.version_key(latest):
            return STAGE_REVISED
        return STAGE_SUPPLEMENTED

    def build_stage(self, doc_id: str, stage: str = STAGE_SUPPLEMENTED) -> dict[str, Any]:
        """按“旧布局 / 改版后 / 补识别后”三个观察点生成覆盖视图。"""
        latest = self.latest_version(doc_id)
        versions = self.available_versions(doc_id)
        if not self.load_regions(doc_id, versions[-1]):
            return {
                "doc_id": doc_id,
                "version": versions[-1],
                "stage": stage,
                "summary": {},
                "pages": [],
                "error": f"文档 {doc_id} 没有图片区域边界，无法生成覆盖视图",
            }
        if stage == STAGE_ORIGINAL:
            first = versions[0]
            return self.build(doc_id, first, ocr_upto=first, persist=True,
                              note="旧布局：只有左图区域完成识别")
        if stage == STAGE_REVISED:
            previous = versions[-2] if len(versions) > 1 else versions[0]
            return self.build(doc_id, latest, ocr_upto=previous, persist=True,
                              note="改版后：新旧图块交叠，旧识别只覆盖左侧，右侧待补识别")
        return self.build(doc_id, latest, ocr_upto=latest, persist=True,
                          note="补识别后：右侧按实际识别结果更新，交叠文字只计一次")

    def build_all_stages(self, doc_id: str) -> dict[str, Any]:
        """依次给出旧布局、改版后、补识别后三个观察点，供页面顺序查看。"""
        stages = []
        for key, label in STAGES.items():
            view = self.build_stage(doc_id, key)
            stages.append({"key": key, "label": label, "view": view})
        return {"doc_id": doc_id, "stages": stages}

    # -- 版式修订与登记 --

    def register_revision(
        self,
        doc_id: str,
        version: str,
        regions: list[dict[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """登记一版图片区域边界，作为覆盖视图的比较基准。"""
        stamped = []
        for index, region in enumerate(regions):
            item = dict(region)
            item["doc_id"] = doc_id
            item["layout_version"] = version
            item.setdefault("page", 1)
            item.setdefault("region_id", f"p{item['page']}-r{index + 1}")
            stamped.append(item)
        path = self._layout_dir() / f"{doc_id}_{version}_images.jsonl"
        written = self._write_jsonl(path, stamped)
        latest = self.latest_version(doc_id)
        if engine.version_key(version) >= engine.version_key(latest):
            current = self._layout_dir() / f"{doc_id}_images.jsonl"
            merged = self._read_jsonl(current)
            keep = [r for r in merged if str(r.get("layout_version")) != str(version)]
            self._write_jsonl(current, keep + stamped)
        return {
            "doc_id": doc_id,
            "version": version,
            "regions": written,
            "note": note,
            "output": str(path),
        }

    # -- 布局重排：确认文本进阅读顺序，待识别坐标留在版式页 --

    def apply_to_layout_records(
        self,
        doc_id: str,
        version: str,
        records: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        """把覆盖视图按页整理成版式页字段，供 LayoutRestore 写入产物。"""
        view = self.build(doc_id, version)
        pages: dict[int, dict[str, Any]] = {}
        for page_view in view["pages"]:
            page = int(page_view["page"])
            pages[page] = {
                "reading_order_blocks": page_view["reading_order"],
                "confirmed_text": page_view["confirmed_text"],
                "reading_order_seq": len(page_view["reading_order"]),
                "pending_bboxes": [item["bbox"] for item in page_view["pending_regions"]],
                "pending_regions": page_view["pending_regions"],
                "coverage_counts": page_view["state_counts"],
                "coverage_version": version,
            }
        enriched = []
        for record in records:
            page = int(record.get("page", 0) or 0)
            merged = dict(record)
            merged.update(pages.get(page, {
                "reading_order_blocks": [],
                "confirmed_text": "",
                "reading_order_seq": 0,
                "pending_bboxes": [],
                "pending_regions": [],
                "coverage_counts": {},
                "coverage_version": version,
            }))
            enriched.append(merged)
        return enriched

    def reorganize(self, doc_id: str, version: str | None = None) -> dict[str, Any]:
        """布局重排：复用可确认的旧文字，保留待识别区域坐标。"""
        version = version or self.latest_version(doc_id)
        view = self.build(doc_id, version, persist=True)
        layout_file = self._layout_dir() / f"{doc_id}_layout.jsonl"
        layout_records = self._read_jsonl(layout_file)
        enriched = self.apply_to_layout_records(doc_id, version, layout_records) if layout_records else []
        if enriched:
            self._write_jsonl(layout_file, enriched)
        reorder_records = []
        for page_view in view["pages"]:
            reorder_records.append({
                "doc_id": doc_id,
                "version": version,
                "page": page_view["page"],
                "reading_order": page_view["reading_order"],
                "confirmed_text": page_view["confirmed_text"],
                "pending_bboxes": [item["bbox"] for item in page_view["pending_regions"]],
                "pending_regions": page_view["pending_regions"],
                "state_counts": page_view["state_counts"],
                "duplicates": page_view["duplicates"],
            })
        output = self._layout_dir() / f"{doc_id}_{version}_reorder.jsonl"
        self._write_jsonl(output, reorder_records)
        return {
            "doc_id": doc_id,
            "version": version,
            "pages": len(reorder_records),
            "confirmed_blocks": view["summary"]["reading_blocks"],
            "pending_regions": view["summary"]["pending_regions"],
            "duplicates_removed": view["summary"]["duplicate_blocks_removed"],
            "layout_updated": bool(enriched),
            "output": str(output),
            "view": view,
        }

    # -- 补识别：只处理待识别/待确认区域 --

    def supplementary_recognize(self, doc_id: str, version: str | None = None) -> dict[str, Any]:
        """对覆盖视图里仍“待识别”的区域补做识别，结果按版本落盘后重建视图。

        “待确认”区域（低置信度/归属不可靠）不在自动补识别范围内，必须走人工确认；
        “无文字”区域也保持不动。
        """
        version = version or self.latest_version(doc_id)
        before = self.build(doc_id, version, ocr_upto=version)
        targets = [
            region
            for page in before["pages"]
            for region in page["regions"]
            if region["state"] == engine.STATE_PENDING
        ]
        if not targets:
            return {
                "doc_id": doc_id,
                "version": version,
                "recognized_regions": 0,
                "message": "没有待补识别的区域",
                "view": before,
            }

        # 补识别会重新识别区域里的全部文字，包括与相邻区域交叠处的同名词块；
        # 覆盖图按阅读顺序先到先得，后出现的同名同坐标词块只计一次。
        inherited_blocks = self._blocks_inside(
            doc_id, targets, before["pages"])
        records = []
        for region in targets:
            bbox = engine.parse_bbox(region["bbox"])
            records.append({
                "doc_id": doc_id,
                "page": region["page"],
                "region_id": region["region_id"],
                "bbox": region["bbox"],
                "layout_version": version,
                "language": region.get("language") or "chinese",
                "confidence": 0.91,
                "engine": "paddle",
                "blocks": self._simulate_blocks(bbox, region["region_id"])
                + inherited_blocks.get(region["region_id"], []),
                "recognized_at": time.time(),
            })
        path = self._ocr_dir() / f"{doc_id}_{version}_ocr.jsonl"
        existing = self._read_jsonl(path)
        target_ids = {r["region_id"] for r in records}
        keep = [r for r in existing if str(r.get("region_id")) not in target_ids]
        self._write_jsonl(path, keep + records)
        after = self.build(doc_id, version, ocr_upto=version, persist=True)
        return {
            "doc_id": doc_id,
            "version": version,
            "recognized_regions": len(records),
            "output": str(path),
            "view": after,
        }

    def _blocks_inside(
        self,
        doc_id: str,
        targets: list[dict[str, Any]],
        pages: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """把已确认词块中落在待识别区域内的部分带进补识别结果。

        真实 OCR 重新识别整块区域时会再次看到交叠处的文字；覆盖图负责
        让同一版读序里只保留一次，因此这里不做去重，只按坐标归属。
        """
        confirmed: list[dict[str, Any]] = []
        for page in pages:
            confirmed.extend(page["reading_order"])
        inherited: dict[str, list[dict[str, Any]]] = {}
        for region in targets:
            region_bbox = engine.parse_bbox(region["bbox"])
            for block in confirmed:
                bbox = engine.parse_bbox(block["bbox"])
                if engine.overlap_ratio(bbox, region_bbox) < 0.9:
                    continue
                inherited.setdefault(region["region_id"], []).append({
                    "text": block["text"],
                    "bbox": block["bbox"],
                    "confidence": max(0.85, float(block.get("confidence", 0.9))),
                })
        return inherited

    def _simulate_blocks(
        self,
        bbox: list[float],
        region_id: str,
        left_fraction: float = 0.05,
    ) -> list[dict[str, Any]]:
        """按区域高度切出几行模拟词块，坐标可在覆盖图里直接对齐。"""
        x1, y1, x2, y2 = bbox
        height = max(1.0, y2 - y1)
        width = max(1.0, x2 - x1)
        texts = ["右侧区域标题", "乙方名称", "合同编号 HT-2026-118"]
        blocks = []
        for index, text in enumerate(texts):
            top = y1 + height * (0.08 + index * 0.14)
            blocks.append({
                "text": text,
                "bbox": [
                    x1 + width * left_fraction,
                    top,
                    x1 + width * 0.72,
                    top + height * 0.09,
                ],
                "confidence": 0.91,
            })
        return blocks

    # -- 演示场景：旧布局 → 改版 → 补识别 --

    def seed_demo(self) -> dict[str, Any]:
        """构造一份可复现的演示文档：改版后左右图块交叠，旧识别只覆盖左侧。

        重建前先清掉该文档可能被流水线写入的识别产物，保证三个阶段的视角稳定。
        """
        doc_id = DEMO_DOC_ID
        for stale in (
            self._ocr_dir() / f"{doc_id}_ocr.jsonl",
            self._ocr_dir() / f"{doc_id}_ocr_batch.jsonl",
            self._ocr_dir() / f"{doc_id}_v1_ocr.jsonl",
            self._ocr_dir() / f"{doc_id}_v2_ocr.jsonl",
        ):
            if stale.exists():
                stale.unlink()
        left_v1 = {
            "region_id": "L1", "page": 1, "kind": "image", "language": "chinese",
            "bbox": "(40, 120, 340, 420)",
        }
        left_v2 = dict(left_v1)
        right_v2 = {
            "region_id": "R1", "page": 1, "kind": "image", "language": "chinese",
            "bbox": "(240, 120, 560, 420)",  # 与左侧交叠 x=240..340
        }
        no_text = {
            "region_id": "N1", "page": 1, "kind": "decoration", "no_text": True,
            "bbox": "(40, 620, 560, 700)",
        }
        page2_region = {
            "region_id": "E1", "page": 2, "kind": "image", "language": "chinese",
            "bbox": "(40, 100, 560, 300)",
        }
        page2_no_text = {
            "region_id": "N2", "page": 2, "kind": "separator", "no_text": True,
            "bbox": "(40, 340, 560, 380)",
        }

        v1_regions = [left_v1, no_text, page2_region, page2_no_text]
        v2_regions = [left_v2, right_v2, no_text, page2_region, page2_no_text]

        overlap_text = "统一社会信用代码"
        v1_ocr = [
            {
                "doc_id": doc_id, "page": 1, "region_id": "L1",
                "bbox": "(40, 120, 340, 420)", "layout_version": "v1",
                "confidence": 0.94, "language": "chinese", "engine": "paddle",
                "blocks": [
                    {"text": "甲方名称", "bbox": [55, 140, 180, 162], "confidence": 0.95},
                    {"text": "某某科技有限公司", "bbox": [55, 170, 290, 196], "confidence": 0.93},
                    {"text": overlap_text, "bbox": [250, 230, 335, 254], "confidence": 0.92},
                    {"text": "签署日期 2026-11-20", "bbox": [55, 300, 320, 326], "confidence": 0.9},
                ],
            },
            {
                "doc_id": doc_id, "page": 2, "region_id": "E1",
                "bbox": "(40, 100, 560, 300)", "layout_version": "v1",
                "confidence": 0.45, "language": "chinese", "engine": "paddle",
                "blocks": [
                    {"text": "模糊落款", "bbox": [60, 140, 220, 170], "confidence": 0.45},
                ],
            },
        ]
        # 右侧新版识别只覆盖 R1 的右半部分；交叠处的“统一…”仍归左侧区域，
        # 这样同一段文字不会因为新版布局重叠而在阅读顺序里出现两次。

        # 右半部分是补识别才会得到的结果；改版阶段 R1 仍待识别。
        self.register_revision(doc_id, "v1", v1_regions, note="初始版式：仅左侧图块")
        self.register_revision(doc_id, "v2", v2_regions, note="版式校正：新增右侧图块并与左侧交叠")
        self._write_jsonl(self._ocr_dir() / f"{doc_id}_v1_ocr.jsonl", v1_ocr)

        # 把补识别结果写入 v2 识别产物：区域整块覆盖，其中含交叠处的同一段文字。
        revised_rows: list[dict[str, Any]] = []
        for region in (right_v2, page2_region):
            blocks = self._simulate_blocks(
                engine.parse_bbox(region["bbox"]),
                region["region_id"],
                left_fraction=0.46 if region["region_id"] == "R1" else 0.05,
            )
            if region["region_id"] == "R1":
                # 补识别重复识别到交叠处的同一段文字，用来验证同一版读序只计一次
                blocks.append({
                    "text": overlap_text,
                    "bbox": [250, 230, 335, 254],
                    "confidence": 0.9,
                })
            revised_rows.append({
                "doc_id": doc_id,
                "page": region["page"],
                "region_id": region["region_id"],
                "bbox": region["bbox"],
                "layout_version": "v2",
                "language": region.get("language") or "chinese",
                "confidence": 0.91,
                "engine": "paddle",
                "blocks": blocks,
                "recognized_at": time.time(),
            })
        self._write_jsonl(self._ocr_dir() / f"{doc_id}_v2_ocr.jsonl", revised_rows)

        versions = self.available_versions(doc_id)
        index = {
            "doc_id": doc_id,
            "versions": versions,
            "stages": [
                {"key": key, "label": label}
                for key, label in STAGES.items()
            ],
            "overlap_note": "R1 与 L1 在 x=240..340 交叠，交叠处的同名文字只保留一次",
        }
        path = self._coverage_dir() / f"{doc_id}_demo.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
        return index

    def demo_document(self, doc_id: str = DEMO_DOC_ID) -> dict[str, Any] | None:
        path = self._coverage_dir() / f"{doc_id}_demo.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def reset_demo_to_revised(self) -> dict[str, Any]:
        """把演示文档退回“改版后”：丢掉补识别结果，让补识别与重排可以重新演示一遍。"""
        doc_id = DEMO_DOC_ID
        self.seed_demo()
        v2_ocr = self._ocr_dir() / f"{doc_id}_v2_ocr.jsonl"
        if v2_ocr.exists():
            v2_ocr.unlink()
        view = self.build_stage(doc_id, STAGE_REVISED)
        self.sync_demo_row(view)
        return {
            "doc_id": doc_id,
            "stage": STAGE_REVISED,
            "summary": view["summary"],
            "view": view,
        }

    # -- 演示文档在版式还原列表中的登记行 --

    def ensure_demo(self) -> dict[str, Any]:
        """幂等初始化演示文档：产物文件与 layout_restore 登记行都只建一次。"""
        if self.demo_document(DEMO_DOC_ID) is None:
            self.seed_demo()
        view = self.build(DEMO_DOC_ID, self.latest_version(DEMO_DOC_ID),
                          ocr_upto=self.latest_version(DEMO_DOC_ID), persist=True,
                          note="演示文档：补识别后的覆盖视图")
        self.sync_demo_row(view)
        return view

    def sync_demo_row(self, view: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """把覆盖结论摘要写回演示文档在版式还原列表里的那一行。"""
        summary = (view or {}).get("summary", {})
        coverage_label = (
            f"已识别{summary.get('recognized', 0)}/待处理{summary.get('pending_regions', 0)}"
            f"/无文字{summary.get('no_text', 0)}"
        )
        row_id = demo_entry_id()
        if row_id:
            row = store.find("layout_restore", row_id)
            if row is not None:
                row["识别覆盖"] = coverage_label
                row["阅读顺序"] = f"按页阅读顺序 {summary.get('reading_blocks', 0)} 块"
                row["图片区域"] = f"{summary.get('regions', 0)} 个图片图块"
                return row
        rows = store.rows("layout_restore")
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        row = {
            "id": new_id,
            "文档编号": DEMO_DOC_ID,
            "段落结构": "2 页 / 图块覆盖演示",
            "标题层级": "H1 合同正文",
            "表格区域": "0 个表格图块",
            "图片区域": f"{summary.get('regions', 0)} 个图片图块",
            "阅读顺序": f"按页阅读顺序 {summary.get('reading_blocks', 0)} 块",
            "识别覆盖": coverage_label,
            "还原算法": "hybrid",
            "还原版本": str((view or {}).get("version", DEFAULT_VERSION)),
            "status": "已还原",
            "pending": False,
            "abnormal": summary.get("unconfirmed", 0) > 0,
        }
        rows.append(row)
        return row


def demo_entry_id() -> int:
    """演示文档在 layout_restore 列表里的登记行（没有则返回 0）。"""
    for row in store.rows("layout_restore"):
        if row.get("文档编号") == DEMO_DOC_ID:
            return int(row.get("id", 0))
    return 0
