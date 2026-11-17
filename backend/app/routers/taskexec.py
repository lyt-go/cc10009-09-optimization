"""任务执行侧接口：重试结果回写、作业执行进度、结果索引清单与两类数据集。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, PageResult, RetryResultPayload
from app.services.taskexec import taskexec_service

router = APIRouter(prefix="/api/taskexec", tags=["任务执行侧"])

STATES = ["失败待重试", "重试中", "已完成", "最终失败", "迟到归档"]


def _text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


@router.get("/progress", response_model=PageResult[dict])
def list_progress(
    job: str | None = Query(default=None, description="按作业编号筛选"),
    version: str | None = Query(default=None, description="按作业版本筛选"),
    status: str | None = Query(default=None, description="执行中、已完成、已停止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """作业执行进度列表：同一作业版本只会有一条进度。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = taskexec_service.list_progress(
        job_no=_text(job), version=_text(version), status=_text(status), page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/progress/{entry_id}", response_model=dict)
def get_progress(entry_id: int) -> dict:
    entry = taskexec_service.get_progress(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"作业执行进度 {entry_id} 不存在")
    return entry


@router.get("/progress/by-code/{code}", response_model=dict)
def get_progress_by_code(code: str) -> dict:
    entry = taskexec_service.get_progress_by_code(code)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"作业执行进度 {code} 不存在")
    return entry


@router.get("/manifest", response_model=PageResult[dict])
def list_manifest(
    job: str | None = Query(default=None),
    version: str | None = Query(default=None),
    state: str | None = Query(default=None, description="失败待重试、重试中、已完成、最终失败、迟到归档"),
    keyword: str | None = Query(default=None, description="按索引编号、失败URL、规范化URL检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """结果索引清单：重试队列的每条URL回写都落在这里，可从清单回查进度。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = taskexec_service.list_manifest(
        job_no=_text(job), version=_text(version), state=_text(state),
        keyword=_text(keyword), page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results", response_model=PageResult[dict])
def list_results(
    job: str | None = Query(default=None),
    version: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """重试结果明细列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = taskexec_service.list_results(
        job_no=_text(job), version=_text(version), keyword=_text(keyword), page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/results", response_model=ActionResult)
def write_retry_result(payload: RetryResultPayload) -> ActionResult:
    """接收重试结果并回写原作业进度；作业结束后晚到的结果只归档。

    成功与失败都走同一个入口：成功把条目移出重试队列，失败按退避次数决定
    留在队列还是进入最终失败清单。重复提交用「幂等键」去重。
    """
    result, message, extra = taskexec_service.write_retry_result(payload.values())
    if result is None:
        return ActionResult(ok=False, message=message)
    entry: dict[str, Any] = {"重试结果": result}
    entry.update({key: value for key, value in extra.items() if key != "重试队列条目"})
    if extra.get("重试队列条目") is not None:
        entry["重试队列条目"] = extra["重试队列条目"]
    return ActionResult(ok=not extra.get("幂等"), message=message, entry=entry)


@router.get("/audit")
def audit(
    job: str = Query(description="作业编号"),
    version: str = Query(default="1", description="作业版本"),
) -> dict[str, Any]:
    """按同一URL集合核对进度计数、索引清单与重试结果，可逐项复核。"""
    return taskexec_service.audit(job, version)


@router.get("/reconcile")
def reconcile(
    job: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """两侧计数核对：进度、索引清单、重试队列是否一致。"""
    return taskexec_service.reconcile(job_no=_text(job), version=_text(version))


@router.get("/dataset/progress")
def progress_dataset(job: str | None = Query(default=None)) -> dict[str, Any]:
    """作业执行进度数据集：每行带唯一URL集合与逐URL明细。"""
    return taskexec_service.progress_dataset(_text(job))


@router.get("/dataset/retry-results")
def retry_result_dataset(job: str | None = Query(default=None)) -> dict[str, Any]:
    """重试结果数据集：与作业执行进度数据集共用同一URL集合。"""
    return taskexec_service.retry_result_dataset(_text(job))
