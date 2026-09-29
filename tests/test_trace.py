"""P2.4-T1 回归例：追踪写入与事后回放（全部离线；契约 mentis-trace/1）。

验收映射（计划 §4/T1）：
- Fake 测试证明 seq/时间/ID/版本/哈希正确；prompt 哈希与实际发送文本一致；
- 落盘失败不委托；新离线运行能重建 决定→委托→执行终态→复检 顺序；
- 历史缺资料显示「未记录」且不被回填。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mentis.contracts import AttemptStatus
from mentis.trace import (TraceArchiveError, TraceWriter, load_events, render_timeline)

try:
    from tests.test_collab import FakeAdapter, make_collab, stub_capture
    from tests.test_guidance import (DIAGNOSIS_OVERFLOW, DIAGNOSIS_NEEDS_USER, FakeGateway, REVIEW_RESOLVED,
                                     file_aware_measure, make_baseline)
except ImportError:
    from test_collab import FakeAdapter, make_collab, stub_capture
    from test_guidance import (DIAGNOSIS_OVERFLOW, DIAGNOSIS_NEEDS_USER, FakeGateway, REVIEW_RESOLVED,
                               file_aware_measure, make_baseline)


class RecordingAdapter(FakeAdapter):
    """在 FakeAdapter 基础上记录完整收到的 prompt（供哈希一致性断言）。"""

    def _run(self, spec):
        if not hasattr(self, "prompts"):
            self.prompts = []
        self.prompts.append(spec.prompt)
        return super()._run(spec)


try:
    from tests.test_guidance import FakeAdapter as GuidanceFakeAdapter
except ImportError:
    from test_guidance import FakeAdapter as GuidanceFakeAdapter


class RecordingGuidanceAdapter(GuidanceFakeAdapter):
    """guidance 流的页面感知假执行器 + 完整 prompt 记录。"""

    def _run(self, spec):
        if not hasattr(self, "prompts"):
            self.prompts = []
        self.prompts.append(spec.prompt)
        return super()._run(spec)


class TraceWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_seq_monotonic_and_fields(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x", executor_version="fake-0")
        w.emit("mentis", "goal.recorded", "目标文本", brief_version=1)
        w.emit("executor", "executor.completed", "终态 completed", attempt_id="attempt-1",
               brief_version=1, native_thread_id="t-1", status="ok")
        events = load_events(run_dir)
        self.assertEqual([e["seq"] for e in events], [1, 2])
        for e in events:
            self.assertEqual(e["schema_version"], "mentis-trace/1")
            self.assertEqual(e["run_id"], "run-x")
            self.assertIn("time_utc", e)  # ISO 时间，含时区
        self.assertEqual(events[1]["attempt_id"], "attempt-1")
        self.assertEqual(events[1]["native_thread_id"], "t-1")
        self.assertEqual(events[1]["status"], "ok")

    def test_resume_appends_seq_without_reuse(self):
        run_dir = self.root / "run"
        w1 = TraceWriter(run_dir, "run-x")
        w1.emit("mentis", "goal.recorded", "一")
        w1.emit("mentis", "policy.set", "二")
        w2 = TraceWriter(run_dir, "run-x")  # 模拟恢复后的新写入器
        seq = w2.emit("mentis", "run.resumed", "续接")
        self.assertEqual(seq, 3)
        # 损坏行容忍：手工塞入半行后仍能续排
        with (run_dir / "trace" / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write('{"seq": 99, "kind": "trunc')  # 半行
        w3 = TraceWriter(run_dir, "run-x")
        self.assertEqual(w3.emit("mentis", "goal.recorded", "三"), 100)
        events = load_events(run_dir)
        self.assertEqual(events[-1]["seq"], 100)  # 半行不能吞掉后续有效事件
        self.assertIn("corrupt_line", [e["kind"] for e in events])
        self.assertFalse(w3.finalize()["integrity"]["events_complete"])

    def test_prompt_hash_matches_content(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        text = "确切委托指令全文"
        ref = w.archive_prompt("attempt-1", text)
        p = Path(ref["path"])
        self.assertTrue(p.is_file())
        self.assertEqual(ref["sha256"], hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertEqual(ref["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_manifest_integrity_and_delegations(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x", executor_version="fake-0")
        ref = w.archive_prompt("attempt-1", "指令")
        w.emit("mentis", "delegation.prepared", "已归档并委托 build-page",
               attempt_id="attempt-1", brief_version=1, refs=[ref])
        w.record_delegation("attempt-1", 1, ref, action="build-page")
        w.record_executor_result("attempt-1", AttemptStatus.COMPLETED,
                                 native_thread_id="t-9", usage={"input_tokens": 10})
        manifest = w.finalize(outcome="DELIVERED")
        self.assertTrue(manifest["integrity"]["events_complete"])
        d = manifest["delegations"][0]
        self.assertEqual(d["prompt_sha256"], ref["sha256"])
        self.assertEqual(d["native_thread_id"], "t-9")
        self.assertEqual(d["terminal_status"], "completed")
        # 从事件重建（恢复场景）：新写入器读到同一委托
        w2 = TraceWriter(run_dir, "run-x")
        rebuilt = w2.finalize()["delegations"][0]
        self.assertEqual(rebuilt["prompt_sha256"], ref["sha256"])
        self.assertEqual(rebuilt["terminal_status"], "completed")
        self.assertEqual(rebuilt["native_thread_id"], "t-9")
        self.assertEqual(rebuilt["usage"], {"input_tokens": 10})

    def test_terminal_event_restores_usage_even_without_manifest(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        ref = w.archive_prompt("attempt-1", "指令")
        w.emit("mentis", "delegation.prepared", "委托", attempt_id="attempt-1", refs=[ref])
        w.emit("executor", "executor.completed", "完成", attempt_id="attempt-1",
               native_thread_id="thread-1", executor_status="completed",
               usage={"input_tokens": 9})
        restored = TraceWriter(run_dir, "run-x").finalize()["delegations"][0]
        self.assertEqual(restored["usage"], {"input_tokens": 9})
        self.assertEqual(restored["terminal_status"], "completed")
        self.assertEqual(restored["native_thread_id"], "thread-1")

    def test_replay_detects_mutated_reference(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        ref = w.archive_prompt("attempt-1", "最初指令")
        w.emit("mentis", "delegation.prepared", "委托", attempt_id="attempt-1", refs=[ref])
        w.finalize()
        Path(ref["path"]).write_bytes("改动后的指令".encode("utf-8"))
        timeline = render_timeline(run_dir)
        self.assertIn("当前核验：**未通过**", timeline)
        self.assertIn("哈希不匹配", timeline)


class TraceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_offline_run_rebuilds_full_order_and_hash_matches_sent(self):
        """T1 验收：新离线运行重建 决定→委托→执行终态→复检 顺序；哈希=实际发送文本。"""
        collab = make_collab(adapter=RecordingAdapter(behavior={"first": "good"}),
                             base_dir=self.root / "collab")
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        kinds = [e["kind"] for e in load_events(collab.run_dir)]
        self.assertEqual(kinds[0], "goal.recorded")
        self.assertIn("policy.set", kinds)
        self.assertIn("authorization.granted", kinds)
        self.assertIn("delegation.prepared", kinds)
        self.assertIn("executor.completed", kinds)
        self.assertIn("review.recorded", kinds)
        self.assertEqual(kinds[-1], "run.finalized")
        self.assertLess(kinds.index("delegation.prepared"), kinds.index("executor.completed"))
        self.assertLess(kinds.index("executor.completed"), kinds.index("review.recorded"))
        # prompt 哈希 = 实际发送给执行器的文本哈希（FakeAdapter 记录了收到的 prompt）
        manifest = json.loads((collab.run_dir / "trace" / "manifest.json").read_text(encoding="utf-8"))
        sent = collab.adapter.prompts[0]
        self.assertEqual(manifest["delegations"][0]["prompt_sha256"],
                         hashlib.sha256(sent.encode("utf-8")).hexdigest())
        self.assertTrue(manifest["integrity"]["events_complete"])
        # 回放可读
        timeline = render_timeline(collab.run_dir)
        self.assertIn("事后回放（T1）", timeline)
        self.assertIn("当前核验：**通过**", timeline)
        self.assertIn("delegation.prepared", timeline)

    def test_prompt_archive_failure_blocks_delegation(self):
        """契约 §1：prompt 落盘失败 → 不发起委托，尝试按失败记录并留痕。"""
        collab = make_collab(adapter=RecordingAdapter(), base_dir=self.root / "collab")

        def raise_archive(*args, **kwargs):
            raise TraceArchiveError("注入的落盘失败")
        collab.trace.archive_prompt = raise_archive
        status = collab.run()
        self.assertEqual(status, "FAILED")
        self.assertEqual(getattr(collab.adapter, "prompts", []), [])  # 执行器从未被调用
        self.assertEqual(collab.session.attempts[-1].status, AttemptStatus.FAILED)
        failed = [e for e in load_events(collab.run_dir)
                  if e["kind"] == "delegation.prepared" and e["status"] == "failed"]
        self.assertTrue(failed and "不发起" in failed[0]["summary"])
        self.assertFalse(collab.trace.finalize()["integrity"]["events_complete"] is None)

    def test_guidance_run_traces_observation_review_and_delegation(self):
        from mentis.guidance import GuidanceCycle

        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        try:
            from tests.test_guidance import stub_capture as g_capture
        except ImportError:
            from test_guidance import stub_capture as g_capture
        cycle = GuidanceCycle(
            baseline_dir=baseline, run_dir=self.root / "run",
            adapter=RecordingGuidanceAdapter(behavior={"first": "fixed"}), gateway=gateway,
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        self.assertEqual(cycle.run(), "IMPROVED")
        events = load_events(cycle.run_dir)
        kinds = [e["kind"] for e in events]
        for kind in ("goal.recorded", "observation.structure", "observation.render",
                     "observation.diagnosis", "delegation.prepared",
                     "executor.completed", "review.recorded", "run.finalized"):
            self.assertIn(kind, kinds)
        manifest = json.loads((cycle.run_dir / "trace" / "manifest.json").read_text(encoding="utf-8"))
        sent = cycle.adapter.prompts[0]
        self.assertEqual(manifest["delegations"][0]["prompt_sha256"],
                         hashlib.sha256(sent.encode("utf-8")).hexdigest())

    def test_guidance_decision_pause_and_answer_are_traced(self):
        from mentis.guidance import GuidanceCycle
        try:
            from tests.test_guidance import stub_capture as g_capture
        except ImportError:
            from test_guidance import stub_capture as g_capture
        cycle = GuidanceCycle(
            baseline_dir=make_baseline(self.root), run_dir=self.root / "decision-run",
            adapter=RecordingGuidanceAdapter(behavior={"first": "fixed"}),
            gateway=FakeGateway([DIAGNOSIS_NEEDS_USER, REVIEW_RESOLVED]),
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        self.assertEqual(cycle.run(), "NEEDS_DECISION")
        kinds = [e["kind"] for e in load_events(cycle.run_dir)]
        self.assertIn("decision.requested", kinds)
        self.assertIn("decision.routed", kinds)
        self.assertNotIn("delegation.prepared", kinds)
        pending = [i for i in cycle.session.interactions
                   if i.kind.value == "request_decision" and i.answer_source is None]
        self.assertEqual(len(pending), 1)
        cycle.resume(answers={pending[0].interaction_id: "需要，纳入直播信息"})
        kinds = [e["kind"] for e in load_events(cycle.run_dir)]
        self.assertIn("decision.answered", kinds)
        self.assertIn("brief.updated", kinds)

    def test_historical_replay_lists_gaps_without_backfill(self):
        """历史运行：列现存材料与未记录缺口；不生成 trace/（契约 §9 不回填）。"""
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
        timeline = render_timeline(run_dir)
        self.assertIn("历史运行", timeline)
        self.assertIn("instruction-round1.md", timeline)
        self.assertIn("未记录", timeline)
        self.assertIn("不可回填", timeline)
        self.assertFalse((run_dir / "trace").exists())  # 不回填
        self.assertEqual(load_events(run_dir), [])  # 无事件流


if __name__ == "__main__":
    unittest.main()
