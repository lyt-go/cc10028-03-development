"""识别区域覆盖图接口：按页返回覆盖视图，并编排旧布局→改版→补识别的观察点。

路径固定在 ``/api/layout-coverage`` 前缀下，注册顺序排在通用资源路由之前，
避免 ``/api/{module}/{entry_id}`` 把文档编号当成整数解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services.coverage import DEMO_DOC_ID, STAGES, CoverageService

router = APIRouter(prefix="/api/layout-coverage", tags=["识别区域覆盖"])


def _service() -> CoverageService:
    return CoverageService()


@router.get("/stages")
def stage_catalog() -> dict[str, Any]:
    """返回三个观察点的顺序与名称。"""
    return {"stages": [{"key": key, "label": label} for key, label in STAGES.items()]}


@router.get("/demo")
def demo_document() -> dict[str, Any]:
    """演示文档索引；尚未初始化时自动生成一份。"""
    service = _service()
    document = service.demo_document(DEMO_DOC_ID)
    if document is None:
        document = service.seed_demo()
    return document


@router.post("/demo", response_model=ActionResult)
def reset_demo() -> ActionResult:
    """重新生成演示文档，便于反复核对三个阶段的差异。"""
    index = _service().seed_demo()
    return ActionResult(ok=True, message="识别覆盖演示文档已重建", entry=index)


@router.post("/demo/revised")
def reset_demo_to_revised() -> dict[str, Any]:
    """把演示文档退回改版后状态，便于重新演示补识别与布局重排。"""
    return _service().reset_demo_to_revised()


@router.get("/{doc_id}/stages")
def document_stages(doc_id: str) -> dict[str, Any]:
    """依次给出旧布局、改版后、补识别后三个阶段的覆盖视图。"""
    service = _service()
    payload = service.build_all_stages(doc_id)
    if not payload["stages"][-1]["view"].get("pages"):
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 没有可用的图片区域")
    return payload


@router.get("/{doc_id}")
def coverage_view(
    doc_id: str,
    stage: str = Query(default="supplemented"),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """读取某一阶段的覆盖视图；``stage=raw`` 时按指定版本直接计算。"""
    service = _service()
    if stage not in STAGES and stage != "raw":
        raise HTTPException(status_code=400, detail=f"未知阶段：{stage}")
    view = service.build_stage(doc_id, stage) if stage != "raw" else service.build(
        doc_id, version, persist=False)
    if not view.get("pages"):
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 没有可用的图片区域")
    return view


@router.post("/{doc_id}/supplement")
def supplement(doc_id: str, version: str | None = Query(default=None)) -> dict[str, Any]:
    """对待识别/待确认区域补做识别，并返回补识别后的覆盖视图。"""
    service = _service()
    if not service.available_versions(doc_id) or not service.load_regions(doc_id, service.latest_version(doc_id)):
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 没有可用的图片区域")
    return service.supplementary_recognize(doc_id, version)


@router.post("/{doc_id}/reorganize")
def reorganize(doc_id: str, version: str | None = Query(default=None)) -> dict[str, Any]:
    """布局重排：可确认文本进入阅读顺序，待识别坐标保留到版式页。"""
    service = _service()
    if not service.available_versions(doc_id) or not service.load_regions(doc_id, service.latest_version(doc_id)):
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 没有可用的图片区域")
    result = service.reorganize(doc_id, version)
    if not result.get("pages"):
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 没有可用的版式页")
    return result
