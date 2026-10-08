"""只读流程页的 HTTP、更新与证据边界；全部为临时合成记录，无 AI 调用。"""
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mentis.run_view import RunView, make_server


class RunViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "run"
        (self.run / "trace").mkdir(parents=True)
        (self.run / "versions").mkdir()
        self.artifact = self.run / "versions" / "index.html"
        self.artifact.write_text("<!doctype html><h1>离线合成网页</h1>", encoding="utf-8")
        self.observation = self.run / "observed.html"
        self.observation.write_text("bound-original", encoding="utf-8")
        state = {"schema": "mentis-eval-state/1", "meta": {"input": {"user_goal": "离线夹具目标 <script>alert('unsafe')</script>"}},
                 "summary": {"verdict": "no_change", "verdict_basis": "质量未验证", "candidates": []},
                 "model_requests": []}
        (self.run / "runner-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.event = {"schema_version": "mentis-trace/1", "run_id": "run", "seq": 1,
                      "time_utc": "2026-10-08T00:00:00+00:00", "source": "observer",
                      "kind": "observation.structure", "status": "ok", "summary": "合成观察",
                      "refs": [{"path": str(self.observation), "role": "observation",
                                "sha256": hashlib.sha256(self.observation.read_bytes()).hexdigest()}]}
        (self.run / "trace" / "events.jsonl").write_text(json.dumps(self.event) + "\n", encoding="utf-8")
        self.server = make_server(self.run, 0)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path="/", method="GET", headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=3)

    def test_real_http_page_and_public_api_are_read_only(self):
        before = {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        with self.request() as response:
            page = response.read().decode("utf-8")
            self.assertIn("专业决策", page)
            self.assertIn("执行时间线", page)
            self.assertIn("保留作品", page)
            # O2：历史运行缺活动信号——页面不得把读取/刷新当作生产者活跃
            self.assertIn("活动信号未采集", page)
            self.assertIn("不能从事件静默判断任务是否仍在运行", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>alert('unsafe')</script>", page)
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        with self.request("/api/view") as response:
            snapshot = json.load(response)
            self.assertEqual(len(snapshot["fingerprint"]), 64)
            self.assertIn("合成观察", snapshot["html"])
        for route in ("/viewer.js", "/viewer.css"):
            with self.request(route) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()})

    def test_appended_record_changes_snapshot_without_inventing_liveness(self):
        with self.request("/api/view") as response:
            before = json.load(response)
        event = dict(self.event, seq=2, source="mentis", kind="review.recorded", summary="新合成评审")
        with (self.run / "trace" / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        with self.request("/api/view") as response:
            after = json.load(response)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertIn("新合成评审", after["html"])
        self.assertIn("不能从事件静默判断任务是否仍在运行", after["html"])
        self.assertIn("当前状态", after["html"])  # O2 状态面板（阶段/等待/活动）随记录刷新

    def test_outside_and_unlisted_files_cannot_be_read(self):
        secret = Path(self.temp.name) / "outside.txt"
        secret.write_text("outside-secret-marker", encoding="utf-8")
        event = dict(self.event, refs=[{"path": str(secret), "sha256": hashlib.sha256(secret.read_bytes()).hexdigest(), "role": "observation"}])
        (self.run / "trace" / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.request() as response:
            self.assertNotIn("outside-secret-marker", response.read().decode("utf-8"))
        for route in ("/evidence/unknown", "/../outside.txt", "/runner-state.json"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

    def test_mutations_and_foreign_host_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(method=method)
            self.assertEqual(error.exception.code, 405)
            error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(headers={"Host": "example.invalid"})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_html_evidence_is_sandboxed(self):
        _, files = RunView(self.run).snapshot()
        token = next(key for key, row in files.items() if row["path"] == self.artifact)
        with self.request("/evidence/" + token) as response:
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.read(), self.artifact.read_bytes())

    def test_changed_bound_evidence_is_not_served(self):
        _, files = RunView(self.run).snapshot()
        token = next(key for key, row in files.items() if row["path"] == self.observation)
        self.observation.write_text("changed-content", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/evidence/" + token)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_version_comparison_keeps_no_change_and_shows_actual_diff(self):
        initial = self.run / "versions" / "initial.html"
        final = self.run / "versions" / "final-001.html"
        initial.write_text("before-line\n", encoding="utf-8")
        final.write_text("before-line\n", encoding="utf-8")
        with self.request() as response:
            same = response.read().decode("utf-8")
        self.assertIn("本次未修改作品", same)
        self.assertIn("没有文件差异", same)
        final.write_text("after-line\n", encoding="utf-8")
        with self.request() as response:
            changed = response.read().decode("utf-8")
        self.assertIn("-before-line", changed)
        self.assertIn("+after-line", changed)
        self.assertIn("保留作品", changed)  # 原判定从记录读取，文件差异不改判。

    def test_missing_trace_is_not_a_proven_zero_delegation(self):
        (self.run / "trace" / "events.jsonl").unlink()
        with self.request() as response:
            page = response.read().decode("utf-8")
        self.assertIn("事件未采集", page)
        self.assertNotIn("这份记录中没有执行器委托", page)
        self.assertIn("保留作品", page)


if __name__ == "__main__":
    unittest.main()
