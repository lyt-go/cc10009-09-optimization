"""重试队列接口：登记去重、发起重试、最终失败处置与结果核对。

- `POST /api/retryqueue` 单条登记，同一作业版本的唯一URL只会更新已有条目；
- `POST /api/retryqueue/failures` 批量读取失败页面，按唯一URL合并；
- `POST /api/retryqueue/reconnect-sync` 网络恢复后的补同步，按幂等键重放；
- `POST /api/retryqueue/{id}/actions` 队列动作，同时回写任务执行侧清单。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, FailureBatchPayload, PageResult, action_of, values_of
from app.services.retryqueue import RetryqueueService

router = APIRouter(prefix="/api/retryqueue", tags=["重试队列"])

service = RetryqueueService()

LIST_FIELDS = ["条目编号", "失败URL", "规范化URL", "作业编号", "作业版本", "失败原因码",
               "首次失败时间", "已重试次数", "下次重试时间", "退避级别", "最终处置"]
STATUSES = ["草稿", "等待重试", "重试中", "重试成功", "最终失败"]


def _as_list_query(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按条目编号、失败URL、规范化URL检索"),
    status: str | None = Query(default=None, description="草稿、等待重试、重试中、重试成功、最终失败"),
    reason: str | None = Query(default=None, description="按失败原因码精确检索，例如 TIMEOUT"),
    job: str | None = Query(default=None, description="按作业编号筛选"),
    version: str | None = Query(default=None, description="按作业版本筛选"),
    url: str | None = Query(default=None, description="按失败URL定位，会先规范化再匹配"),
    scope: str | None = Query(default=None, description="unconverged 只看未收敛，converged 只看已收敛"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号、状态、原因码、作业版本和URL过滤重试队列；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=_as_list_query(status),
        reason=_as_list_query(reason),
        job_no=_as_list_query(job),
        version=_as_list_query(version),
        url=_as_list_query(url),
        scope=_as_list_query(scope),
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/reasons")
def reason_catalog(job: str | None = Query(default=None, description="只统计某个作业的原因码")) -> dict[str, Any]:
    """原因码目录：每个原因码对应的条目数、条目编号与具体重试记录定位。"""
    summary = service.reason_summary(job_no=_as_list_query(job))
    return {"total": len(summary), "items": summary}


@router.get("/reconcile")
def reconcile(
    job: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """两侧核对：重试队列、结果索引清单、作业执行进度是否落在同一URL集合上。"""
    return service.reconcile(
        job_no=_as_list_query(job), version=_as_list_query(version)
    )


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出重试队列清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "retryqueue", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重试条目明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重试条目 {entry_id} 不存在或已归档")
    return entry


@router.get("/lookup/{entry_code}", response_model=dict)
def lookup_entry(entry_code: str) -> dict:
    """按条目编号或去重键定位具体重试记录。"""
    entry = service.get_entry_by_code(entry_code)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重试条目 {entry_code} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重试条目；同一作业版本的同一规范化URL只更新已有条目。"""
    entry, missing, message, idempotent = service.create_entry(values_of(payload))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=not idempotent, message=message, entry=entry)


@router.post("/failures", response_model=ActionResult)
def register_failures(payload: FailureBatchPayload) -> ActionResult:
    """读取失败页面并按唯一URL合并重试条目，生成可供逐项复核的登记结果。"""
    values = payload.model_dump(exclude_none=True)
    entries, skipped, message, idempotent = service.register_failures(values)
    if not entries:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=not idempotent,
        message=message,
        entry={"items": entries, "skipped": skipped, "idempotent": idempotent},
    )


@router.post("/reconnect-sync", response_model=ActionResult)
def reconnect_sync(payload: FailureBatchPayload) -> ActionResult:
    """网络中断恢复后的补同步：同一幂等键只生效一次，不重复生成条目。"""
    values = payload.model_dump(exclude_none=True)
    values.setdefault("注册来源", "补同步")
    entries, skipped, message, idempotent = service.register_failures(values)
    if not entries:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=not idempotent,
        message=message,
        entry={"items": entries, "skipped": skipped, "idempotent": idempotent},
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """加入队列、发起重试、标记最终失败；不允许的动作会被拦下并说明原因。"""
    action = action_of(payload)
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
