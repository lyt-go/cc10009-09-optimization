"""重试队列业务规则：登记去重、状态流转、重试结果回写与筛选口径。

重试队列只保留「未收敛」的失败URL：同一作业版本内，一个规范化URL只有一条
重试条目；重试成功或达到退避上限后条目归档，不再留在队列里。队列的每一次
状态变化都会同步到任务执行侧清单，避免两边各说各话。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.urlnorm import (
    merge_reasons,
    normalize_url,
    primary_reason,
    reason_priority,
    same_site,
)
from app.store import store

MODULE = "retryqueue"
BATCH_MODULE = "retryqueue_batch"
REQUIRED_FIELDS = ["作业编号", "失败URL", "失败原因码"]
STATUS_ORDER = ["草稿", "等待重试", "重试中", "重试成功", "最终失败"]
ACTION_RULES = {"加入队列": "等待重试", "发起重试": "重试中", "标记最终失败": "最终失败"}
NEGATIVE_ACTIONS: list[str] = []
MAX_RETRY_ATTEMPTS = 5
IN_QUEUE_STATUSES = {"草稿", "等待重试", "重试中"}
CONVERGED_STATUSES = {"重试成功", "最终失败"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _version_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "1"


def _attempt(value: Any, default: int = 0) -> int:
    try:
        return max(int(str(value).strip()), 0)
    except (TypeError, ValueError):
        return default


def entry_code(entry_id: int) -> str:
    return f"RETR-{entry_id:04d}"


def dedup_key(job_no: object, version: object, norm_url: object) -> str:
    return f"{str(job_no or '').strip()}|{_version_text(version)}|{norm_url}"


def _find_job(job_no: object) -> dict[str, Any] | None:
    text = str(job_no or "").strip()
    if not text:
        return None
    for row in store.rows("crawljob"):
        if str(row.get("作业编号")) == text:
            return row
    try:
        return store.find("crawljob", int(text))
    except (TypeError, ValueError):
        return None


class RetryqueueService:
    # ------------------------------------------------------------------ 规范化与迁移
    def prepare_entries(self) -> None:
        """补齐条目字段，保证同一作业版本内唯一URL只留下一条有效条目。

        历史数据里可能已经出现重复条目（例如并发登记或补同步重放）。这里按
        「作业编号 + 作业版本 + 规范化URL」合并：保留最早一条，其余标记为已合并
        并移出队列，进度与索引清单都只认保留的那条。
        """
        with store.lock:
            seen: dict[str, dict[str, Any]] = {}
            for row in store.rows(MODULE):
                job_no = str(row.get("作业编号") or "").strip()
                if not job_no or not str(row.get("失败URL") or "").strip():
                    continue
                job = _find_job(job_no) or {}
                version = _version_text(row.get("作业版本") or job.get("作业版本"))
                strategy = row.get("URL去重策略") or job.get("URL去重策略")
                norm_url = str(row.get("规范化URL") or "").strip() or normalize_url(row.get("失败URL"), strategy)
                if not norm_url:
                    continue
                row["作业编号"] = job_no
                row["作业版本"] = version
                row["规范化URL"] = norm_url
                row["去重键"] = dedup_key(job_no, version, norm_url)
                row["URL去重策略"] = strategy or ""
                row["起始URL"] = row.get("起始URL") or job.get("起始URL") or ""
                row["页面解析器"] = row.get("页面解析器") or job.get("页面解析器") or ""
                row["失败原因码列表"] = merge_reasons(
                    row.get("失败原因码列表"), row.get("失败原因码")
                )
                row["失败原因码"] = primary_reason(row.get("失败原因码列表"))
                row["已重试次数"] = _attempt(row.get("已重试次数"), 0)
                row["退避级别"] = _attempt(row.get("退避级别"), row["已重试次数"])
                if not str(row.get("条目编号") or "").strip():
                    row["条目编号"] = entry_code(int(row.get("id", 0)))
                if row.get("收敛状态") != "重复已合并":
                    row["收敛状态"] = self._converged_state(row)
                    row["in_queue"] = row.get("status") in IN_QUEUE_STATUSES
                row.setdefault("注册来源", "失败页面")

                key = row["去重键"]
                if key in seen:
                    self._merge_duplicate(seen[key], row)
                    continue
                seen[key] = row

    def _converged_state(self, row: dict[str, Any]) -> str:
        status = str(row.get("status") or "")
        if status == "重试成功":
            return "已收敛"
        if status == "最终失败":
            return "已终局"
        return "未收敛"

    def _merge_duplicate(self, target: dict[str, Any], duplicate: dict[str, Any]) -> None:
        """把重复条目并入最早一条：原因码按优先级合并，重试次数保留最大值。

        合并后的重复条目移出队列，不再参与进度计数；重试成功状态优先保留，
        避免重复条目把已收敛的URL重新拉回队列。
        """
        duplicate["in_queue"] = False
        duplicate["收敛状态"] = "重复已合并"
        duplicate["最终处置"] = f"重复条目已合并到 {target.get('条目编号')}"
        duplicate["合并至"] = target.get("条目编号")
        if target.get("status") == "重试成功":
            duplicate["status"] = "重试成功"
        target["失败原因码列表"] = merge_reasons(
            target.get("失败原因码列表"), duplicate.get("失败原因码列表")
        )
        target["失败原因码"] = primary_reason(target.get("失败原因码列表"))
        target["已重试次数"] = max(
            _attempt(target.get("已重试次数"), 0), _attempt(duplicate.get("已重试次数"), 0)
        )
        target["重复登记次数"] = _attempt(target.get("重复登记次数"), 0) + 1

    # ------------------------------------------------------------------ 查询
    def _matches_reason(self, row: dict[str, Any], reason: str) -> bool:
        wanted = reason.strip().upper()
        if not wanted:
            return True
        codes = merge_reasons(row.get("失败原因码列表"), row.get("失败原因码"))
        return any(code.upper() == wanted for code in codes)

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        job_no: str | None = None,
        version: str | None = None,
        url: str | None = None,
        scope: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.prepare_entries()
        rows = list(store.rows(MODULE))
        if keyword:
            needle = keyword.strip().lower()
            rows = [
                row for row in rows
                if needle in str(row.get("条目编号", "")).lower()
                or needle in str(row.get("失败URL", "")).lower()
                or needle in str(row.get("规范化URL", "")).lower()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if reason:
            rows = [row for row in rows if self._matches_reason(row, reason)]
        if job_no:
            rows = [row for row in rows if job_no.strip() == str(row.get("作业编号", ""))]
        if version:
            rows = [row for row in rows if _version_text(row.get("作业版本")) == _version_text(version)]
        if url:
            norm = normalize_url(url, None)
            rows = [
                row for row in rows
                if row.get("规范化URL") == norm or row.get("失败URL") == url
            ]
        if scope == "unconverged":
            rows = [row for row in rows if row.get("in_queue") and row.get("收敛状态") == "未收敛"]
        elif scope == "converged":
            rows = [row for row in rows if row.get("收敛状态") in {"已收敛", "已终局"}]
        elif scope == "final_failed":
            # 最终失败清单：只包含达到退避上限的URL（回写与人工动作都做了次数校验）。
            rows = [row for row in rows if row.get("status") == "最终失败"]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def queue_snapshot(self, *, job_no: str | None = None, version: str | None = None) -> list[dict[str, Any]]:
        """当前重试队列快照：只包含未收敛条目。"""
        rows, _ = self.list_entries(
            job_no=job_no, version=version, scope="unconverged", page=1, size=10000
        )
        return rows

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self.prepare_entries()
        return store.find(MODULE, entry_id)

    def get_entry_by_code(self, code: str) -> dict[str, Any] | None:
        self.prepare_entries()
        text = str(code or "").strip()
        for row in store.rows(MODULE):
            if str(row.get("条目编号")) == text or row.get("去重键") == text:
                return row
        return None

    def reason_summary(self, *, job_no: str | None = None) -> list[dict[str, Any]]:
        self.prepare_entries()
        rows = store.rows(MODULE)
        if job_no:
            rows = [row for row in rows if str(row.get("作业编号")) == job_no]
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for code in merge_reasons(row.get("失败原因码列表"), row.get("失败原因码")):
                buckets.setdefault(code, []).append(row)
        summary = [
            {
                "失败原因码": code,
                "优先级": reason_priority(code),
                "条目数": len(items),
                "未收敛数": sum(1 for row in items if row.get("in_queue")),
                "条目编号": [row.get("条目编号") for row in items],
                "定位": [
                    {"条目编号": row.get("条目编号"), "作业编号": row.get("作业编号"),
                     "作业版本": row.get("作业版本"), "规范化URL": row.get("规范化URL")}
                    for row in items
                ],
            }
            for code, items in buckets.items()
        ]
        summary.sort(key=lambda item: (-int(item["优先级"]), item["失败原因码"]))
        return summary

    # ------------------------------------------------------------------ 登记
    def _validate_job_url(self, job: dict[str, Any], raw_url: str) -> str:
        start_url = str(job.get("起始URL") or "")
        if start_url and not same_site(raw_url, start_url):
            return (
                f"失败URL {raw_url} 不属于作业 {job.get('作业编号')} 起始URL "
                f"{start_url} 所在站点，不能登记到该作业"
            )
        return ""

    def _validate_open_job(self, job: dict[str, Any]) -> str:
        """作业结束后不再接收新的失败URL；已存在的条目只允许合并原因码。"""
        status = str(job.get("status") or "")
        if status in {"已完成", "已停止"}:
            return (
                f"作业 {job.get('作业编号')} 已结束（{status}），新的失败页面不再进入重试队列"
            )
        return ""

    def _resolve_entry(
        self,
        *,
        job: dict[str, Any],
        version: str,
        raw_url: str,
        reason: str,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        job_no = str(job.get("作业编号"))
        strategy = job.get("URL去重策略")
        norm_url = normalize_url(values.get("规范化URL") or raw_url, strategy)
        if not norm_url:
            return None, f"失败URL「{raw_url}」无法规范化，已跳过"
        key = dedup_key(job_no, version, norm_url)
        for row in store.rows(MODULE):
            if row.get("去重键") == key:
                if row.get("in_queue"):
                    row["失败原因码列表"] = merge_reasons(row.get("失败原因码列表"), reason)
                    row["失败原因码"] = primary_reason(row.get("失败原因码列表"))
                    row["重复登记次数"] = _attempt(row.get("重复登记次数"), 0) + 1
                    row["最近更新时间"] = _now()
                    self._mark_status(row, str(row.get("status") or "等待重试"))
                    return row, "同一作业版本内已存在该URL的重试条目，已合并原因码"
                # 已收敛的条目保留，不因补同步重新入队，也不重复生成新URL。
                row["失败原因码列表"] = merge_reasons(row.get("失败原因码列表"), reason)
                row["失败原因码"] = primary_reason(row.get("失败原因码列表"))
                return row, "该URL已收敛，补同步未重复生成条目"

        new_id = store.next_id(MODULE)
        row = {
            "id": new_id,
            "条目编号": str(values.get("条目编号") or entry_code(new_id)),
            "作业编号": job_no,
            "作业版本": version,
            "起始URL": job.get("起始URL") or "",
            "页面解析器": job.get("页面解析器") or "",
            "URL去重策略": strategy or "",
            "去重键": key,
            "失败URL": raw_url,
            "规范化URL": norm_url,
            "失败原因码": "",
            "失败原因码列表": [],
            "首次失败时间": str(values.get("首次失败时间") or _now()),
            "已重试次数": _attempt(values.get("已重试次数"), 0),
            "下次重试时间": str(values.get("下次重试时间") or ""),
            "退避级别": _attempt(values.get("退避级别"), 0),
            "最终处置": "",
            "收敛状态": "未收敛",
            "in_queue": True,
            "注册来源": str(values.get("注册来源") or "失败页面"),
            "最近更新时间": _now(),
        }
        row["失败原因码列表"] = merge_reasons(row.get("失败原因码列表"), reason)
        row["失败原因码"] = primary_reason(row.get("失败原因码列表"))
        store.rows(MODULE).append(row)
        self._mark_status(row, "等待重试")
        return row, "重试条目已登记"

    def _mark_status(self, row: dict[str, Any], status: str) -> None:
        row["status"] = status
        row["pending"] = status not in CONVERGED_STATUSES
        row["abnormal"] = status == "最终失败"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str, bool]:
        """登记一条重试条目；同一作业版本的唯一URL只保留一条。"""
        idem = str(values.get("幂等键") or "").strip()
        with store.lock:
            self.prepare_entries()
            if idem:
                for row in store.rows(MODULE):
                    if row.get("幂等键") == idem:
                        return row, [], "重复提交已忽略（幂等键命中）", True
            missing = [
                field for field in REQUIRED_FIELDS
                if not str(values.get(field) or "").strip()
            ]
            if missing:
                return None, missing, "", False
            job = _find_job(values.get("作业编号"))
            if job is None:
                return None, [], f"作业 {values.get('作业编号')} 不存在，失败URL不能脱离作业登记", False
            raw_url = str(values.get("失败URL") or "").strip()
            message = self._validate_job_url(job, raw_url)
            if message:
                return None, [], message, False
            version = _version_text(values.get("作业版本") or job.get("作业版本"))
            norm_url = normalize_url(raw_url, job.get("URL去重策略"))
            existing = next(
                (row for row in store.rows(MODULE)
                 if row.get("去重键") == dedup_key(job.get("作业编号"), version, norm_url)),
                None,
            )
            if existing is None:
                closed = self._validate_open_job(job)
                if closed:
                    return None, [], closed, False
            entry, message = self._resolve_entry(
                job=job,
                version=version,
                raw_url=raw_url,
                reason=str(values.get("失败原因码") or "").strip(),
                values=values,
            )
            if entry is None:
                return None, [], message, False
            if idem:
                entry["幂等键"] = idem
            self._sync_taskexec(entry, "加入队列")
            return entry, [], message, message.startswith("重复提交")

    def register_failures(self, values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str, bool]:
        """批量登记失败页面：读取失败页面、合并同URL条目、按唯一URL更新结果。"""
        job = _find_job(values.get("作业编号") or values.get("job"))
        if job is None:
            return [], [], f"作业 {values.get('作业编号') or ''} 不存在，失败页面无法回写", False
        failures = values.get("失败页面列表") or values.get("failures") or []
        if not isinstance(failures, list) or not failures:
            return [], [], "失败页面列表为空，未生成重试条目", False
        idem = str(values.get("幂等键") or "").strip()
        version = _version_text(values.get("作业版本") or job.get("作业版本"))

        with store.lock:
            self.prepare_entries()
            batch = store.rows(BATCH_MODULE)
            if idem:
                for row in batch:
                    if row.get("幂等键") == idem:
                        entry_ids = row.get("条目id") or []
                        entries = [store.find(MODULE, int(i)) for i in entry_ids]
                        return [item for item in entries if item], [], (
                            "重复提交已忽略（幂等键命中，未重复生成条目）"
                        ), True
            entries: list[dict[str, Any]] = []
            found: dict[int, dict[str, Any]] = {}
            skipped: list[str] = []
            messages: list[str] = []
            for raw in failures:
                if isinstance(raw, dict):
                    raw_url = str(raw.get("失败URL") or raw.get("url") or "").strip()
                    reason = str(raw.get("失败原因码") or raw.get("reason") or "").strip()
                    per_entry = dict(values)
                    per_entry.update({
                        key: value for key, value in raw.items()
                        if key not in {"失败URL", "url", "失败原因码", "reason"}
                    })
                else:
                    raw_url = str(raw or "").strip()
                    reason = ""
                    per_entry = dict(values)
                if not raw_url:
                    skipped.append("空URL")
                    continue
                if not reason:
                    reason = str(values.get("失败原因码") or "").strip()
                if not reason:
                    skipped.append(f"{raw_url}（缺少失败原因码）")
                    continue
                guard = self._validate_job_url(job, raw_url)
                if guard:
                    skipped.append(f"{raw_url}（不属于起始URL站点）")
                    messages.append(guard)
                    continue
                norm_guess = normalize_url(raw_url, job.get("URL去重策略"))
                known = any(
                    row.get("去重键") == dedup_key(job.get("作业编号"), version, norm_guess)
                    for row in store.rows(MODULE)
                )
                if not known:
                    guard = self._validate_open_job(job)
                    if guard:
                        skipped.append(f"{raw_url}（作业已结束）")
                        messages.append(guard)
                        continue
                entry, message = self._resolve_entry(
                    job=job,
                    version=version,
                    raw_url=raw_url,
                    reason=reason,
                    values=per_entry,
                )
                if entry is None:
                    skipped.append(f"{raw_url}（{message}）")
                    continue
                # 同一批次里规范化后相同的URL只返回一条条目，避免逐项复核时出现重复行。
                found[int(entry.get("id", 0))] = entry
                entries.append(entry)
                messages.append(message)
            unique_entries = list(found.values())
            if idem:
                batch.append({
                    "id": store.next_id(BATCH_MODULE),
                    "幂等键": idem,
                    "作业编号": job.get("作业编号"),
                    "作业版本": version,
                    "条目id": [int(item.get("id", 0)) for item in unique_entries],
                    "提交时间": _now(),
                })
            if unique_entries:
                self._sync_taskexec(unique_entries[0], "加入队列")
            return unique_entries, skipped, (
                f"已处理 {len(unique_entries)} 个唯一URL，按唯一URL合并后未新增重复条目"
                if not skipped else f"已处理 {len(unique_entries)} 个唯一URL，跳过 {len(skipped)} 条"
            ), False

    def _sync_taskexec(self, entry: dict[str, Any], action: str) -> None:
        # 延迟导入：taskexec 依赖队列数据，队列这里只在动作后回调它，避免循环导入。
        from app.services.taskexec import taskexec_service

        taskexec_service.sync_action(entry, action)

    def upsert_unconverged_from_result(
        self,
        *,
        job: dict[str, Any],
        version: str,
        raw_url: str,
        norm_url: str,
        reason: str,
        attempt: int,
        final_failed: bool,
    ) -> dict[str, Any]:
        """重试结果失败时保证队列里有一条未收敛条目，成功结果不建队列条目。"""
        with store.lock:
            self.prepare_entries()
            job_no = str(job.get("作业编号"))
            key = dedup_key(job_no, version, norm_url)
            entry = next((row for row in store.rows(MODULE) if row.get("去重键") == key), None)
            if entry is None:
                new_id = store.next_id(MODULE)
                entry = {
                    "id": new_id,
                    "条目编号": entry_code(new_id),
                    "作业编号": job_no,
                    "作业版本": _version_text(version),
                    "起始URL": job.get("起始URL") or "",
                    "页面解析器": job.get("页面解析器") or "",
                    "URL去重策略": job.get("URL去重策略") or "",
                    "去重键": key,
                    "失败URL": raw_url,
                    "规范化URL": norm_url,
                    "首次失败时间": _now(),
                    "已重试次数": 0,
                    "下次重试时间": "",
                    "退避级别": 0,
                    "最终处置": "",
                    "注册来源": "任务执行侧回写",
                    "重复登记次数": 0,
                }
                entry["失败原因码列表"] = merge_reasons(reason, None)
                entry["失败原因码"] = primary_reason(entry.get("失败原因码列表"))
                store.rows(MODULE).append(entry)
            entry["已重试次数"] = max(_attempt(entry.get("已重试次数"), 0), attempt)
            entry["退避级别"] = entry["已重试次数"]
            entry["失败原因码列表"] = merge_reasons(entry.get("失败原因码列表"), reason)
            entry["失败原因码"] = primary_reason(entry.get("失败原因码列表"))
            entry["最近更新时间"] = _now()
            if final_failed:
                self._mark_status(entry, "最终失败")
                entry["收敛状态"] = "已终局"
                entry["in_queue"] = False
                entry["最终处置"] = f"达到退避上限（{MAX_RETRY_ATTEMPTS}），转最终失败清单"
            else:
                self._mark_status(entry, "等待重试")
                entry["收敛状态"] = "未收敛"
                entry["in_queue"] = True
                entry["最终处置"] = ""
                entry["下次重试时间"] = entry.get("下次重试时间") or _now()
            return entry

        """队列视角的两侧核对：队列、索引清单、作业执行进度是否落在同一URL集合。"""
        from app.services.taskexec import taskexec_service

        result = taskexec_service.reconcile(job_no=job_no, version=version)
        queue_total, _ = self.list_entries(
            job_no=job_no, version=version, scope="unconverged", page=1, size=10000
        )
        result["重试队列未收敛条目数"] = len(queue_total)
        result["说明"] = "未收敛条目数应与各版本未收敛URL数之和一致；URL集合取自索引清单与进度。"
        return result

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            self.prepare_entries()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"重试条目 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于重试队列可执行范围"
            if entry.get("收敛状态") == "已收敛":
                return None, f"重试条目 {entry.get('条目编号')} 已重试成功并移出队列，不能再执行「{action}」"
            if entry.get("收敛状态") == "已终局":
                return None, f"重试条目 {entry.get('条目编号')} 已进入最终失败清单，不能再执行「{action}」"
            job = _find_job(entry.get("作业编号"))
            if job is not None and str(job.get("status")) in {"已完成", "已停止"}:
                return None, (
                    f"作业 {job.get('作业编号')} 已结束（{job.get('status')}），"
                    "队列动作不会把作业重新带回执行中"
                )
            target = ACTION_RULES[action]
            if action == "标记最终失败":
                attempts = _attempt(entry.get("已重试次数"), 0)
                if attempts < MAX_RETRY_ATTEMPTS:
                    return None, (
                        f"重试条目 {entry.get('条目编号')} 仅重试 {attempts} 次，"
                        f"未达到退避上限 {MAX_RETRY_ATTEMPTS}，不能进入最终失败清单"
                    )
            self._mark_status(entry, target)
            entry["最近更新时间"] = _now()
            if action == "加入队列":
                entry["收敛状态"] = "未收敛"
                entry["in_queue"] = True
                entry["最终处置"] = ""
            elif action == "发起重试":
                entry["收敛状态"] = "未收敛"
                entry["in_queue"] = True
                entry["已重试次数"] = _attempt(entry.get("已重试次数"), 0) + 1
                entry["退避级别"] = entry["已重试次数"]
            elif action == "标记最终失败":
                entry["收敛状态"] = "已终局"
                entry["in_queue"] = False
                entry["最终处置"] = f"达到退避上限（{MAX_RETRY_ATTEMPTS}），转最终失败清单"
            self._sync_taskexec(entry, action)
            return entry, f"重试条目已{action}"

    def reconnect_sync(self, values: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """网络中断恢复后的补同步：按幂等键重放，不重复生成条目。"""
        entries, _skipped, message, idempotent = self.register_failures(values)
        if idempotent:
            return entries, message
        return entries, message

    def reconcile(self, *, job_no: str | None = None, version: str | None = None) -> dict[str, Any]:
        """队列视角的两侧核对：队列、索引清单、作业执行进度是否落在同一URL集合。"""
        from app.services.taskexec import taskexec_service

        result = taskexec_service.reconcile(job_no=job_no, version=version)
        queue_total, _ = self.list_entries(
            job_no=job_no, version=version, scope="unconverged", page=1, size=10000
        )
        result["重试队列未收敛条目数"] = len(queue_total)
        result["说明"] = "未收敛条目数应与各版本未收敛URL数之和一致；URL集合取自索引清单与进度。"
        return result
