"""任务执行侧：接收重试结果、回写原作业进度，并维护结果索引清单。

任务执行侧是重试队列的下游：重试队列只负责「哪些失败URL还没收敛」，重试结果
统一从这里回写。三条硬约束都在这里落地：

1. 作业执行进度以「作业编号 + 作业版本」唯一，并发提交时由 ``store.lock``
   保证只会生成一条进度；
2. 作业结束（已完成 / 已停止）之后晚到的重试结果只归档，不把作业改回执行中；
3. URL 规范化前后指向同一页面但失败原因不同（尤其跨作业版本）时，进度记录
   同时保留两组版本依据，并按原因码优先级决定主原因。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.urlnorm import (
    merge_reasons,
    normalize_url,
    primary_reason,
    reason_priority,
    same_site,
)
from app.store import store

PROGRESS_MODULE = "taskexec_progress"
MANIFEST_MODULE = "taskexec_manifest"
RESULT_MODULE = "taskexec_retryresult"

MAX_RETRY_ATTEMPTS = 5
TERMINAL_JOB_STATUSES = {"已完成", "已停止"}
CONVERGED_STATES = {"已完成", "最终失败"}
UNCONVERGED_STATES = {"失败待重试", "重试中"}

PROGRESS_STATUSES = ["执行中", "已完成", "已停止"]
MANIFEST_STATUSES = ["失败待重试", "重试中", "已完成", "最终失败", "迟到归档"]
RESULT_STATUSES = ["成功", "失败", "忽略"]


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


def backoff_seconds(attempt: int) -> int:
    """指数退避：2^attempt 分钟，最长一小时。"""
    return min(2 ** max(attempt, 0) * 60, 3600)


def next_retry_time(attempt: int) -> str:
    return (datetime.now() + timedelta(seconds=backoff_seconds(attempt))).strftime("%Y-%m-%d %H:%M:%S")


def progress_code(entry_id: int) -> str:
    return f"PROG-{entry_id:04d}"


def manifest_code(entry_id: int) -> str:
    return f"MANI-{entry_id:04d}"


def result_code(entry_id: int) -> str:
    return f"RRES-{entry_id:04d}"


def dedup_key(job_no: object, version: object, norm_url: object) -> str:
    return f"{job_no}|{_version_text(version)}|{norm_url}"


class TaskexecService:
    # ------------------------------------------------------------------ 基础查询
    def _prepare_queue(self) -> None:
        # 延迟导入避免循环依赖；队列负责补齐作业版本与规范化URL。
        from app.services.retryqueue import RetryqueueService

        RetryqueueService().prepare_entries()

    def job(self, job_no: object) -> dict[str, Any] | None:
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

    def job_version(self, job: dict[str, Any] | None) -> str:
        return _version_text((job or {}).get("作业版本"))

    def progress_rows(self) -> list[dict[str, Any]]:
        return store.rows(PROGRESS_MODULE)

    def manifest_rows(self) -> list[dict[str, Any]]:
        return store.rows(MANIFEST_MODULE)

    def result_rows(self) -> list[dict[str, Any]]:
        return store.rows(RESULT_MODULE)

    def find_progress(self, job_no: object, version: object) -> dict[str, Any] | None:
        key = str(job_no or "").strip()
        ver = _version_text(version)
        for row in self.progress_rows():
            if str(row.get("作业编号")) == key and str(row.get("作业版本")) == ver:
                return row
        return None

    def find_manifest(self, job_no: object, version: object, norm_url: object) -> dict[str, Any] | None:
        key = dedup_key(job_no, version, norm_url)
        for row in self.manifest_rows():
            if row.get("去重键") == key:
                return row
        return None

    def find_result(self, result_no: object) -> dict[str, Any] | None:
        text = str(result_no or "").strip()
        for row in self.result_rows():
            if row.get("结果编号") == text:
                return row
        return None

    def _find_result_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        for row in self.result_rows():
            if row.get("幂等键") == idempotency_key:
                return row
        return None

    # ------------------------------------------------------------------ 进度与清单
    def ensure_progress(self, job_no: object, version: object) -> dict[str, Any]:
        """按「作业编号 + 作业版本」取唯一进度；不存在时在锁内创建。"""
        with store.lock:
            existing = self.find_progress(job_no, version)
            if existing is not None:
                return existing
            job = self.job(job_no) or {}
            new_id = store.next_id(PROGRESS_MODULE)
            row: dict[str, Any] = {
                "id": new_id,
                "status": "执行中",
                "pending": True,
                "abnormal": False,
                "进度编号": progress_code(new_id),
                "作业编号": str(job_no or "").strip(),
                "作业版本": _version_text(version),
                "起始URL": job.get("起始URL") or "",
                "页面解析器": job.get("页面解析器") or "",
                "URL去重策略": job.get("URL去重策略") or "",
                "总URL数": 0,
                "完成URL数": 0,
                "失败URL数": 0,
                "未收敛URL数": 0,
                "最终失败URL数": 0,
                "迟到结果数": 0,
                "唯一URL集合": [],
                "版本依据": [],
                "冲突标记": False,
                "冲突处置": "",
                "主原因码": "",
                "冲突原因优先级": [],
                "更新时间": _now(),
            }
            self.progress_rows().append(row)
            return row

    def _new_manifest(
        self,
        *,
        job_no: str,
        version: str,
        norm_url: str,
        raw_url: str,
        reason: str,
        state: str,
        source: str,
        result_no: str = "",
        note: str = "",
        late: bool = False,
    ) -> dict[str, Any]:
        new_id = store.next_id(MANIFEST_MODULE)
        row: dict[str, Any] = {
            "id": new_id,
            "status": state,
            "pending": state in UNCONVERGED_STATES,
            "abnormal": state in {"最终失败", "迟到归档"},
            "索引编号": manifest_code(new_id),
            "作业编号": job_no,
            "作业版本": version,
            "进度编号": "",
            "去重键": dedup_key(job_no, version, norm_url),
            "失败URL": raw_url,
            "规范化URL": norm_url,
            "失败原因码": reason,
            "失败原因码列表": merge_reasons(reason, None),
            "结果状态": state,
            "重试结果编号": result_no,
            "回写来源": source,
            "最近回写时间": _now(),
            "迟到回写": late,
            "说明": note,
        }
        self.manifest_rows().append(row)
        return row

    def _state_for_entry(self, entry: dict[str, Any]) -> str:
        status = str(entry.get("status") or "")
        if status == "重试成功":
            return "已完成"
        if status == "最终失败":
            return "最终失败"
        if status == "重试中":
            return "重试中"
        return "失败待重试"

    def _attach_missing_manifests(self) -> None:
        """重试队列里的每个条目都要有对应的结果索引，保证清单能回查进度。"""
        for entry in store.rows("retryqueue"):
            job_no = str(entry.get("作业编号") or "").strip()
            norm_url = str(entry.get("规范化URL") or "").strip()
            if not job_no or not norm_url:
                continue
            version = _version_text(entry.get("作业版本"))
            manifest = self.find_manifest(job_no, version, norm_url)
            state = self._state_for_entry(entry)
            if manifest is None:
                manifest = self._new_manifest(
                    job_no=job_no,
                    version=version,
                    norm_url=norm_url,
                    raw_url=str(entry.get("失败URL") or norm_url),
                    reason=primary_reason(entry.get("失败原因码"), entry.get("失败原因码列表")),
                    state=state,
                    source=str(entry.get("注册来源") or "失败页面"),
                    note="由重试队列条目生成",
                )
            elif not manifest.get("重试结果编号"):
                # 队列状态是当前事实，清单跟随队列，避免两边状态不一致。
                manifest["结果状态"] = state
                manifest["status"] = state
                manifest["pending"] = state in UNCONVERGED_STATES
                manifest["abnormal"] = state in {"最终失败", "迟到归档"}
                manifest["失败原因码列表"] = merge_reasons(
                    manifest.get("失败原因码列表"), entry.get("失败原因码列表")
                )
                manifest["失败原因码"] = primary_reason(manifest.get("失败原因码列表"))

    def refresh_all(self) -> None:
        """重建全部进度与版本依据；任何回写动作结束后都调用一次。"""
        with store.lock:
            self._prepare_queue()
            self._attach_missing_manifests()
            keys: set[tuple[str, str]] = set()
            for row in store.rows("retryqueue"):
                job_no = str(row.get("作业编号") or "").strip()
                if job_no and str(row.get("规范化URL") or "").strip():
                    keys.add((job_no, _version_text(row.get("作业版本"))))
            for manifest in self.manifest_rows():
                job_no = str(manifest.get("作业编号") or "").strip()
                if job_no and str(manifest.get("规范化URL") or "").strip():
                    keys.add((job_no, _version_text(manifest.get("作业版本"))))
            for job_no, version in sorted(keys):
                self.ensure_progress(job_no, version)
            for progress in self.progress_rows():
                self._recompute_counts(progress)
            self._recompute_conflicts()
            for progress in self.progress_rows():
                self._sync_job(progress)

    def _manifest_for_progress(self, progress: dict[str, Any]) -> list[dict[str, Any]]:
        job_no = str(progress.get("作业编号"))
        version = _version_text(progress.get("作业版本"))
        return [
            row for row in self.manifest_rows()
            if str(row.get("作业编号")) == job_no
            and _version_text(row.get("作业版本")) == version
        ]

    def _entries_for_progress(self, progress: dict[str, Any]) -> list[dict[str, Any]]:
        job_no = str(progress.get("作业编号"))
        version = _version_text(progress.get("作业版本"))
        return [
            row for row in store.rows("retryqueue")
            if str(row.get("作业编号")) == job_no
            and _version_text(row.get("作业版本")) == version
        ]

    def _recompute_counts(self, progress: dict[str, Any]) -> None:
        manifests = self._manifest_for_progress(progress)
        entries = self._entries_for_progress(progress)
        urls = {str(row.get("规范化URL")) for row in manifests if row.get("规范化URL")}
        urls |= {str(row.get("规范化URL")) for row in entries if row.get("规范化URL")}
        unique_urls = sorted(urls)
        counts = {state: 0 for state in MANIFEST_STATUSES}
        late = 0
        for row in manifests:
            state = str(row.get("结果状态") or "失败待重试")
            counts[state] = counts.get(state, 0) + 1
            if row.get("迟到回写"):
                late += 1
        completed = counts.get("已完成", 0)
        unconverged = counts.get("失败待重试", 0) + counts.get("重试中", 0)
        final_failed = counts.get("最终失败", 0)
        late_archived = counts.get("迟到归档", 0)
        progress["成功URL数"] = completed
        progress["完成URL数"] = completed
        # 迟到结果单独归档，但仍然计入失败数，保证 总数 = 完成 + 失败 恒成立。
        progress["失败URL数"] = unconverged + final_failed + late_archived
        progress["未收敛URL数"] = unconverged
        progress["最终失败URL数"] = final_failed
        progress["迟到结果数"] = max(late, late_archived)
        progress["总URL数"] = len(unique_urls)
        progress["唯一URL集合"] = unique_urls
        progress["计数明细"] = {
            "已完成": completed,
            "失败待重试": counts.get("失败待重试", 0),
            "重试中": counts.get("重试中", 0),
            "最终失败": final_failed,
            "迟到归档": late_archived,
            "唯一URL数": len(unique_urls),
        }
        job = self.job(progress.get("作业编号"))
        if job is not None and str(job.get("status")) == "已停止":
            status = "已停止"
        elif unconverged > 0:
            status = "执行中"
        elif len(unique_urls) > 0 and str(job.get("status")) in TERMINAL_JOB_STATUSES:
            status = "已完成"
        else:
            # 队列暂时清空不代表作业结束：作业是否收尾由作业本身的动作决定，
            # 否则一次重试回写就会把作业提前收掉，后续正常回写全变成「迟到」。
            status = "执行中"
        progress["status"] = status
        progress["进度状态"] = status
        progress["pending"] = status == "执行中"
        progress["abnormal"] = bool(progress.get("冲突标记")) or final_failed > 0
        progress["更新时间"] = _now()
        for manifest in manifests:
            manifest["进度编号"] = progress.get("进度编号")

    def _sync_job(self, progress: dict[str, Any]) -> None:
        """只在作业收尾时同步进度；绝不把已结束的作业拉回执行中。"""
        job = self.job(progress.get("作业编号"))
        if job is None:
            return
        if str(job.get("status")) in TERMINAL_JOB_STATUSES and str(progress.get("status")) != "已停止":
            progress["status"] = "已完成"
            progress["进度状态"] = "已完成"
            progress["pending"] = False

    def _recompute_conflicts(self) -> None:
        """跨版本比对同一页面的失败原因，保留两组版本依据与主原因优先级。

        作业不同版本可能用不同的URL去重策略，所以比对前用默认策略再规范化一次，
        避免「规范化前后指向同一页面、但失败原因不同」被拆到两个版本看不出来。
        """
        by_job: dict[str, list[dict[str, Any]]] = {}
        for progress in self.progress_rows():
            by_job.setdefault(str(progress.get("作业编号")), []).append(progress)

        for job_no, progresses in by_job.items():
            bases_by_page: dict[str, list[dict[str, Any]]] = {}
            versions_by_page: dict[str, set[str]] = {}
            reasons_by_page: dict[str, dict[str, tuple[str, str]]] = {}
            for progress in progresses:
                version = _version_text(progress.get("作业版本"))
                for manifest in self._manifest_for_progress(progress):
                    norm = str(manifest.get("规范化URL") or "")
                    if not norm:
                        continue
                    page = normalize_url(norm) or norm
                    reason = primary_reason(
                        manifest.get("失败原因码"), manifest.get("失败原因码列表")
                    )
                    base = {
                        "作业版本": version,
                        "来源": str(manifest.get("回写来源") or "失败页面"),
                        "规范化URL": norm,
                        "失败URL": str(manifest.get("失败URL") or norm),
                        "失败原因码": reason,
                        "记录时间": str(manifest.get("最近回写时间") or ""),
                    }
                    bucket = bases_by_page.setdefault(page, [])
                    if base not in bucket:
                        bucket.append(base)
                    versions_by_page.setdefault(page, set()).add(version)
                    if reason:
                        reasons_by_page.setdefault(page, {})[version] = (reason, norm)

            for progress in progresses:
                urls = set(progress.get("唯一URL集合") or [])
                pages = {normalize_url(url) or url for url in urls}
                bases: list[dict[str, Any]] = []
                conflicts: list[dict[str, Any]] = []
                for page in sorted(pages):
                    for base in bases_by_page.get(page, []):
                        if base not in bases:
                            bases.append(base)
                    versions = versions_by_page.get(page, set())
                    reason_map = reasons_by_page.get(page, {})
                    reasons = {reason for reason, _ in reason_map.values() if reason}
                    if len(versions) > 1 and len(reasons) > 1:
                        ordered = sorted(reasons, key=lambda code: (-reason_priority(code), code))
                        conflicts.append({
                            "页面": page,
                            "规范化URL": page,
                            "涉及作业版本": sorted(versions),
                            "原因码": ordered,
                            "主原因码": ordered[0],
                            "各版本规范化URL": {
                                version: norm for version, (_, norm) in reason_map.items()
                            },
                        })
                bases.sort(key=lambda item: (str(item.get("作业版本")), str(item.get("记录时间"))))
                progress["版本依据"] = bases
                progress["冲突标记"] = bool(conflicts)
                progress["冲突明细"] = conflicts
                if conflicts:
                    first = conflicts[0]
                    keep = "、".join(first["原因码"])
                    progress["主原因码"] = first["主原因码"]
                    progress["冲突原因优先级"] = first["原因码"]
                    progress["冲突处置"] = (
                        f"版本冲突：{first['页面']} 在作业版本 "
                        f"{'、'.join(first['涉及作业版本'])} 均有记录，失败原因不同（{keep}）。"
                        f"按优先级保留 {first['主原因码']} 为主原因，其余原因保留在版本依据中，"
                        "两个版本依据都可逐项复核。"
                    )
                else:
                    progress["主原因码"] = primary_reason(
                        *[base.get("失败原因码") for base in bases]
                    )
                    progress["冲突原因优先级"] = []
                    progress["冲突处置"] = ""

    # ------------------------------------------------------------------ 重试结果回写
    def write_retry_result(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """接收任务执行侧重试结果并回写原作业进度。

        返回 ``(结果行, 说明, 附加信息)``；附加信息里带队列条目、进度和清单，
        方便接口一次性返回两侧状态。
        """
        idem = str(values.get("幂等键") or "").strip()
        existing_result = self._find_result_by_key(idem)
        if existing_result is not None:
            progress = self.find_progress(existing_result.get("作业编号"), existing_result.get("作业版本"))
            return existing_result, "重复提交已忽略（幂等键命中，未重复回写）", {
                "幂等": True,
                "进度": progress,
                "索引": self.find_manifest(
                    existing_result.get("作业编号"),
                    existing_result.get("作业版本"),
                    existing_result.get("规范化URL"),
                ),
                "迟到": bool(existing_result.get("迟到")),
            }

        job = self.job(values.get("作业编号") or values.get("job"))
        if job is None:
            return None, f"作业 {values.get('作业编号') or ''} 不存在，重试结果无法回写", {"幂等": False}
        job_no = str(job.get("作业编号"))
        version = _version_text(values.get("作业版本") or job.get("作业版本"))
        raw_url = str(values.get("失败URL") or values.get("规范化URL") or "").strip()
        norm_url = normalize_url(values.get("规范化URL") or raw_url, job.get("URL去重策略"))
        if not norm_url:
            return None, "失败URL为空或无法规范化，重试结果未回写", {"幂等": False}
        if not same_site(raw_url or norm_url, job.get("起始URL")):
            return None, (
                f"失败URL {raw_url or norm_url} 不属于作业 {job_no} 起始URL所在站点，"
                "重试结果不能脱离原作业回写"
            ), {"幂等": False}

        raw_result = str(values.get("结果") or values.get("retry_result") or "").strip()
        success = raw_result in {"成功", "重试成功", "SUCCESS", "success", "OK", "ok"}
        failure = raw_result in {"失败", "FAILURE", "failure", "FAIL", "fail"}
        if not success and not failure:
            return None, f"重试结果「{raw_result}」无法识别，只接受成功或失败", {"幂等": False}

        incoming_reason = str(values.get("失败原因码") or "").strip()
        attempt = _attempt(values.get("尝试次数") or values.get("已重试次数"), 0)

        with store.lock:
            self._prepare_queue()
            progress = self.ensure_progress(job_no, version)
            manifest = self.find_manifest(job_no, version, norm_url)
            entry = None
            for row in store.rows("retryqueue"):
                if row.get("去重键") == dedup_key(job_no, version, norm_url):
                    entry = row
                    break
            job_status = str(job.get("status") or "")
            already_completed = manifest is not None and str(manifest.get("结果状态")) == "已完成"
            # 已完成的页面重复回写只忽略、不再回退作业状态，所以不算「迟到归档」。
            late = (
                job_status in TERMINAL_JOB_STATUSES
                or str(progress.get("status")) == "已停止"
            ) and not already_completed

            if entry is not None and attempt <= 0:
                attempt = _attempt(entry.get("已重试次数"), 0) + 1
            attempt = max(attempt, 1)

            result_id = store.next_id(RESULT_MODULE)
            result_row: dict[str, Any] = {
                "id": result_id,
                "status": "成功" if success else "失败",
                "pending": False,
                "abnormal": (not success) and not late,
                "结果编号": result_code(result_id),
                "作业编号": job_no,
                "作业版本": version,
                "进度编号": progress.get("进度编号"),
                "索引编号": (manifest or {}).get("索引编号", ""),
                "去重键": dedup_key(job_no, version, norm_url),
                "失败URL": raw_url or norm_url,
                "规范化URL": norm_url,
                "尝试序号": attempt,
                "结果": "成功" if success else "失败",
                "失败原因码": incoming_reason if failure else "",
                "失败原因码列表": merge_reasons(incoming_reason, None) if failure else [],
                "收到时间": str(values.get("收到时间") or _now()),
                "幂等键": idem,
                "回写来源": str(values.get("回写来源") or "任务执行侧"),
                "迟到": late,
                "处置": "",
            }

            if late:
                result_row["处置"] = "迟到结果已归档，作业保持结束状态"
                result_row["pending"] = False
                if manifest is None:
                    manifest = self._new_manifest(
                        job_no=job_no,
                        version=version,
                        norm_url=norm_url,
                        raw_url=raw_url or norm_url,
                        reason=incoming_reason,
                        state="迟到归档",
                        source=str(result_row["回写来源"]),
                        result_no=result_row["结果编号"],
                        note="作业结束后晚到的重试结果",
                        late=True,
                    )
                else:
                    manifest["迟到回写"] = True
                    manifest["迟到结果编号"] = result_row["结果编号"]
                    manifest["说明"] = "作业结束后晚到的重试结果，仅归档不回退作业状态"
                    if not success:
                        manifest["失败原因码列表"] = merge_reasons(
                            manifest.get("失败原因码列表"), incoming_reason
                        )
                result_row["索引编号"] = manifest.get("索引编号", "")
                self.result_rows().append(result_row)
                self.refresh_all()
                progress = self.find_progress(job_no, version) or progress
                manifest = self.find_manifest(job_no, version, norm_url)
                return result_row, (
                    f"作业已结束（{job_status or progress.get('进度状态')}），重试结果已归档；"
                    "作业状态保持不变"
                ), {"幂等": False, "迟到": True, "进度": progress, "索引": manifest}

            if already_completed and success:
                result_row["处置"] = "已完成页面重复回写，已忽略"
                result_row["结果"] = "忽略"
                result_row["status"] = "忽略"
                result_row["abnormal"] = False
                if idem:
                    self.result_rows().append(result_row)
                self.refresh_all()
                return result_row, "该页面已完成，重复重试回写已忽略，不会重新抓取", {
                    "幂等": False,
                    "迟到": False,
                    "进度": self.find_progress(job_no, version),
                    "索引": manifest,
                }

            if manifest is None:
                manifest = self._new_manifest(
                    job_no=job_no,
                    version=version,
                    norm_url=norm_url,
                    raw_url=raw_url or norm_url,
                    reason=incoming_reason,
                    state="已完成" if success else "最终失败",
                    source=str(result_row["回写来源"]),
                    result_no=result_row["结果编号"],
                    note="任务执行侧直接回报的重试结果",
                )
            manifest["重试结果编号"] = result_row["结果编号"]
            manifest["回写来源"] = str(result_row["回写来源"])
            manifest["最近回写时间"] = _now()
            manifest["失败原因码列表"] = merge_reasons(
                manifest.get("失败原因码列表"), incoming_reason
            )
            manifest["失败原因码"] = primary_reason(
                manifest.get("失败原因码列表"), incoming_reason
            )

            if success:
                result_row["处置"] = "已回写作业进度"
                manifest["结果状态"] = "已完成"
                manifest["迟到回写"] = False
                manifest["说明"] = "重试成功，页面完成"
                if entry is not None:
                    entry["status"] = "重试成功"
                    entry["已重试次数"] = attempt
                    entry["退避级别"] = 0
                    entry["收敛状态"] = "已收敛"
                    entry["in_queue"] = False
                    entry["最终处置"] = "重试成功，已移出重试队列"
                    entry["最近重试结果"] = result_row["结果编号"]
                    entry["最近更新时间"] = _now()
            else:
                # 失败结果必须先落到重试队列，否则进度里有URL、队列里没有条目。
                from app.services.retryqueue import RetryqueueService

                entry = RetryqueueService().upsert_unconverged_from_result(
                    job=job,
                    version=version,
                    raw_url=raw_url or norm_url,
                    norm_url=norm_url,
                    reason=incoming_reason,
                    attempt=attempt,
                    final_failed=attempt >= MAX_RETRY_ATTEMPTS,
                )
                entry["最近重试结果"] = result_row["结果编号"]
                if attempt >= MAX_RETRY_ATTEMPTS:
                    result_row["处置"] = "达到退避上限，转最终失败清单"
                    manifest["结果状态"] = "最终失败"
                    manifest["说明"] = f"已重试 {attempt} 次，达到退避上限 {MAX_RETRY_ATTEMPTS}"
                else:
                    result_row["处置"] = f"未收敛，进入第 {attempt} 级退避"
                    manifest["结果状态"] = "失败待重试"
                    manifest["说明"] = f"第 {attempt} 次重试失败，等待下次退避重试"
                    entry["下次重试时间"] = next_retry_time(attempt)
            manifest["status"] = manifest["结果状态"]
            manifest["pending"] = manifest["结果状态"] in UNCONVERGED_STATES
            manifest["abnormal"] = manifest["结果状态"] in {"最终失败", "迟到归档"}

            self.result_rows().append(result_row)
            self.refresh_all()
            progress = self.find_progress(job_no, version) or progress
            manifest = self.find_manifest(job_no, version, norm_url)
            message = "重试结果已回写作业进度"
            if success:
                message += "，页面已完成并移出重试队列"
            elif attempt >= MAX_RETRY_ATTEMPTS:
                message += "，已达到退避上限并进入最终失败清单"
            else:
                message += f"，条目保留在重试队列，进入第 {attempt} 级退避"
            return result_row, message, {
                "幂等": False,
                "迟到": False,
                "进度": progress,
                "索引": manifest,
                "重试队列条目": entry,
            }

    def sync_action(self, entry: dict[str, Any], action: str) -> None:
        """重试队列动作回写任务执行侧清单：另一侧清单一同变化。"""
        job_no = str(entry.get("作业编号") or "").strip()
        norm_url = str(entry.get("规范化URL") or "").strip()
        if not job_no or not norm_url:
            return
        version = _version_text(entry.get("作业版本"))
        source = str(entry.get("注册来源") or "失败页面")
        with store.lock:
            manifest = self.find_manifest(job_no, version, norm_url)
            if manifest is None:
                manifest = self._new_manifest(
                    job_no=job_no,
                    version=version,
                    norm_url=norm_url,
                    raw_url=str(entry.get("失败URL") or norm_url),
                    reason=primary_reason(entry.get("失败原因码"), entry.get("失败原因码列表")),
                    state=self._state_for_entry(entry),
                    source=source,
                    note=f"由重试队列登记（{source}）",
                )
            if action == "加入队列":
                manifest["结果状态"] = "失败待重试"
            elif action == "发起重试":
                manifest["结果状态"] = "重试中"
            elif action == "标记最终失败":
                manifest["结果状态"] = "最终失败"
            manifest["status"] = manifest["结果状态"]
            manifest["pending"] = manifest["结果状态"] in UNCONVERGED_STATES
            manifest["abnormal"] = manifest["结果状态"] in {"最终失败", "迟到归档"}
            # 保留原始登记来源，人工动作只体现在说明里，避免版本依据被反复覆盖。
            manifest["回写来源"] = manifest.get("回写来源") or source
            manifest["最近回写时间"] = _now()
            manifest["说明"] = f"重试队列动作「{action}」同步回写"
            manifest["失败原因码列表"] = merge_reasons(
                manifest.get("失败原因码列表"), entry.get("失败原因码列表")
            )
            manifest["失败原因码"] = primary_reason(manifest.get("失败原因码列表"))
            self.refresh_all()

    # ------------------------------------------------------------------ 列表与数据集
    def list_progress(
        self,
        *,
        job_no: str | None = None,
        version: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.refresh_all()
        rows = list(self.progress_rows())
        if job_no:
            rows = [row for row in rows if job_no in str(row.get("作业编号", ""))]
        if version:
            rows = [row for row in rows if str(row.get("作业版本")) == str(version)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_progress(self, entry_id: int) -> dict[str, Any] | None:
        self.refresh_all()
        return store.find(PROGRESS_MODULE, entry_id)

    def get_progress_by_code(self, code: str) -> dict[str, Any] | None:
        self.refresh_all()
        for row in self.progress_rows():
            if row.get("进度编号") == code:
                return row
        return None

    def list_manifest(
        self,
        *,
        job_no: str | None = None,
        version: str | None = None,
        state: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.refresh_all()
        rows = list(self.manifest_rows())
        if job_no:
            rows = [row for row in rows if job_no in str(row.get("作业编号", ""))]
        if version:
            rows = [row for row in rows if str(row.get("作业版本")) == str(version)]
        if state:
            rows = [row for row in rows if row.get("结果状态") == state]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("规范化URL", ""))
                or keyword in str(row.get("失败URL", ""))
                or keyword in str(row.get("索引编号", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_results(
        self,
        *,
        job_no: str | None = None,
        version: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.refresh_all()
        rows = list(self.result_rows())
        if job_no:
            rows = [row for row in rows if job_no in str(row.get("作业编号", ""))]
        if version:
            rows = [row for row in rows if str(row.get("作业版本")) == str(version)]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("结果编号", ""))
                or keyword in str(row.get("规范化URL", ""))
                or keyword in str(row.get("失败URL", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def audit(self, job_no: str, version: object) -> dict[str, Any]:
        """按同一 URL 集合核对两侧计数：进度、索引清单、重试结果都从这里取数。"""
        self.refresh_all()
        progress = self.find_progress(job_no, version)
        if progress is None:
            return {
                "作业编号": job_no,
                "作业版本": _version_text(version),
                "进度编号": "",
                "唯一URL集合": [],
                "计数": {},
                "明细": [],
                "核对": {"存在进度": False, "计数与URL集合一致": False},
            }
        manifests = self._manifest_for_progress(progress)
        by_url = {str(row.get("规范化URL")): row for row in manifests}
        details: list[dict[str, Any]] = []
        for norm_url in progress.get("唯一URL集合") or []:
            manifest = by_url.get(norm_url)
            result_no = (manifest or {}).get("重试结果编号", "")
            result = self.find_result(result_no)
            details.append({
                "规范化URL": norm_url,
                "失败URL": (manifest or {}).get("失败URL", ""),
                "失败原因码": (manifest or {}).get("失败原因码", ""),
                "结果状态": (manifest or {}).get("结果状态", ""),
                "索引编号": (manifest or {}).get("索引编号", ""),
                "重试结果编号": result_no,
                "重试结果": (result or {}).get("结果", ""),
                "进度编号": progress.get("进度编号"),
                "作业编号": job_no,
                "作业版本": _version_text(version),
                "迟到回写": bool((manifest or {}).get("迟到回写")),
            })
        counts = {
            "总URL数": progress.get("总URL数", 0),
            "完成URL数": progress.get("完成URL数", 0),
            "失败URL数": progress.get("失败URL数", 0),
            "未收敛URL数": progress.get("未收敛URL数", 0),
            "最终失败URL数": progress.get("最终失败URL数", 0),
            "迟到归档数": sum(1 for row in manifests if str(row.get("结果状态")) == "迟到归档"),
            "迟到结果数": progress.get("迟到结果数", 0),
        }
        unique_count = len(progress.get("唯一URL集合") or [])
        reconciled = (
            unique_count == counts["总URL数"]
            and counts["总URL数"] == counts["完成URL数"] + counts["失败URL数"]
            and counts["失败URL数"] == (
                counts["未收敛URL数"] + counts["最终失败URL数"] + int(counts.get("迟到归档数", 0))
            )
            and len(details) == unique_count
        )
        return {
            "作业编号": job_no,
            "作业版本": _version_text(version),
            "进度编号": progress.get("进度编号"),
            "进度状态": progress.get("进度状态") or progress.get("status"),
            "唯一URL集合": list(progress.get("唯一URL集合") or []),
            "计数": counts,
            "明细": details,
            "版本依据": list(progress.get("版本依据") or []),
            "冲突标记": bool(progress.get("冲突标记")),
            "冲突处置": progress.get("冲突处置", ""),
            "主原因码": progress.get("主原因码", ""),
            "冲突原因优先级": list(progress.get("冲突原因优先级") or []),
            "冲突明细": list(progress.get("冲突明细") or []),
            "核对": {
                "存在进度": True,
                "URL集合大小": unique_count,
                "总URL数等于集合大小": unique_count == counts["总URL数"],
                "总URL数等于完成加失败": counts["总URL数"] == counts["完成URL数"] + counts["失败URL数"],
                "失败数等于未收敛加最终失败加迟到归档": counts["失败URL数"] == (
                    counts["未收敛URL数"]
                    + counts["最终失败URL数"]
                    + int(counts.get("迟到归档数", 0))
                ),
                "明细条数等于集合大小": len(details) == unique_count,
                "计数与URL集合一致": reconciled,
            },
        }

    def progress_dataset(self, job_no: str | None = None) -> dict[str, Any]:
        self.refresh_all()
        items: list[dict[str, Any]] = []
        for progress in self.progress_rows():
            if job_no and job_no not in str(progress.get("作业编号", "")):
                continue
            audit = self.audit(str(progress.get("作业编号")), progress.get("作业版本"))
            items.append({
                "进度编号": progress.get("进度编号"),
                "作业编号": progress.get("作业编号"),
                "作业版本": progress.get("作业版本"),
                "进度状态": progress.get("进度状态") or progress.get("status"),
                "起始URL": progress.get("起始URL"),
                "页面解析器": progress.get("页面解析器"),
                "URL去重策略": progress.get("URL去重策略"),
                "总URL数": progress.get("总URL数"),
                "完成URL数": progress.get("完成URL数"),
                "失败URL数": progress.get("失败URL数"),
                "未收敛URL数": progress.get("未收敛URL数"),
                "最终失败URL数": progress.get("最终失败URL数"),
                "迟到结果数": progress.get("迟到结果数"),
                "唯一URL集合": progress.get("唯一URL集合"),
                "版本依据": progress.get("版本依据"),
                "冲突标记": progress.get("冲突标记"),
                "冲突处置": progress.get("冲突处置"),
                "计数核对": audit.get("核对"),
                "明细": audit.get("明细"),
            })
        return {
            "dataset": "作业执行进度数据集",
            "total": len(items),
            "items": items,
            "说明": "每一行都带唯一URL集合与逐URL明细，计数可从同一集合复核。",
        }

    def retry_result_dataset(self, job_no: str | None = None) -> dict[str, Any]:
        """重试结果数据集：按唯一URL一行，和作业执行进度共用同一 URL 集合。"""
        self.refresh_all()
        items: list[dict[str, Any]] = []
        for progress in self.progress_rows():
            if job_no and job_no not in str(progress.get("作业编号", "")):
                continue
            audit = self.audit(str(progress.get("作业编号")), progress.get("作业版本"))
            manifests = self._manifest_for_progress(progress)
            entry_by_url = {
                str(row.get("规范化URL")): row for row in self._entries_for_progress(progress)
            }
            rows: list[dict[str, Any]] = []
            for detail in audit.get("明细", []):
                norm = detail["规范化URL"]
                entry = entry_by_url.get(norm) or {}
                result = self.find_result(detail.get("重试结果编号"))
                rows.append({
                    "作业编号": progress.get("作业编号"),
                    "作业版本": progress.get("作业版本"),
                    "进度编号": progress.get("进度编号"),
                    "重试条目编号": entry.get("条目编号", ""),
                    "规范化URL": norm,
                    "失败URL": detail.get("失败URL", ""),
                    "失败原因码": detail.get("失败原因码", ""),
                    "结果状态": detail.get("结果状态", ""),
                    "重试结果编号": detail.get("重试结果编号", ""),
                    "尝试序号": (result or {}).get("尝试序号", entry.get("已重试次数", "")),
                    "结果": (result or {}).get("结果", ""),
                    "索引编号": detail.get("索引编号", ""),
                    "迟到回写": detail.get("迟到回写", False),
                    "去重键": f"{progress.get('作业编号')}|{progress.get('作业版本')}|{norm}",
                })
            items.append({
                "作业编号": progress.get("作业编号"),
                "作业版本": progress.get("作业版本"),
                "进度编号": progress.get("进度编号"),
                "唯一URL数": len(progress.get("唯一URL集合") or []),
                "重试结果条数": len(rows),
                "计数核对": audit.get("核对"),
                "URL集合": progress.get("唯一URL集合"),
                "items": rows,
            })
        return {
            "dataset": "重试结果数据集",
            "total": len(items),
            "items": items,
            "说明": "每个作业版本一行，URL集合与作业执行进度数据集一致，可逐项核对。",
        }

    def reconcile(self, job_no: str | None = None, version: object | None = None) -> dict[str, Any]:
        self.refresh_all()
        keys: list[tuple[str, str]] = []
        for progress in self.progress_rows():
            if job_no and job_no not in str(progress.get("作业编号", "")):
                continue
            if version is not None and str(progress.get("作业版本")) != _version_text(version):
                continue
            keys.append((str(progress.get("作业编号")), _version_text(progress.get("作业版本"))))
        items: list[dict[str, Any]] = []
        for key_job, key_version in sorted(keys):
            audit = self.audit(key_job, key_version)
            entries = [
                row for row in store.rows("retryqueue")
                if str(row.get("作业编号")) == key_job
                and _version_text(row.get("作业版本")) == key_version
            ]
            queue_urls = {str(row.get("规范化URL")) for row in entries if row.get("规范化URL")}
            progress_urls = set(audit.get("唯一URL集合") or [])
            manifest_urls = {
                str(row.get("规范化URL"))
                for row in self.manifest_rows()
                if str(row.get("作业编号")) == key_job
                and _version_text(row.get("作业版本")) == key_version
            }
            queue_unconverged = [
                row for row in entries if row.get("in_queue") and row.get("收敛状态") == "未收敛"
            ]
            consistent = (
                len(progress_urls) == len(manifest_urls)
                and progress_urls == manifest_urls
                and (queue_urls <= progress_urls)
                and len(queue_unconverged) == int(audit.get("计数", {}).get("未收敛URL数", 0))
                and bool(audit.get("核对", {}).get("计数与URL集合一致"))
            )
            items.append({
                "作业编号": key_job,
                "作业版本": key_version,
                "进度编号": audit.get("进度编号"),
                "重试队列URL数": len(queue_urls),
                "进度URL数": len(progress_urls),
                "索引清单URL数": len(manifest_urls),
                "未收敛条目数": len(queue_unconverged),
                "未收敛URL数": audit.get("计数", {}).get("未收敛URL数", 0),
                "计数核对": audit.get("核对"),
                "两侧一致": consistent,
                "差异URL": sorted(progress_urls.symmetric_difference(manifest_urls)),
            })
        return {
            "total": len(items),
            "items": items,
            "全部一致": all(item["两侧一致"] for item in items) if items else True,
        }


taskexec_service = TaskexecService()
