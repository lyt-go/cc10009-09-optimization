"""抓取作业业务规则：状态流转、字段校验与筛选口径都收在这里。

作业带「作业版本」：同一次执行内的失败URL回写只在当前版本内去重；作业重新
启动时版本号递增，上一版本的重试结果仍然挂在上一版本的作业执行进度上。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.urlnorm import normalize_url
from app.store import store

MODULE = "crawljob"
REQUIRED_FIELDS = ["作业编号", "目标站点", "起始URL"]
STATUS_ORDER = ["草稿", "作业审批中", "执行中", "已完成", "已停止"]
ACTION_RULES = {
    "提交作业审批": "作业审批中",
    "启动作业": "执行中",
    "完成作业": "已完成",
    "停止作业": "已停止",
}
NEGATIVE_ACTIONS: list[str] = []


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _version(value: Any) -> int:
    try:
        return max(int(str(value).strip()), 1)
    except (TypeError, ValueError):
        return 1


class CrawljobService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self._ensure_fields(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._ensure_fields(entry)
        return entry

    def find_by_no(self, job_no: object) -> dict[str, Any] | None:
        text = str(job_no or "").strip()
        if not text:
            return None
        for row in store.rows(MODULE):
            if str(row.get("作业编号")) == text:
                self._ensure_fields(row)
                return row
        try:
            return self.get_entry(int(text))
        except (TypeError, ValueError):
            return None

    def _ensure_fields(self, row: dict[str, Any]) -> None:
        row.setdefault("作业版本", 1)
        row["作业版本"] = _version(row.get("作业版本"))
        if row.get("起始URL"):
            row.setdefault("规范化起始URL", normalize_url(row.get("起始URL"), row.get("URL去重策略")))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        job_no = str(values.get("作业编号")).strip()
        if self.find_by_no(job_no) is not None:
            return None, []
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.setdefault("URL去重策略", values.get("URL去重策略") or "默认去重")
        entry.setdefault("页面解析器", values.get("页面解析器") or "默认解析器")
        entry["作业版本"] = _version(values.get("作业版本") or 1)
        entry["规范化起始URL"] = normalize_url(entry.get("起始URL"), entry.get("URL去重策略"))
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"作业定义 {entry_id} 不存在或已归档"
            self._ensure_fields(entry)
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于抓取作业可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            current = str(entry.get("status") or "")
            if action == "启动作业" and current != "执行中":
                # 每次启动都开启新版本：失败URL只在「作业版本」内去重，上一版本的
                # 重试条目与进度留在上一版本，不会串到新版本。
                entry["作业版本"] = _version(entry.get("作业版本")) + 1
                entry["最近启动时间"] = _now()
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if action == "停止作业":
                entry["最近停止时间"] = _now()
            elif action == "完成作业":
                entry["最近完成时间"] = _now()
            # 作业状态变化后重建任务执行侧进度，保证两侧口径一致。
            from app.services.taskexec import taskexec_service

            taskexec_service.refresh_all()
            if action in {"完成作业", "停止作业"}:
                message = self._finish_message(entry, action)
                return entry, message
            return entry, f"作业定义已{action}"

    def _finish_message(self, entry: dict[str, Any], action: str) -> str:
        from app.services.taskexec import taskexec_service

        progress = taskexec_service.find_progress(entry.get("作业编号"), entry.get("作业版本"))
        if progress is None:
            return f"作业定义已{action}"
        unconverged = int(progress.get("未收敛URL数") or 0)
        if unconverged:
            return (
                f"作业定义已{action}，但仍有 {unconverged} 个URL未收敛；"
                "这些条目的重试结果此后只归档，不会把作业带回执行中"
            )
        return f"作业定义已{action}，作业执行进度已收敛"
