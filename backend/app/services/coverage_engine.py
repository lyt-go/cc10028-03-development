"""识别区域覆盖图引擎：纯计算，不读写文件。

版式还原只给出图片区域边界，判断不了“这块区域是否已经有文字”。这里把
图片区域定义（bbox/region_id/版式版本）与 OCR 识别结论（bbox/文本块/版本）
对齐，按页产出覆盖视图：

* 已识别：当前版式下有可用文字（同版识别，或旧版识别完整落在本区域内可继续用）
* 待识别：区域还没有可用文字（含旧识别只覆盖了一部分，仍需补识别）
* 待确认：有识别候选，但版本/置信度/归属不可靠，不能直接放入阅读顺序
* 无文字：区域被明确标注为没有文字，与“暂未确认”严格区分

同一页里两块区域交叠时，同一段文字只归属一次：按阅读顺序先到先得，
后处理的区域遇到同坐标同文本的词块会判为重复并从阅读顺序中剔除。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

STATE_RECOGNIZED = "已识别"
STATE_PENDING = "待识别"
STATE_UNCONFIRMED = "待确认"
STATE_NO_TEXT = "无文字"

STATE_KEYS = {
    STATE_RECOGNIZED: "recognized",
    STATE_PENDING: "pending",
    STATE_UNCONFIRMED: "unconfirmed",
    STATE_NO_TEXT: "no_text",
}

# 两条识别记录/词块的重叠达到该比例才视为同一处内容
BLOCK_MATCH_RATIO = 0.5
# OCR 记录至少这么大比例落在区域内才参与归属判断
MIN_ATTRIBUTION_RATIO = 0.45
# 记录几乎完整落在区域内才可直接复用（允许少量边界误差）
FULL_CONTAIN_RATIO = 0.90
# 低于该置信度的识别结果只能进入待确认
MIN_CONFIDENCE = 0.60

NO_TEXT_KINDS = {"no_text", "decoration", "separator", "空白", "无文字"}

_BBOX_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_bbox(value: Any) -> list[float]:
    """把 "(x1, y1, x2, y2)"、[x1,y1,x2,y2] 或 dict 统一成有序矩形。"""
    if isinstance(value, dict):
        return [
            float(value.get("x1", value.get("left", 0))),
            float(value.get("y1", value.get("top", 0))),
            float(value.get("x2", value.get("right", 0))),
            float(value.get("y2", value.get("bottom", 0))),
        ]
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        nums = [float(v) for v in value[:4]]
    else:
        nums = [float(v) for v in _BBOX_NUMBER.findall(str(value or ""))[:4]]
    if len(nums) < 4:
        return [0.0, 0.0, 0.0, 0.0]
    x1, y1, x2, y2 = nums
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def bbox_area(bbox: Iterable[float]) -> float:
    x1, y1, x2, y2 = list(bbox)[:4]
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def intersection(a: Iterable[float], b: Iterable[float]) -> list[float]:
    ax1, ay1, ax2, ay2 = list(a)[:4]
    bx1, by1, bx2, by2 = list(b)[:4]
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    if x2 <= x1 or y2 <= y1:
        return [0.0, 0.0, 0.0, 0.0]
    return [x1, y1, x2, y2]


def overlap_ratio(inner: Iterable[float], outer: Iterable[float]) -> float:
    """inner 有多大比例落在 outer 内。"""
    inner_area = bbox_area(inner)
    if inner_area <= 0:
        return 0.0
    return bbox_area(intersection(inner, outer)) / inner_area


def same_block(a: Iterable[float], b: Iterable[float]) -> bool:
    """两个词块坐标是否指向同一处文字（双向重叠都够高）。"""
    return overlap_ratio(a, b) >= BLOCK_MATCH_RATIO and overlap_ratio(b, a) >= BLOCK_MATCH_RATIO


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def record_version(record: dict[str, Any]) -> str:
    return str(record.get("layout_version") or record.get("version") or "v1")


def record_page(record: dict[str, Any]) -> int:
    raw = record.get("page")
    if raw is None:
        raw = record.get("image_area")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def version_key(version: str) -> tuple[int, str]:
    """让 v2 排在 v10 前面，同时保留非数字后缀的稳定顺序。"""
    numbers = [int(part) for part in re.findall(r"\d+", str(version))]
    if numbers:
        return (numbers[0], str(version))
    return (0, str(version))


def region_version_matches(record_version_value: str, current_version: str) -> bool:
    return str(record_version_value or "") == str(current_version or "")


def _region_id(region: dict[str, Any], page: int, index: int) -> str:
    return str(region.get("region_id") or region.get("area_id") or f"p{page}-r{index + 1}")


def _ocr_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """取识别记录里的词块；旧数据没有词块时把整段文本当成一个块。"""
    raw = record.get("blocks")
    blocks: list[dict[str, Any]] = []
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("recognized_text") or "")
            if not text.strip():
                continue
            blocks.append({
                "text": text,
                "bbox": parse_bbox(item.get("bbox") or record.get("bbox")),
                "confidence": float(item.get("confidence", record.get("confidence", 0.0)) or 0.0),
            })
    elif str(record.get("recognized_text") or record.get("text") or "").strip():
        blocks.append({
            "text": str(record.get("recognized_text") or record.get("text")),
            "bbox": parse_bbox(record.get("bbox")),
            "confidence": float(record.get("confidence", 0.0) or 0.0),
        })
    return blocks


def _normalize_ocr(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for rec in records:
        bbox = parse_bbox(rec.get("bbox"))
        if bbox_area(bbox) <= 0:
            continue
        normalized.append({
            "region_id": str(rec.get("region_id") or rec.get("image_area_id") or ""),
            "page": record_page(rec),
            "bbox": bbox,
            "version": record_version(rec),
            "confidence": float(rec.get("confidence", 0.0) or 0.0),
            "engine": rec.get("engine"),
            "blocks": _ocr_blocks(rec),
        })
    return normalized


def _classify_region(
    bbox: list[float],
    no_text: bool,
    records: list[dict[str, Any]],
    version: str,
) -> dict[str, Any]:
    """判定单个区域状态；返回状态、原因、采用的识别记录和覆盖率。

    两个比例必须分开看：
    * ``attribution`` = 识别框有多大比例落在本区域内（决定这条记录归不归本区域）；
    * ``coverage``    = 本区域有多大比例被识别框盖住（决定这块区域是否已有文字）。
    只看前者会把“小块旧识别完全落在新版大区域里”误判成整块已识别。
    """
    candidates = []
    for rec in records:
        attribution = overlap_ratio(rec["bbox"], bbox)
        if attribution <= 0:
            continue
        covered = bbox_area(intersection(rec["bbox"], bbox)) / max(bbox_area(bbox), 1e-9)
        candidates.append({"record": rec, "ratio": covered, "attribution": attribution})

    usable = [
        c for c in candidates
        if c["attribution"] >= FULL_CONTAIN_RATIO and c["ratio"] >= FULL_CONTAIN_RATIO
    ]
    partial = [
        c for c in candidates
        if c["attribution"] >= MIN_ATTRIBUTION_RATIO and MIN_ATTRIBUTION_RATIO <= c["ratio"] < FULL_CONTAIN_RATIO
    ]

    if no_text and not usable and not partial:
        return {
            "state": STATE_NO_TEXT,
            "reason": "区域已明确标注无文字，不需要补识别",
            "records": [],
            "coverage_ratio": 0.0,
        }

    low_confidence = [c for c in usable if c["record"]["confidence"] < MIN_CONFIDENCE]
    reliable = [c for c in usable if c["record"]["confidence"] >= MIN_CONFIDENCE]
    if reliable:
        # 同版本识别直接可用；旧版本识别若完整落在新区域内，也可继续使用。
        chosen = max(reliable, key=lambda c: c["record"]["confidence"])
        record = chosen["record"]
        reused = not region_version_matches(record["version"], version)
        reason = (
            f"旧版 {record['version']} 识别完整覆盖本区域，可继续使用"
            if reused
            else "本版识别完整覆盖区域"
        )
        return {
            "state": STATE_RECOGNIZED,
            "reason": reason,
            "records": [c["record"] for c in reliable],
            "coverage_ratio": round(max(c["ratio"] for c in reliable), 3),
            "source_version": record["version"],
            "reused": reused,
        }

    if low_confidence:
        return {
            "state": STATE_UNCONFIRMED,
            "reason": "识别置信度低于阈值，需要人工确认",
            "records": [c["record"] for c in low_confidence],
            "coverage_ratio": round(max(c["ratio"] for c in low_confidence), 3),
        }

    version_conflicts = [
        c for c in partial
        if not region_version_matches(c["record"]["version"], version)
    ]
    if version_conflicts:
        return {
            "state": STATE_UNCONFIRMED,
            "reason": "旧版识别与当前图块只部分对齐，归属需要确认",
            "records": [c["record"] for c in version_conflicts],
            "coverage_ratio": round(max(c["ratio"] for c in version_conflicts), 3),
        }

    partial_records = [c for c in candidates if c["attribution"] >= MIN_ATTRIBUTION_RATIO]
    if partial_records:
        best = max(partial_records, key=lambda c: c["ratio"])
        same_version = region_version_matches(best["record"]["version"], version)
        reason = (
            "本版识别只覆盖了本区域的一部分，仍需补识别"
            if same_version
            else f"旧版 {best['record']['version']} 识别只覆盖了本区域的一部分，仍需补识别"
        )
        coverage_ratio = round(best["ratio"], 3)
    else:
        reason = "当前版式下尚无归属可信的识别结果，需要补识别"
        coverage_ratio = round(max((c["ratio"] for c in candidates), default=0.0), 3)
    return {
        "state": STATE_PENDING,
        "reason": reason,
        "records": [],
        "coverage_ratio": coverage_ratio,
    }


def _sort_key(region: dict[str, Any]) -> tuple[float, float, str]:
    bbox = region["bbox"]
    return (round(bbox[1], 3), round(bbox[0], 3), region["region_id"])


def build_page_view(
    page: int,
    regions: list[dict[str, Any]],
    ocr_records: list[dict[str, Any]],
    version: str,
) -> dict[str, Any]:
    """构建一页的识别区域覆盖视图与阅读顺序。"""
    normalized_regions = []
    for index, region in enumerate(regions):
        bbox = parse_bbox(region.get("bbox"))
        kind = str(region.get("kind") or region.get("type") or "image")
        normalized_regions.append({
            "region_id": _region_id(region, page, index),
            "page": page,
            "bbox": bbox,
            "kind": kind,
            "language": region.get("language", ""),
            "source": region.get("source", "layout"),
            "no_text": (
                bool(region.get("no_text"))
                or kind in NO_TEXT_KINDS
                or region.get("has_text") is False
                or str(region.get("text_kind", "")).lower() in NO_TEXT_KINDS
            ),
        })
    normalized_regions.sort(key=_sort_key)
    normalized_ocr = _normalize_ocr(ocr_records)

    used_blocks: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    view_regions: list[dict[str, Any]] = []
    reading_order: list[dict[str, Any]] = []
    pending_regions: list[dict[str, Any]] = []
    seq = 0

    for region in normalized_regions:
        decision = _classify_region(
            region["bbox"], region["no_text"], normalized_ocr, version)
        region_out = {
            "region_id": region["region_id"],
            "page": page,
            "bbox": region["bbox"],
            "kind": region["kind"],
            "language": region["language"],
            "state": decision["state"],
            "state_key": STATE_KEYS[decision["state"]],
            "reason": decision["reason"],
            "coverage_ratio": decision["coverage_ratio"],
            "source_version": decision.get("source_version", ""),
            "reused": bool(decision.get("reused")),
            "recognized_text": "",
            "blocks": [],
        }

        if decision["state"] == STATE_RECOGNIZED:
            region_blocks: list[dict[str, Any]] = []
            for record in decision["records"]:
                for block in record["blocks"]:
                    # 词块必须落在本区域内才归属本区域，避免把邻区文字算进来
                    if overlap_ratio(block["bbox"], region["bbox"]) < BLOCK_MATCH_RATIO:
                        continue
                    duplicate_of = None
                    for used in used_blocks:
                        if normalize_text(used["text"]) == normalize_text(block["text"]) and same_block(
                            used["bbox"], block["bbox"]
                        ):
                            duplicate_of = used
                            break
                    block_out = {
                        "text": block["text"],
                        "bbox": block["bbox"],
                        "confidence": block["confidence"],
                    }
                    if duplicate_of is not None:
                        duplicates.append({
                            "text": block["text"],
                            "bbox": block["bbox"],
                            "region_id": region["region_id"],
                            "kept_in_region_id": duplicate_of["region_id"],
                            "reason": "与先前区域交叠处的同名词块只计一次",
                        })
                        block_out["duplicate"] = True
                        block_out["duplicate_of_region_id"] = duplicate_of["region_id"]
                        region_blocks.append(block_out)
                        continue
                    seq += 1
                    kept = {
                        "seq": seq,
                        "region_id": region["region_id"],
                        "page": page,
                        "text": block["text"],
                        "bbox": block["bbox"],
                        "confidence": block["confidence"],
                        "reused": bool(decision.get("reused")),
                    }
                    used_blocks.append(kept)
                    reading_order.append(kept)
                    block_out["seq"] = seq
                    region_blocks.append(block_out)

            region_out["blocks"] = region_blocks
            region_out["recognized_text"] = "".join(b["text"] for b in region_blocks if not b.get("duplicate"))

        if decision["state"] in (STATE_PENDING, STATE_UNCONFIRMED):
            pending_regions.append({
                "region_id": region["region_id"],
                "bbox": region["bbox"],
                "state": decision["state"],
                "state_key": STATE_KEYS[decision["state"]],
                "reason": decision["reason"],
            })
        view_regions.append(region_out)

    overlap_pairs = []
    for i, first in enumerate(normalized_regions):
        for second in normalized_regions[i + 1:]:
            box = intersection(first["bbox"], second["bbox"])
            if bbox_area(box) > 0:
                overlap_pairs.append({
                    "a": first["region_id"],
                    "b": second["region_id"],
                    "bbox": box,
                })

    state_counts = {key: 0 for key in STATE_KEYS.values()}
    for region_out in view_regions:
        state_counts[region_out["state_key"]] += 1

    return {
        "page": page,
        "version": version,
        "regions": view_regions,
        "reading_order": reading_order,
        "duplicates": duplicates,
        "pending_regions": pending_regions,
        "overlap_pairs": overlap_pairs,
        "state_counts": state_counts,
        "confirmed_text": "".join(item["text"] for item in reading_order),
    }


def build_view(
    doc_id: str,
    regions: list[dict[str, Any]],
    ocr_records: list[dict[str, Any]],
    version: str,
    note: str = "",
) -> dict[str, Any]:
    """按页构建整篇文档的覆盖视图。"""
    pages = sorted({int(r.get("page", 1) or 1) for r in regions} |
                   {p for p in (record_page(r) for r in ocr_records) if p > 0})
    if not pages:
        pages = [1]

    page_views = []
    for page in pages:
        page_regions = [r for r in regions if int(r.get("page", 1) or 1) == page]
        # 页码缺失的旧识别记录无法判断归属，保守地交给每一页参与校验
        page_ocr = [r for r in ocr_records if record_page(r) in (0, page)]
        page_views.append(build_page_view(page, page_regions, page_ocr, version))

    summary = {
        "pages": len(page_views),
        "regions": sum(len(p["regions"]) for p in page_views),
        "recognized": 0,
        "pending": 0,
        "unconfirmed": 0,
        "no_text": 0,
        "reading_blocks": sum(len(p["reading_order"]) for p in page_views),
        "duplicate_blocks_removed": sum(len(p["duplicates"]) for p in page_views),
        "overlap_pairs": sum(len(p["overlap_pairs"]) for p in page_views),
        "pending_regions": sum(len(p["pending_regions"]) for p in page_views),
    }
    for page in page_views:
        for key in ("recognized", "pending", "unconfirmed", "no_text"):
            summary[key] += page["state_counts"][key]

    return {
        "doc_id": doc_id,
        "version": version,
        "note": note,
        "summary": summary,
        "pages": page_views,
    }
