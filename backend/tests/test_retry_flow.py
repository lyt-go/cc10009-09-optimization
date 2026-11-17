"""重试回写与作业执行进度的回归用例。

用标准库 unittest 即可运行（后端依赖里没有 pytest）：

    cd backend && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v

用例覆盖需求里的关键约束：URL 去重、并发只生成一条进度、作业结束后晚到结果、
两侧计数可核对、原因码检索与优先级。
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.main import app
from app.services.retryqueue import RetryqueueService
from app.services.taskexec import TaskexecService, taskexec_service
from app.services.urlnorm import merge_reasons, normalize_url, primary_reason
from app.store import store

try:  # TestClient 依赖 httpx，缺依赖时跳过接口层用例。
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]


class NormalizeTest(unittest.TestCase):
    def test_equivalent_forms_collapse(self) -> None:
        expected = "https://shop.example.com/item/1001"
        for raw in (
            "https://shop.example.com/item/1001?utm_source=feed",
            "https://shop.example.com/item/1001#reviews",
            "https://shop.example.com/item/1001/",
            "HTTPS://Shop.Example.COM/a/../item/1001",
        ):
            self.assertEqual(normalize_url(raw), expected, raw)

    def test_scheme_less_url_gets_default_scheme(self) -> None:
        # 省略 scheme 的失败URL无唯一答案，统一补 http 后再参与去重。
        self.assertEqual(
            normalize_url("shop.example.com/item/1001"),
            "http://shop.example.com/item/1001",
        )

    def test_query_order_is_stable(self) -> None:
        self.assertEqual(
            normalize_url("https://a.example.com/p?b=2&a=1"),
            normalize_url("https://a.example.com/p?a=1&b=2"),
        )

    def test_strategy_can_keep_query_and_slash(self) -> None:
        raw = "https://a.example.com/p/?utm_source=x"
        self.assertEqual(normalize_url(raw, "保留查询参数、保留结尾斜杠"), raw)

    def test_reason_priority(self) -> None:
        self.assertEqual(primary_reason("HTTP_503", "TIMEOUT"), "TIMEOUT")
        self.assertEqual(merge_reasons(["HTTP_404"], "ROBOTS_DENIED"), ["ROBOTS_DENIED", "HTTP_404"])


class RetryDedupTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()

    def test_same_url_same_version_only_one_entry(self) -> None:
        service = RetryqueueService()
        first, _, _, _ = service.create_entry({
            "作业编号": "CRAW-0002",
            "失败URL": "https://shop.example.com/item/1001?utm_source=a",
            "失败原因码": "HTTP_503",
        })
        second, _, message, _ = service.create_entry({
            "作业编号": "CRAW-0002",
            "失败URL": "https://shop.example.com/item/1001/",
            "失败原因码": "TIMEOUT",
        })
        self.assertEqual(first["id"], second["id"])
        self.assertIn("已存在", message)
        self.assertEqual(second["失败原因码"], "TIMEOUT")
        self.assertIn("HTTP_503", second["失败原因码列表"])
        rows = [row for row in store.rows("retryqueue")
                if row.get("规范化URL") == "https://shop.example.com/item/1001"
                and str(row.get("作业版本")) == "2"]
        self.assertEqual(len(rows), 1)

    def test_concurrent_registration_creates_one_entry(self) -> None:
        service = RetryqueueService()
        forms = [
            "https://shop.example.com/item/8001?utm_source=a",
            "https://shop.example.com/item/8001/",
            "https://shop.example.com/item/8001#x",
        ] * 4

        def register(url: str) -> None:
            service.create_entry({
                "作业编号": "CRAW-0002", "失败URL": url, "失败原因码": "TIMEOUT",
            })

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(register, forms))
        rows = [row for row in store.rows("retryqueue")
                if row.get("规范化URL") == "https://shop.example.com/item/8001"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["重复登记次数"], len(forms) - 1)

    def test_reconnect_sync_idempotent(self) -> None:
        service = RetryqueueService()
        payload = {
            "作业编号": "CRAW-0002",
            "幂等键": "sync-1",
            "失败页面列表": [
                {"url": "https://shop.example.com/item/9001?utm_source=x", "reason": "TIMEOUT"},
                {"url": "https://shop.example.com/item/9001", "reason": "TIMEOUT"},
            ],
        }
        first, _, _, _ = service.register_failures(dict(payload))
        second, _, message, idempotent = service.register_failures(dict(payload))
        self.assertEqual(len(first), 1)
        self.assertTrue(idempotent)
        self.assertIn("幂等键", message)
        self.assertEqual(len(second), 1)
        rows = [row for row in store.rows("retryqueue")
                if row.get("规范化URL") == "https://shop.example.com/item/9001"]
        self.assertEqual(len(rows), 1)

    def test_out_of_job_url_rejected(self) -> None:
        service = RetryqueueService()
        entry, _, message, _ = service.create_entry({
            "作业编号": "CRAW-0002",
            "失败URL": "https://evil.example.org/x",
            "失败原因码": "TIMEOUT",
        })
        self.assertIsNone(entry)
        self.assertIn("起始URL", message)

    def test_final_failure_requires_backoff_limit(self) -> None:
        service = RetryqueueService()
        entry, _ = service.run_action(1, "标记最终失败")
        self.assertIsNone(entry)


class ProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()

    def test_concurrent_results_single_progress_row(self) -> None:
        service = TaskexecService()

        def write(index: int) -> None:
            service.write_retry_result({
                "作业编号": "CRAW-0003",
                "作业版本": "5",
                "失败URL": f"https://blog.example.com/post/{index % 7}",
                "结果": "失败",
                "失败原因码": "TIMEOUT",
            })

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(20)))
        rows = [row for row in service.progress_rows()
                if row.get("作业编号") == "CRAW-0003" and str(row.get("作业版本")) == "5"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["总URL数"], len(rows[0]["唯一URL集合"]))
        audit = service.audit("CRAW-0003", "5")
        self.assertTrue(audit["核对"]["计数与URL集合一致"])

    def test_late_result_after_job_finished(self) -> None:
        service = TaskexecService()
        job = next(row for row in store.rows("crawljob") if row.get("作业编号") == "CRAW-0002")
        job["status"] = "已完成"
        result, message, extra = service.write_retry_result({
            "作业编号": "CRAW-0002",
            "失败URL": "https://shop.example.com/item/4242",
            "结果": "失败",
            "失败原因码": "TIMEOUT",
        })
        self.assertIsNotNone(result)
        self.assertTrue(extra["迟到"])
        self.assertIn("作业状态保持不变", message)
        self.assertEqual(job["status"], "已完成")
        progress = service.find_progress("CRAW-0002", job["作业版本"])
        self.assertEqual(progress["进度状态"], "已完成")
        self.assertEqual(progress["迟到结果数"], 1)

    def test_cross_version_conflict_keeps_two_bases(self) -> None:
        service = TaskexecService()
        service.write_retry_result({
            "作业编号": "CRAW-0002", "作业版本": "1",
            "失败URL": "https://shop.example.com/item/6000?utm_source=a",
            "结果": "失败", "失败原因码": "TIMEOUT",
        })
        service.write_retry_result({
            "作业编号": "CRAW-0002", "作业版本": "2",
            "失败URL": "https://shop.example.com/item/6000/",
            "结果": "失败", "失败原因码": "HTTP_404",
        })
        audit = service.audit("CRAW-0002", "1")
        self.assertTrue(audit["冲突标记"])
        bases = {(base["作业版本"], base["失败原因码"]) for base in audit["版本依据"]}
        self.assertIn(("1", "TIMEOUT"), bases)
        self.assertIn(("2", "HTTP_404"), bases)
        self.assertEqual(audit["主原因码"], "TIMEOUT")
        self.assertEqual(audit["冲突原因优先级"], ["TIMEOUT", "HTTP_404"])

    def test_completed_page_rewrite_ignored(self) -> None:
        service = TaskexecService()
        service.write_retry_result({
            "作业编号": "CRAW-0002",
            "失败URL": "https://shop.example.com/item/1002",
            "结果": "成功",
        })
        result, message, _ = service.write_retry_result({
            "作业编号": "CRAW-0002",
            "失败URL": "https://shop.example.com/item/1002/",
            "结果": "成功",
        })
        self.assertEqual(result["结果"], "忽略")
        self.assertIn("不会重新抓取", message)
        audit = service.audit("CRAW-0002", "2")
        statuses = {row["规范化URL"]: row["结果状态"] for row in audit["明细"]}
        self.assertEqual(statuses["https://shop.example.com/item/1002"], "已完成")

    def test_failed_result_keeps_queue_and_progress_aligned(self) -> None:
        service = TaskexecService()
        service.write_retry_result({
            "作业编号": "CRAW-0003",
            "失败URL": "https://blog.example.com/post/777",
            "结果": "失败",
            "失败原因码": "HTTP_503",
        })
        reconcile = service.reconcile()
        for item in reconcile["items"]:
            self.assertTrue(item["两侧一致"], item)
            self.assertEqual(item["未收敛条目数"], item["未收敛URL数"])
        self.assertTrue(reconcile["全部一致"])

    def test_result_idempotency(self) -> None:
        service = TaskexecService()
        payload = {
            "作业编号": "CRAW-0003",
            "失败URL": "https://blog.example.com/post/888",
            "结果": "失败",
            "幂等键": "res-idem-1",
        }
        service.write_retry_result(dict(payload))
        total_before = len(service.result_rows())
        _, message, extra = service.write_retry_result(dict(payload))
        self.assertTrue(extra["幂等"])
        self.assertIn("幂等键", message)
        self.assertEqual(len(service.result_rows()), total_before)

    def test_datasets_share_url_set(self) -> None:
        service = TaskexecService()
        progress = service.progress_dataset("CRAW-0003")
        results = service.retry_result_dataset("CRAW-0003")
        self.assertEqual(progress["total"], results["total"])
        for left, right in zip(progress["items"], results["items"]):
            self.assertEqual(left["唯一URL集合"], right["URL集合"])
            self.assertEqual(left["总URL数"], right["重试结果条数"])
            self.assertTrue(left["计数核对"]["计数与URL集合一致"])


@unittest.skipIf(TestClient is None, "需要 httpx 才能运行接口层用例")
class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.client = TestClient(app)

    def test_queue_list_accepts_reason_and_url_filters(self) -> None:
        payload = self.client.get(
            "/api/retryqueue", params={"reason": "TIMEOUT", "job": "CRAW-0002"}
        ).json()
        self.assertEqual(payload["total"], 1)
        located = self.client.get(
            "/api/retryqueue",
            params={"url": "https://shop.example.com/item/1001?utm_source=any"},
        ).json()
        self.assertEqual(located["total"], 1)
        self.assertEqual(located["items"][0]["条目编号"], "RETR-0001")

    def test_manifest_reaches_progress(self) -> None:
        manifest = self.client.get("/api/taskexec/manifest", params={"size": 100}).json()
        self.assertGreater(manifest["total"], 0)
        for row in manifest["items"]:
            code = row["进度编号"]
            self.assertTrue(code)
            progress = self.client.get(f"/api/taskexec/progress/by-code/{code}").json()
            self.assertEqual(progress["作业编号"], row["作业编号"])

    def test_action_payload_shapes(self) -> None:
        entry = self.client.get("/api/retryqueue", params={"scope": "unconverged"}).json()["items"][0]
        flat = self.client.post(
            f"/api/retryqueue/{entry['id']}/actions", json={"action": "发起重试"}
        ).json()
        self.assertTrue(flat["ok"], flat)


if __name__ == "__main__":
    unittest.main()
