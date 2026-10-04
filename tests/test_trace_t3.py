"""P2.4-T3 回归例：故障与隐私加固（全部离线；计划 §4/T3 夹具矩阵）。

覆盖：敏感输出隐去（事件/回放/导出；prompt 归档除外）、追踪写失败不粉饰、
执行器流截断/限额不判成功、续接多 attempt 连续时间线、撤回落trace、
现场会话异常收尾（collab / guidance / field）、导出白名单与旧记录兼容导出。
损坏 JSONL、无终态与旁观器退出由 T1/T2 既有夹具覆盖（test_trace.py / test_trace_stream.py）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from mentis.contracts import AttemptStatus
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec, CodexTaskResult, ExecutorBusy
from mentis.trace import TraceWriter, load_events, redact_text, render_timeline
from mentis.trace_export import export_timeline

try:
    from tests.test_collab import FakeAdapter, make_collab, stub_capture
    from tests.test_trace import RecordingAdapter
    from tests.test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                                     DIAGNOSIS_OVERFLOW, FakeGateway,
                                     file_aware_measure, make_baseline)
    from tests.test_trace_stream import ScriptedStream
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import FakeAdapter, make_collab, stub_capture
    from test_trace import RecordingAdapter
    from test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                               DIAGNOSIS_OVERFLOW, FakeGateway,
                               file_aware_measure, make_baseline)
    from test_trace_stream import ScriptedStream


def _git_root(base: Path) -> Path:
    root = base / "ws"
    root.mkdir(parents=True)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return root


class RedactionTests(unittest.TestCase):
    """敏感输出夹具：已知字段名确定性隐去；prompt 归档保持发送原文（哈希一致）。"""

    SECRET_SAMPLES = (
        "配置 DEEPSEEK_API_KEY=sk-abc123def456 已读取",
        '请求头 "api_key": "sk-secret999" 已带上',
        "Authorization: Bearer eyJaabc123456789 已发送",
        "数据库密码：hunter2pass 已填入",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_redact_text_known_fields_only(self):
        for sample in self.SECRET_SAMPLES:
            out = redact_text(sample)
            self.assertIn("⟨已隐去⟩", out)
            for secret in ("sk-abc123def456", "sk-secret999", "eyJJaabc123456789",
                           "hunter2pass"):
                self.assertNotIn(secret, out, sample)
            self.assertNotIn("sk-", out.replace("⟨已隐去⟩", ""))  # 值不残留（字段名保留）
        # 非敏感赋值不受影响；token 用量计数不是密钥，不隐去
        self.assertEqual(redact_text("用量 {'input_tokens': 69259}"),
                         "用量 {'input_tokens': 69259}")

    def test_events_redacted_but_prompt_archive_keeps_sent_text(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        for sample in self.SECRET_SAMPLES:
            w.emit("executor", "executor.item", sample, status="pending")
        for e in load_events(run_dir):
            self.assertNotIn("sk-secret999", e["summary"])
            self.assertNotIn("hunter2pass", e["summary"])
        ref = w.archive_prompt("attempt-1", "确切指令（api_key=sk-plain123456 不脱敏）")
        self.assertIn("sk-plain123456", Path(ref["path"]).read_text(encoding="utf-8"))
        import hashlib

        self.assertEqual(ref["sha256"],
                         hashlib.sha256("确切指令（api_key=sk-plain123456 不脱敏）".encode()).hexdigest())

    def test_timeline_and_export_contain_no_secrets(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        w.emit("executor", "executor.item", self.SECRET_SAMPLES[1], status="pending")
        w.finalize(outcome="X")
        timeline = render_timeline(run_dir)
        self.assertNotIn("sk-secret999", timeline)
        out_path = self.root / "export.json"
        export_timeline(run_dir, out_path)
        blob = out_path.read_text(encoding="utf-8")
        self.assertNotIn("sk-secret999", blob)


class WriteFailureTests(unittest.TestCase):
    """追踪写失败夹具：委托期间失败不停止执行；完整性如实降级，不显示「已完整追踪」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_midrun_write_failure_marks_integrity_and_replay_fails_audit(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        w.emit("mentis", "goal.recorded", "目标")
        w.events_path = w.prompts_dir  # 注入：事件文件路径指向目录 → 之后每次写都失败
        w.emit("executor", "executor.item", "这条会写失败", status="pending")
        manifest = w.finalize(outcome="X")
        self.assertFalse(manifest["integrity"]["events_complete"])
        self.assertGreaterEqual(manifest["integrity"]["write_failures"], 1)
        self.assertIn("当前核验：**未通过**", render_timeline(run_dir))

    def test_delegation_continues_and_executor_status_kept_when_trace_fails(self):
        collab = make_collab(adapter=RecordingAdapter(behavior={"first": "good"}),
                             base_dir=self.root / "collab")
        collab.trace.events_path = collab.trace.prompts_dir  # 全部事件写失败
        status = collab.run()
        self.assertEqual(status, "DELIVERED")  # 执行器真实状态不被追踪故障改写
        self.assertTrue(collab.adapter.prompts)  # 委托照常发起
        manifest = collab.trace.finalize(outcome=status)
        self.assertFalse(manifest["integrity"]["events_complete"])
        self.assertGreaterEqual(manifest["integrity"]["write_failures"], 1)
        self.assertNotIn("已完整追踪", json.dumps(manifest, ensure_ascii=False))


class ExecutorFaultFixtureTests(unittest.TestCase):
    """截断与限额夹具（Adapter 层）：不出现假完成。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = _git_root(self.base)

    def _adapter(self, stream):
        return CodexCliAdapter(executable="codex", lock_dir=self.base / "locks",
                               cli_version="codex test",
                               stream_factory=lambda *a: stream)

    def _spec(self, **kw):
        defaults = dict(workspace=self.root, prompt="只读核对", brief_version=1,
                        sandbox="read-only", timeout_s=30)
        defaults.update(kw)
        return CodexTaskSpec(**defaults)

    def test_truncated_last_line_is_not_success(self):
        """截断：最后一行 JSON 被截断 → invalid_jsonl，不判成功。"""
        tid = "11111111-1111-1111-1111-111111111111"
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             json.dumps({"type": "turn.started"}),
             '{"type": "turn.compl'],  # 截断行（无换行、不完整）
            returncode=0)
        result = self._adapter(stream).start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.FAILED)
        self.assertIn("invalid_jsonl", [e.kind for e in result.events])

    def test_unknown_without_timeout_returns_normally(self):
        """T4 真实运行发现的 T2 遗留缺陷回归：流式路径判 unknown 后不得因冗余重分类崩溃。"""
        tid = "33333333-3333-3333-3333-333333333333"
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             json.dumps({"type": "turn.started"}),
             json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "做完了"}})],
            returncode=0)  # 干净退出但无 turn.completed → unknown（原代码此处 TypeError）
        result = self._adapter(stream).start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        self.assertIn("缺少 turn.completed", result.error)

    def test_turn_completed_with_failed_subitem_is_unknown(self):
        """终态在场但有失败子项：不判完成（P3.1 纪律），错误消息如实指向失败子项。"""
        tid = "44444444-4444-4444-4444-444444444444"
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             json.dumps({"type": "item.completed",
                         "item": {"type": "command_execution", "status": "failed",
                                  "command": "probe"}}),
             json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "完成（含一次失败探测）"}}),
             json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}})],
            returncode=0)
        result = self._adapter(stream).start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        self.assertIn("失败子项", result.error)

    def test_quota_error_maps_to_failed_and_keeps_message(self):
        """限额：error/turn.failed → FAILED；错误原文（有界）入 result.error 并可入 trace summary。"""
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started",
                         "thread_id": "22222222-2222-2222-2222-222222222222"}),
             json.dumps({"type": "error",
                         "message": "usage limit reached for your account"}),
             json.dumps({"type": "turn.failed"})],
            returncode=1)
        result = self._adapter(stream).start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.FAILED)
        self.assertIn("usage limit reached", result.error)  # 契约 §7：原文有界保留
        # 追踪层：终态 failed 的 summary 带原文（emit 时再经敏感字段规则），供事后回放核对
        run_dir = self.base / "trace-run"
        w = TraceWriter(run_dir, "quota-run")
        w.emit("executor", "executor.completed",
               f"终态 failed；错误 {result.error[:120]}", status="failed",
               executor_status="failed")
        events = load_events(run_dir)
        self.assertEqual(events[-1]["status"], "failed")
        self.assertIn("usage limit", events[-1]["summary"])


class MultiAttemptTimelineTests(unittest.TestCase):
    """续接/恢复/多 attempt 的连续时间线：同一 thread、seq 续排、台账与中断区间。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_feedback_revision_continues_timeline_on_same_thread(self):
        from mentis.collab import collaboration_from_state

        collab = make_collab(adapter=FakeAdapter(), base_dir=self.root / "collab")
        self.assertEqual(collab.run(), "DELIVERED")
        # 复用同一 adapter 实例：恢复不重放历史委托，但执行器行为计数连续；
        # 注入 stub 截图（恢复对象默认真实 Chrome，泄漏过 runtime/collab——已修）
        restored = collaboration_from_state(collab.run_dir / "state.json",
                                            adapter=collab.adapter,
                                            browser_capture=stub_capture)
        self.assertEqual(restored.resume(feedback="首图换成咖啡杯特写"), "DELIVERED")
        manifest = json.loads((collab.run_dir / "trace" / "manifest.json")
                              .read_text(encoding="utf-8"))
        delegations = manifest["delegations"]
        self.assertGreaterEqual(len(delegations), 2)
        threads = {d["native_thread_id"] for d in delegations}
        self.assertEqual(threads, {"t-fake-0000-0000-0000-000000"})  # 续接同一原生 thread
        self.assertTrue(all(d["terminal_status"] == "completed" for d in delegations))
        kinds = [e["kind"] for e in load_events(collab.run_dir)]
        self.assertIn("run.resumed", kinds)  # 恢复段有标记，不与首轮混淆
        self.assertEqual(kinds.count("run.finalized"), 2)
        timeline = render_timeline(collab.run_dir)
        self.assertIn("委托台账", timeline)
        self.assertIn("中断区间", timeline)  # 恢复前的无记录区间如实标注
        self.assertIn("当前核验：**通过**", timeline)  # seq 连续、事件完整

    def test_confirm_outcome_writes_executor_confirmed_event(self):
        """T4：核对确认路径（问题 5）在事件流中可回放——ERROR/unknown → 确认 → 交付。"""
        from mentis.collab import collaboration_from_state

        class UnknownAdapter(FakeAdapter):
            def _run(self, spec, status_override=None):
                if not self.calls:
                    self.calls.append(spec.prompt[:30])
                    return CodexTaskResult(
                        task_id="t", attempt_id="attempt-unknown", brief_version=spec.brief_version,
                        workspace=str(spec.workspace), status=AttemptStatus.UNKNOWN,
                        native_thread_id="", usage={}, artifact_paths=[], cli_version="fake")
                return super()._run(spec, status_override)

        collab = make_collab(adapter=UnknownAdapter(), base_dir=self.root / "collab")
        self.assertEqual(collab.run(), "UNKNOWN")
        restored = collaboration_from_state(collab.run_dir / "state.json",
                                            adapter=collab.adapter,
                                            browser_capture=stub_capture)
        self.assertEqual(restored.resume(confirm_outcome=AttemptStatus.COMPLETED), "DELIVERED")
        kinds = [e["kind"] for e in load_events(collab.run_dir)]
        self.assertIn("executor.confirmed", kinds)
        confirmed = [e for e in load_events(collab.run_dir) if e["kind"] == "executor.confirmed"][0]
        self.assertEqual(confirmed["status"], "ok")
        self.assertIn("无活跃写入者", confirmed["summary"])

    def test_replay_cross_references_session_for_unrecorded_terminal(self):
        """终态未入事件流的委托（如 executor.confirmed 之前的 T4 真实运行）：
        回放交叉引用 session.json 的尝试状态，只读展示，不回填事件。"""
        run_dir = self.root / "cross-run"
        w = TraceWriter(run_dir, "cross-run")
        ref = w.archive_prompt("attempt-1", "指令")
        w.emit("mentis", "delegation.prepared", "委托", attempt_id="attempt-1",
               brief_version=1, refs=[ref])
        w.record_delegation("attempt-1", 1, ref, action="build-page")
        w.finalize(outcome="ERROR（delivered=False）")
        # 人工核对确认只落在 session.json（确认通知在 interactions），事件流无终态
        (run_dir / "session.json").write_text(json.dumps({
            "attempts": [{"attempt_id": "attempt-1", "status": "completed", "accepted": True}],
        }, ensure_ascii=False), encoding="utf-8")
        timeline = render_timeline(run_dir)
        self.assertIn("终态 （未取得）", timeline)
        self.assertIn("session.json 记该尝试 status=completed", timeline)
        self.assertIn("accepted=是", timeline)
        self.assertIn("不回填为事件", timeline)
        self.assertEqual(len(load_events(run_dir)), 1)  # 事件流未被回填

    def test_revoke_is_traced_and_persists(self):
        from mentis.collab import collaboration_from_state

        collab = make_collab(adapter=FakeAdapter(), base_dir=self.root / "collab")
        self.assertEqual(collab.run(), "DELIVERED")
        restored = collaboration_from_state(collab.run_dir / "state.json",
                                            adapter=collab.adapter,
                                            browser_capture=stub_capture)
        self.assertEqual(restored.resume(revoke=True), "WITHDRAWN")
        kinds = [(e["source"], e["kind"], e["status"]) for e in load_events(collab.run_dir)]
        self.assertIn(("user", "authorization.revoked", "failed"), kinds)
        manifest = json.loads((collab.run_dir / "trace" / "manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertTrue(manifest["outcome"].startswith("WITHDRAWN"))
        state = json.loads((collab.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "WITHDRAWN")


class ExceptionFinalizeTests(unittest.TestCase):
    """现场会话异常夹具：异常也收尾——ERROR 落盘、无终态尝试记 unknown、不假完成。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_collab_exception_persists_error_and_unknown_attempts(self):
        class LockedAdapter(FakeAdapter):
            def start_task(self, spec):
                raise ExecutorBusy("工作区已有写入锁（注入）")

        collab = make_collab(adapter=LockedAdapter(), base_dir=self.root / "collab")
        with self.assertRaises(ExecutorBusy):
            collab.run()
        state = json.loads((collab.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "ERROR")
        from mentis.persist import session_from_dict

        session = session_from_dict(json.loads(
            (collab.run_dir / "session.json").read_text(encoding="utf-8")))
        self.assertEqual(session.attempts[-1].status, AttemptStatus.UNKNOWN)  # 不假记失败/完成
        manifest = json.loads((collab.run_dir / "trace" / "manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertTrue(manifest["outcome"].startswith("ERROR"))
        self.assertIn("运行异常终止", render_timeline(collab.run_dir))

    def test_guidance_exception_persists_error(self):
        from mentis.guidance import GuidanceCycle

        try:
            from tests.test_guidance import FakeAdapter as GuidanceFakeAdapter, stub_capture as g_capture
        except ImportError:
            from test_guidance import FakeAdapter as GuidanceFakeAdapter, stub_capture as g_capture

        class ExplodingGuidanceAdapter(GuidanceFakeAdapter):
            def _run(self, spec):
                raise RuntimeError("注入的执行器故障")

        cycle = GuidanceCycle(
            baseline_dir=make_baseline(self.root), run_dir=self.root / "err-run",
            adapter=ExplodingGuidanceAdapter(behavior={"first": "fixed"}),
            gateway=FakeGateway([DIAGNOSIS_OVERFLOW, BLIND_AFTER_X, BLIND_AFTER_Y]),
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        with self.assertRaises(RuntimeError):
            cycle.run()
        state = json.loads((cycle.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "ERROR")
        self.assertIn("运行异常终止", render_timeline(cycle.run_dir))

    def test_field_start_closes_session_on_exception(self):
        """field-start 异常也收尾：SESSION_ERROR、半份记录标不完整、日志入口保留。"""
        from mentis.collab import Collaboration
        from mentis.field import run_field_start

        def factory():
            collab = Collaboration(
                goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）", adapter=FakeAdapter(),
                gateway=None, browser_capture=stub_capture, measure=file_aware_measure, use_model=False,
                interactive=False, base_dir=self.root / "collab")

            def boom():
                raise RuntimeError("注入的工作区故障")
            collab.prepare_workspace = boom
            return collab

        status, session_dir, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）", interactive=False,
            base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=factory)
        self.assertEqual(status, "SESSION_ERROR")
        record = (session_dir / "usage-record.md").read_text(encoding="utf-8")
        self.assertIn("SESSION_ERROR", record)
        self.assertIn("记录不完整", record)
        self.assertIn("session-log.txt", record)  # 日志入口保留
        log = (session_dir / "session-log.txt").read_text(encoding="utf-8")
        self.assertIn("[会话异常]", log)
        self.assertIn("[会话结束]", log)  # 收尾本身完成
        # 协作对象自身也已异常收尾落盘（ERROR），可从 state.json 恢复
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "ERROR")

    def test_field_feedback_error_still_finalizes_session(self):
        """field-feedback 空原话：ValueError 照抛，但会话不留半开日志（异常收尾）。"""
        from mentis.field import run_field_feedback, run_field_start

        try:
            from tests.test_field import make_factory
        except ImportError:
            from test_field import make_factory

        field_base = self.root / "field"
        _status, _s, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）", design=True,
            interactive=False, base_dir=field_base, collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(), design=True,
                                        base_dir=self.root / "collab"))
        before = {p for p in field_base.iterdir() if p.is_dir()}
        with self.assertRaises(ValueError):
            run_field_feedback(run_dir, satisfied=True, quote="   ", base_dir=field_base)
        new_dirs = [p for p in field_base.iterdir() if p.is_dir() and p not in before]
        self.assertEqual(len(new_dirs), 1)  # 反馈会话目录已创建并收尾
        record = (new_dirs[0] / "usage-record.md").read_text(encoding="utf-8")
        self.assertIn("SESSION_ERROR", record)


class ExportWhitelistTests(unittest.TestCase):
    """导出白名单夹具：只出白名单字段与哈希；旧记录导出为历史视图，不回填。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_export_drops_unknown_fields_and_local_paths(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        ref = w.archive_prompt("attempt-1", "指令原文（不出现在导出中）")
        w.emit("mentis", "delegation.prepared", "委托 build-page",
               attempt_id="attempt-1", brief_version=1, refs=[ref])
        w.record_delegation("attempt-1", 1, ref, action="build-page")
        w.record_executor_result("attempt-1", AttemptStatus.COMPLETED,
                                 native_thread_id="t-1", usage={"input_tokens": 5})
        w.finalize(outcome="DELIVERED")
        with (run_dir / "trace" / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({
                "schema_version": "mentis-trace/1", "run_id": "run-x", "seq": 2,
                "time_utc": "2026-09-29T00:00:00.000+00:00", "source": "mentis",
                "kind": "goal.recorded", "attempt_id": None, "brief_version": 1,
                "native_thread_id": None, "status": "ok", "summary": "普通事件",
                "refs": [], "operator_note": "内部备注不应导出"}, ensure_ascii=False) + "\n")
        out_path = self.root / "export.json"
        summary = export_timeline(run_dir, out_path)
        self.assertEqual(summary["mode"], "trace")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        event_kinds = {e["kind"]: e for e in payload["events"]}
        self.assertNotIn("operator_note", event_kinds["goal.recorded"])
        self.assertNotIn("指令原文", out_path.read_text(encoding="utf-8"))  # prompt 原文不导出
        delegation = payload["manifest"]["delegations"][0]
        self.assertIn("prompt_sha256", delegation)
        self.assertNotIn("prompt_file", delegation)  # 本地路径不导出
        self.assertEqual(delegation["native_thread_id"], "t-1")
        exported_ref = event_kinds["delegation.prepared"]["refs"][0]
        self.assertNotIn("path", exported_ref)  # 引用只留 文件名+哈希
        self.assertIn("file", exported_ref)
        self.assertIn("sha256", exported_ref)

    def test_historical_run_exports_materials_view_without_backfill(self):
        run_dir = self.root / "old-run"
        run_dir.mkdir()
        (run_dir / "instruction-round1.md").write_text("历史指令", encoding="utf-8")
        (run_dir / "state.json").write_text(json.dumps({
            "state_version": "p3.2", "status": "IMPROVED", "verdict": "improved",
            "goal": "历史目标", "rounds": [{
                "round_no": 1, "instruction_path": str(run_dir / "instruction-round1.md"),
                "codex_events_path": "", "diagnosis_path": "", "review_path": "",
                "codex_status": "completed", "codex_usage": {"input_tokens": 5},
            }],
        }, ensure_ascii=False), encoding="utf-8")
        out_path = self.root / "old-export.json"
        summary = export_timeline(run_dir, out_path)
        self.assertEqual(summary["mode"], "historical")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertIn("旧记录兼容", payload["compatibility_note"])
        self.assertIn("未记录", payload["historical_view_md"])
        self.assertFalse((run_dir / "trace").exists())  # 导出不回填
        self.assertNotIn("events", payload)  # 无事件流可导出


if __name__ == "__main__":
    unittest.main()
