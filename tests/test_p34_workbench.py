"""P3.4 回归例：本地工作台的编排与投影（全部离线，Fake 子进程）。

真实浏览器会话的端到端核验见 runtime/observability/p34/；本文件只固化
服务端可离线验证的行为：发起不重复（单构建守卫）、逐运行动作串行、
投影计算（待决/可核对/可撤回）、证据文件白名单与路径穿越拒绝。
"""

import json
import threading
import unittest
import urllib.request
from pathlib import Path

from mentis.workbench import Workbench, make_server


class FakeProc:
    """可编程子进程替身：保持 Popen 的 poll/returncode/pid 表面。"""

    def __init__(self, alive=True):
        self.pid = 4321
        self.returncode = None if alive else 0

    def poll(self):
        return self.returncode


class Harness(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs = self.root / "runtime" / "collab"
        self.runs.mkdir(parents=True)
        self.procs = []

        def spawner(args, cwd, env, stdout):
            proc = FakeProc(alive=True)
            self.procs.append({"args": args, "proc": proc})
            return proc

        self.bench = Workbench(self.runs, spawner=spawner, start_wait_s=1)

    def write_heartbeat(self, run_id="20260101_000000-ab", fresh=True):
        from datetime import datetime, timezone, timedelta
        d = self.runs / run_id / "trace"
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts = now if fresh else now - timedelta(seconds=300)
        (d / "heartbeat.json").write_text(json.dumps({
            "schema": "mentis-heartbeat/1", "run_id": run_id,
            "time_utc": ts.isoformat(timespec="seconds"), "ttl_s": 45.0,
        }, ensure_ascii=False), encoding="utf-8")

    def make_run(self, run_id="20260101_000000-ab", status="DELIVERED",
                 pending=True, unknown_attempt=False, active_auth=True,
                 with_versions=False, with_trace=False):
        d = self.runs / run_id
        (d / "workspace").mkdir(parents=True)
        (d / "workspace" / "index.html").write_text("<html><body>页</body></html>",
                                                    encoding="utf-8")
        session = {"interactions": [
            {"interaction_id": "ix-1", "kind": "notify_progress", "message": "进展"},
        ], "attempts": [
            {"attempt_id": "attempt-1", "action_key": "build-page", "status": "completed",
             "accepted": True, "brief_version": 1},
        ], "authorizations": [
            {"auth_id": "auth-1", "scope": "exec-workspace",
             "status": "active" if active_auth else "revoked"},
        ]}
        if pending:
            session["interactions"].append(
                {"interaction_id": "ix-9", "kind": "request_decision",
                 "message": "两处措辞是否统一？", "blocking_action": "review-fix",
                 "answer_source": None, "brief_version": 1})
        if unknown_attempt:
            session["attempts"].append(
                {"attempt_id": "attempt-2", "action_key": "repair-1", "status": "unknown",
                 "accepted": False, "brief_version": 1})
        (d / "session.json").write_text(json.dumps(session, ensure_ascii=False),
                                        encoding="utf-8")
        meta = {"state_version": 3, "run_id": run_id, "mode": "autonomous",
                "status": status, "goal": "测试目标", "delivered": status == "DELIVERED",
                "repairs_used": 0, "max_repairs": 1, "thread_id": "",
                "model_usage": {"real_calls": 1, "mock_calls": 0},
                "screenshots": [], "codex_runs": []}
        (d / "state.json").write_text(json.dumps(meta, ensure_ascii=False),
                                      encoding="utf-8")
        if with_versions:
            (d / "versions").mkdir()
            (d / "versions" / "index-attempt-1.html").write_text(
                "<html>旧版</html>", encoding="utf-8")
        if with_trace:
            tr = d / "trace"
            tr.mkdir(exist_ok=True)
            (tr / "events.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
            (tr / "model-calls.jsonl").write_text('{"ok": true}\n', encoding="utf-8")
        return d


class StartGuardTests(Harness):
    def test_second_start_refused_while_build_running(self):
        first = self.bench.start_task("第一个目标")
        self.assertTrue(first.get("starting") or first.get("run_id"))
        second = self.bench.start_task("第二个目标")
        self.assertEqual(second.get("code"), 409)  # 单用户：进行中不允许再发起
        self.assertEqual(len(self.procs), 1)
        # 构建结束后可以再次发起
        self.procs[0]["proc"].returncode = 0
        third = self.bench.start_task("第三个目标")
        self.assertIn(third.get("run_id") or third.get("starting"), (None, True))
        self.assertEqual(len(self.procs), 2)

    def test_start_refuses_empty_goal(self):
        self.assertEqual(self.bench.start_task("   ").get("code"), 400)
        self.assertEqual(self.procs, [])


class ActionTests(Harness):
    def test_actions_serialize_per_run(self):
        self.make_run()
        first = self.bench.run_action("20260101_000000-ab", "feedback",
                                      {"text": "改一下"})
        self.assertTrue(first.get("accepted"))
        second = self.bench.run_action("20260101_000000-ab", "revoke", {})
        self.assertEqual(second.get("code"), 409)  # 上一个动作未结束
        self.procs[0]["proc"].returncode = 0
        third = self.bench.run_action("20260101_000000-ab", "revoke", {})
        self.assertTrue(third.get("accepted"))
        self.procs[1]["proc"].returncode = 0
        self.bench._action_view("20260101_000000-ab")  # 收尾：关闭日志句柄

    def test_action_args_map_to_real_cli(self):
        self.make_run()
        run_id = "20260101_000000-ab"
        self.bench.run_action(run_id, "answer",
                              {"interaction_id": "ix-9", "text": "统一措辞"})
        args = self.procs[0]["args"]
        self.assertIn("collab-resume", args)
        self.assertIn("--answer", args)
        self.assertIn("ix-9:统一措辞", args)
        self.assertIn("--non-interactive", args)
        self.procs[0]["proc"].returncode = 0
        self.bench.run_action(run_id, "goal", {"text": "新目标"})
        self.assertIn("--goal-new", self.procs[1]["args"])
        self.procs[1]["proc"].returncode = 0
        self.bench.run_action(run_id, "confirm", {"outcome": "completed"})
        self.assertEqual(self.procs[2]["args"][self.procs[2]["args"].index(
            "--confirm-outcome") + 1], "completed")
        self.procs[2]["proc"].returncode = 0
        self.bench.run_action(run_id, "revoke", {})
        self.assertIn("--revoke", self.procs[3]["args"])
        # 触发收尾：日志句柄关闭（Windows 文件占用）
        self.procs[3]["proc"].returncode = 0
        self.bench._action_view(run_id)

    def test_action_refuses_invalid_input_and_missing_run(self):
        self.assertEqual(self.bench.run_action("不存在", "revoke", {}).get("code"), 404)
        self.make_run()
        self.assertEqual(self.bench.run_action("20260101_000000-ab", "answer",
                                               {"interaction_id": "ix-9"}).get("code"), 400)
        self.assertEqual(self.bench.run_action("20260101_000000-ab", "confirm",
                                               {"outcome": "maybe"}).get("code"), 400)
        self.assertEqual(self.bench.run_action("20260101_000000-ab", "dance", {}).get("code"), 400)
        self.assertEqual(self.procs, [])


class ProjectionTests(Harness):
    def test_detail_computes_available_actions(self):
        self.make_run(status="UNKNOWN", pending=True, unknown_attempt=True)
        detail = self.bench.task_detail("20260101_000000-ab")
        self.assertTrue(detail["can_answer"])
        self.assertTrue(detail["can_confirm"])
        self.assertTrue(detail["can_revoke"])
        self.assertEqual([p["interaction_id"] for p in detail["pending"]], ["ix-9"])
        self.assertTrue(detail["artifact"]["exists"])
        self.assertEqual(detail["attempts"][1]["status"], "unknown")

    def test_detail_flags_after_revoke(self):
        self.make_run(status="WITHDRAWN", pending=False, active_auth=False)
        detail = self.bench.task_detail("20260101_000000-ab")
        self.assertFalse(detail["can_revoke"])
        self.assertFalse(detail["can_answer"])

    def test_list_excludes_dirs_without_state(self):
        self.make_run()
        (self.runs / "20260101_000001-cd").mkdir()
        rows = self.bench.list_tasks()
        self.assertEqual([r["run_id"] for r in rows], ["20260101_000000-ab"])


class ServerTests(Harness):
    def setUp(self):
        super().setUp()
        self.make_run()

    def _get(self, path, host="127.0.0.1"):
        server = make_server(self.runs, 0)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        req.add_header("Host", f"{host}:{port}")
        opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            return exc.code, body

    def test_pages_and_api_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("工作台", body.decode("utf-8"))
        status, body = self._get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["run_id"], "20260101_000000-ab")
        status, body = self._get("/api/task/20260101_000000-ab")
        detail = json.loads(body)
        self.assertEqual(detail["goal"], "测试目标")
        status, _ = self._get("/preview/20260101_000000-ab/index.html")
        self.assertEqual(status, 200)
        status, _ = self._get("/api/task/missing")
        self.assertEqual(status, 404)

    def test_preview_and_evidence_paths_are_jailed(self):
        # 路径穿越拒绝
        status, _ = self._get("/preview/20260101_000000-ab/..%2Fstate.json")
        self.assertIn(status, (404, 503))
        status, _ = self._get("/preview/20260101_000000-ab/../state.json")
        self.assertIn(status, (404, 503))
        # 证据白名单：session.json 可以，workspace 产物与任意文件不行
        status, _ = self._get("/evidence/20260101_000000-ab/session.json")
        self.assertEqual(status, 200)
        status, _ = self._get("/evidence/20260101_000000-ab/workspace/index.html")
        self.assertEqual(status, 404)
        status, _ = self._get("/evidence/20260101_000000-ab/nope.md")
        self.assertEqual(status, 404)

    def test_foreign_host_rejected(self):
        status, _ = self._get("/", host="evil.example.com")
        self.assertEqual(status, 403)


class RouteSubpathTests(Harness):
    """P3.4 验收修复（审计 #2）：versions/ 与 trace/ 白名单子路径可达，
    路径隔离与穿越拒绝保持。"""

    def setUp(self):
        super().setUp()
        self.make_run(with_versions=True, with_trace=True)

    def _get(self, path):
        server = make_server(self.runs, 0, bench=self.bench)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        req.add_header("Host", f"127.0.0.1:{port}")
        try:
            with urllib.request.build_opener().open(req, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            return exc.code, body

    def test_version_snapshot_preview_served(self):
        status, body = self._get("/preview/20260101_000000-ab/versions/index-attempt-1.html")
        self.assertEqual(status, 200)
        self.assertIn("旧版", body.decode("utf-8"))

    def test_trace_evidence_routes_served(self):
        status, body = self._get("/evidence/20260101_000000-ab/trace/events.jsonl")
        self.assertEqual(status, 200)
        self.assertIn('"seq": 1', body.decode("utf-8"))
        status, body = self._get("/evidence/20260101_000000-ab/trace/model-calls.jsonl")
        self.assertEqual(status, 200)
        status, _ = self._get("/evidence/20260101_000000-ab/trace/stages.jsonl")
        self.assertEqual(status, 404)  # 白名单内但文件不存在 → 仍 404

    def test_isolation_kept(self):
        # versions 之外的两段子路径不可达（trace/ 通过 preview 不可达）
        status, _ = self._get("/preview/20260101_000000-ab/versions/../state.json")
        self.assertIn(status, (404, 503))
        status, _ = self._get("/preview/20260101_000000-ab/trace/events.jsonl")
        self.assertEqual(status, 404)
        status, _ = self._get("/evidence/20260101_000000-ab/workspace/index.html")
        self.assertEqual(status, 404)
        status, _ = self._get("/evidence/20260101_000000-ab/trace/nope.jsonl")
        self.assertEqual(status, 404)
        status, _ = self._get("/evidence/20260101_000000-ab/session.jsonx")
        self.assertEqual(status, 404)
        status, _ = self._get("/preview/20260101_000000-ab/versions/x/../../state.json")
        self.assertIn(status, (404, 503))


class WriteGuardTests(Harness):
    """P3.4 验收修复（审计 #4）：写接口校验请求来源与媒体类型。"""

    def setUp(self):
        super().setUp()
        self.make_run()
        server = make_server(self.runs, 0, bench=self.bench)
        self.port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def _post(self, path, payload, content_type="application/json", origin=None):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=body, method="POST")
        req.add_header("Host", f"127.0.0.1:{self.port}")
        if content_type:
            req.add_header("Content-Type", content_type)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.build_opener().open(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, {}

    def test_foreign_origin_rejected(self):
        status, body = self._post("/api/start", {"goal": "x"},
                                  origin="http://evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.procs, [])

    def test_non_json_media_rejected(self):
        status, body = self._post("/api/start", {"goal": "x"},
                                  content_type="text/plain")
        self.assertEqual(status, 415)
        status, body = self._post("/api/start", {"goal": "x"}, content_type="")
        self.assertEqual(status, 415)
        self.assertEqual(self.procs, [])

    def test_local_json_request_accepted(self):
        status, body = self._post("/api/start", {"goal": "x"},
                                  origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.procs), 1)
        status, _ = self._post("/api/start", {"goal": "y"})  # 无 Origin 的本机工具调用放行
        # 第二个发起被单构建守卫拒绝（409），但守卫本身放行了请求
        self.assertEqual(status, 409)


class InFlightAndStopTests(Harness):
    """P3.4 验收修复（审计 #3）：在途识别跨重启、动作串行、
    授权撤回 / 停止请求 / 已确认停止三状态分开。"""

    def test_fresh_activity_refuses_actions(self):
        self.make_run(status="UNKNOWN", unknown_attempt=True)
        self.write_heartbeat(fresh=True)
        result = self.bench.run_action("20260101_000000-ab", "revoke", {})
        self.assertEqual(result.get("code"), 409)
        self.assertIn("请求停止", result.get("error", ""))
        self.assertEqual(self.procs, [])

    def test_restart_identifies_in_flight_without_registry(self):
        """服务重启（注册表丢失）后仍能识别在途任务并拒绝动作。"""
        self.make_run(status="UNKNOWN", unknown_attempt=True)
        self.write_heartbeat(fresh=True)
        fresh_bench = Workbench(self.runs, spawner=lambda *a, **k: None,
                                start_wait_s=1)
        result = fresh_bench.run_action("20260101_000000-ab", "feedback",
                                        {"text": "改"})
        self.assertEqual(result.get("code"), 409)

    def test_stale_activity_allows_actions(self):
        self.make_run(status="UNKNOWN", unknown_attempt=True)
        self.write_heartbeat(fresh=False)
        result = self.bench.run_action("20260101_000000-ab", "revoke", {})
        self.assertTrue(result.get("accepted"))
        self.procs[0]["proc"].returncode = 0
        self.bench._action_view("20260101_000000-ab")

    def test_stop_request_marks_and_recovers(self):
        self.make_run(status="UNKNOWN", unknown_attempt=True)
        self.write_heartbeat(fresh=True)
        result = self.bench.request_stop("20260101_000000-ab")
        self.assertTrue(result.get("stop_requested"))
        self.assertTrue(self.bench._stop_marker("20260101_000000-ab").is_file())
        detail = self.bench.task_detail("20260101_000000-ab")
        self.assertTrue(detail["in_flight"])
        self.assertTrue(detail["stop_requested"])
        # 标记目录不写进运行目录（单写入不破例）
        self.assertFalse((self.runs / "20260101_000000-ab" / "workbench").exists())
        # 任务结束后（心跳过期）：动作恢复可用；停止请求应清除语义
        self.write_heartbeat(fresh=False)
        detail = self.bench.task_detail("20260101_000000-ab")
        self.assertFalse(detail["in_flight"])
        self.assertTrue(detail["stop_requested"])  # 历史仍可见，直到撤回确认

    def test_stop_request_refused_when_idle(self):
        self.make_run()
        result = self.bench.request_stop("20260101_000000-ab")
        self.assertEqual(result.get("code"), 409)
        self.assertFalse(self.bench._stop_marker("20260101_000000-ab").is_file())

    def test_three_states_separated_in_detail(self):
        # 已确认停止：collab 自身 WITHDRAWN + 授权全部撤回
        self.make_run(status="WITHDRAWN", pending=False, active_auth=False)
        detail = self.bench.task_detail("20260101_000000-ab")
        self.assertTrue(detail["confirmed_stop"])
        self.assertTrue(detail["authorizations_revoked"])
        self.assertFalse(detail["can_revoke"])
        self.assertFalse(detail["in_flight"])
        # 进行中 + 授权仍有效：in_flight True、撤回不可用、停止请求可发
        self.make_run(run_id="20260101_000002-ef", status="UNKNOWN",
                      unknown_attempt=True, active_auth=True)
        self.write_heartbeat("20260101_000002-ef", fresh=True)
        detail = self.bench.task_detail("20260101_000002-ef")
        self.assertTrue(detail["in_flight"])
        self.assertTrue(detail["can_confirm"])
        self.assertFalse(detail["can_revoke"])
        self.assertFalse(detail["confirmed_stop"])
        self.assertFalse(detail["stop_requested"])


if __name__ == "__main__":
    unittest.main()
