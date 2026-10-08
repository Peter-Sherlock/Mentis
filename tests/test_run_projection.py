"""P2.4-O1 回归例：统一公开消息采集、补齐交流视图、共用只读运行投影（全部离线）。

验收映射（OBSERVABILITY_PLAN §4/O1 退出门槛的离线部分）：
- 三入口共用采集逻辑：agent_message 全文归档+哈希引用+首尾保留摘要；归档失败
  显式标完整性缺口；命令输出未采集标缺口；推理只记类型；
- conversation 补齐评估计划/观察/诊断/处置决定并保持事件顺序；零委托如实显示；
- 投影（replay）确定性读取：真实形状的零委托链（费用缺失→模型候选→程序 0.082
  判计划盲区→零委托→no_change）连续可追溯；坏哈希/越界引用/缺字段正确降级；
- 读取前后源材料字节不变；不重算业务判定。
真实模型/执行器调用 0；本文件全部为离线夹具（FakeAdapter/Mock 网关，已标注）。
"""

import hashlib
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from mentis.trace import (TraceWriter, bounded_text, describe_executor_event,
                          load_events, load_model_calls, render_conversation)
from mentis.run_projection import load_projection, render_projection_markdown

try:
    from tests.test_collab import FakeAdapter, GOOD_PAGE, make_collab, stub_capture
    from tests.test_eval_runner import (PASSING_ISO, make_prepared, make_run)
    from tests.test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                                     DIAGNOSIS_OVERFLOW, DIAGNOSIS_QUALIFIED,
                                     FakeGateway, file_aware_measure, make_baseline)
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import FakeAdapter, GOOD_PAGE, make_collab, stub_capture
    from test_eval_runner import (PASSING_ISO, make_prepared, make_run)
    from test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                               DIAGNOSIS_OVERFLOW, DIAGNOSIS_QUALIFIED,
                               FakeGateway, file_aware_measure, make_baseline)


LONG_CHINESE_MESSAGE = ("初稿已完成，费用区域保留了占位符。"
                        + "这是很长的中文公开回复，" * 30
                        + "尾部说明：临时目录删除被审批拦截，目录仍保留，请人工核对。")


def _archive_ok(text):
    return {"path": "trace/messages/m.md", "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "role": "executor_message"}


class BoundedTextTests(unittest.TestCase):
    def test_long_chinese_message_keeps_tail(self):
        """超过 160 字的中文消息：摘要首尾保留，尾部关键说明不丢失。"""
        out = bounded_text(LONG_CHINESE_MESSAGE)
        self.assertLessEqual(len(out), 160)
        self.assertIn("…（中略）…", out)
        self.assertTrue(LONG_CHINESE_MESSAGE.startswith(out[:20]))
        self.assertIn("尾部说明：临时目录删除被审批拦截", out)  # 尾部保留
        self.assertTrue(LONG_CHINESE_MESSAGE.endswith(out[-20:]))

    def test_short_message_untouched(self):
        self.assertEqual(bounded_text("短消息"), "短消息")


class DescribeExecutorEventTests(unittest.TestCase):
    def test_agent_message_archived_full_text_with_hash_ref(self):
        """全文经回调归档，摘要含「全文已归档」且首尾保留；refs 携带哈希引用。"""
        detail, refs = describe_executor_event(
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": LONG_CHINESE_MESSAGE}},
            {}, archive_message=_archive_ok)
        self.assertIn("全文已归档", detail)
        self.assertLessEqual(len(detail), 220)  # 有界摘要（前缀+160 预算）
        self.assertIn("临时目录删除被审批拦截", detail)  # 尾部保留
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["role"], "executor_message")

    def test_agent_message_archive_failure_marks_gap(self):
        """归档失败：摘要显式标完整性缺口，不静默（refs 为空）。"""
        detail, refs = describe_executor_event(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "回复"}},
            {}, archive_message=lambda text: None)
        self.assertIn("全文归档失败", detail)
        self.assertIn("完整性缺口", detail)
        self.assertEqual(refs, [])

    def test_command_execution_status_and_output_gap(self):
        """命令动作状态明确；未采集输出标缺口；状态后缀仍居末位（子项小结可解析）。"""
        for raw, want in (("completed", "（completed）"), ("failed", "（failed）"),
                          ("in_progress", "（in_progress）"), ("", "（unknown）")):
            detail, refs = describe_executor_event(
                {"type": "item.completed", "item": {"type": "command_execution",
                                                    "status": raw, "command": "ls -la"}},
                {})
            self.assertIn("命令 ls -la", detail)
            self.assertIn("输出未采集", detail)
            self.assertTrue(detail.endswith(want), detail)
            self.assertEqual(refs, [])

    def test_reasoning_records_type_only(self):
        detail, _ = describe_executor_event(
            {"type": "item.completed", "item": {"type": "reasoning", "text": "内部推理"}},
            {})
        self.assertEqual(detail, "推理条目（内容不入默认层）")
        self.assertNotIn("内部推理", detail)

    def test_never_raises_and_first_latency(self):
        """输入异常时降级为采集异常占位（不抛）；首事件带延迟前缀。"""
        detail, _ = describe_executor_event("not-a-dict", {})  # 非法事件：不抛
        self.assertIn("事件采集异常", detail)
        detail, _ = describe_executor_event(
            {"type": "item.started", "item": {"type": "command_execution",
                                              "status": "in_progress", "command": "ls"}},
            {"first": True, "first_event_latency_s": 1.5})
        self.assertTrue(detail.startswith("首事件延迟 1.5s；"), detail)


class ConversationViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _seed_zero_delegation_run(self, run_dir: Path):
        w = TraceWriter(run_dir, "run-z")
        w.emit("mentis", "policy.set", "预算 3 委托/2 修订/4 模型调用", status="ok")
        plan_path = run_dir / "evaluation-plan" / "plan-v1.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_bytes(b'{"plan": "synthetic"}')
        plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        w.emit("mentis", "evaluation_plan.recorded",
               "评估计划 v1 已落盘（委托前）：14 个维度",
               refs=[{"path": str(plan_path), "sha256": plan_sha, "role": "plan"}])
        w.emit("observer", "observation.structure", "观察 49 条目")
        diag = run_dir / "diagnosis.md"
        diag.write_text("诊断（真实模型）：有效 4、拒收 0", encoding="utf-8")
        w.emit("mentis", "observation.diagnosis", "诊断（真实模型）：有效 4、拒收 0",
               refs=[{"path": str(diag), "sha256": hashlib.sha256(diag.read_bytes()).hexdigest(),
                      "role": "diagnosis"}])
        w.emit("mentis", "decision.routed",
               "候选审查 d1「费用信息缺失」→ plan_blind_spot；映射 none（得分 0.082）",
               status="pending")
        w.emit("mentis", "review.recorded", "可成立的自主修订 0 条，保留作品")
        w.finalize()

    def test_zero_delegation_run_shows_plan_observation_diagnosis_decisions_in_order(self):
        """零委托运行：计划→观察→诊断→处置→复查按 seq 顺序可读；明示零委托非缺记录。"""
        run_dir = self.root / "run"
        self._seed_zero_delegation_run(run_dir)
        view = render_conversation(run_dir)
        positions = [view.index(marker) for marker in (
            "[2] Mentis｜评估计划（委托前落盘）", "[3] Mentis 观察：观察 49 条目",
            "[4] Mentis｜诊断（模型候选，逐条审查见下）", "[5] Mentis｜处置决定",
            "费用信息缺失", "0.082", "[6] Mentis｜review.recorded")]
        self.assertEqual(positions, sorted(positions))  # 事件顺序保持
        self.assertIn("0 次委托", view)
        self.assertIn("不为展示交流补造执行器消息", view)
        self.assertNotIn("Codex 回复 Mentis", view)  # 无委托就不能出现执行器回复

    def test_plan_ref_hash_mismatch_marked(self):
        """计划引用被改动：视图仍显示记录中的原始 SHA（不回填），篡改由投影标 unverified。"""
        run_dir = self.root / "run"
        self._seed_zero_delegation_run(run_dir)
        plan_path = run_dir / "evaluation-plan" / "plan-v1.json"
        plan_path.write_bytes(b'{"plan": "tampered"}')  # 读取前篡改（模拟坏哈希）
        view = render_conversation(run_dir)
        self.assertIn("SHA-256", view)  # 记录值原样展示（原始记录，不重写）
        proj = load_projection(run_dir)
        plan_rows = [v for v in proj["versions"] if "plan-v1" in v["label"]]
        self.assertEqual(plan_rows[0]["verify"], "unverified")


class RecordKeepingGateway(FakeGateway):
    """测试网关：complete 后把 CallRecord 形状的返回记录也存入 calls（对齐真实
    ModelGateway.calls 语义），供 sidecar 记账测试使用；不改变 FakeGateway
    既有 dict（prompt 日志）语义，其余测试不受影响。"""

    def complete(self, system, user_prompt, json_mode=True, mock=False, images=None):
        record = super().complete(system, user_prompt, json_mode, mock, images)
        self.calls.append(record)
        return record


class ModelCallRecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_record_and_load_model_calls_roundtrip(self):
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        call = SimpleNamespace(
            request_id="call-abc", model="mock-model", host="example.test",
            created_at="2026-10-08T00:00:00", latency_ms=123, ok=True,
            usage={"total_tokens": 10}, error="", mock=True, http_attempted=False,
            prompt_sha256="ab" * 8, image_refs=["desktop.png;" + "f" * 12])
        self.assertTrue(w.record_model_call(call, stage="diagnosis",
                                            output_refs=[{"path": "diagnosis.md",
                                                          "sha256": "cd" * 32, "role": "diagnosis"}]))
        rows = load_model_calls(run_dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema"], "mentis-model-call/1")
        self.assertEqual(row["request_id"], "call-abc")
        self.assertEqual(row["stage"], "diagnosis")
        self.assertEqual(row["usage"], {"total_tokens": 10})
        self.assertFalse(row["input_text_archived"])  # 输入全文未归档：显式为假
        self.assertEqual(row["output_refs"][0]["role"], "diagnosis")

    def test_model_call_write_failure_counts_integrity(self):
        """sidecar 写失败计入 write_failures——采集缺口不静默。"""
        run_dir = self.root / "run"
        w = TraceWriter(run_dir, "run-x")
        blocked = run_dir / "trace" / "blocked-dir"
        blocked.mkdir()  # 同名目录挡路：open("ab") 抛 OSError
        w.model_calls_path = blocked
        call = SimpleNamespace(request_id="call-abc", model="m", host="", created_at="",
                               latency_ms=0, ok=True, usage={}, error="", mock=True,
                               http_attempted=False, prompt_sha256="", image_refs=[])
        self.assertFalse(w.record_model_call(call, stage="diagnosis"))
        self.assertGreaterEqual(w.write_failures, 1)
        self.assertFalse(w.finalize()["integrity"]["events_complete"])


def build_synthetic_eval_run(root: Path) -> Path:
    """合成 eval-run 夹具：形状对齐真实 E2 M 臂零委托运行（数据为合成，非真实运行）。"""
    run_dir = root / "runs" / "synthetic-input-002-M"
    (run_dir / "evaluation-plan").mkdir(parents=True)
    (run_dir / "versions").mkdir(parents=True)
    (run_dir / "workspace").mkdir()
    plan_payload = json.dumps({"plan_id": "synthetic", "version": 1,
                               "model_instantiation": {"status": "ok", "mock": True, "usage": {
                                   "total_tokens": 1}}},
                              ensure_ascii=False).encode("utf-8")
    plan_path = run_dir / "evaluation-plan" / "plan-v1.json"
    plan_path.write_bytes(plan_payload)
    plan_sha = hashlib.sha256(plan_payload).hexdigest()
    page = "<html>合成产物</html>"
    (run_dir / "workspace" / "index.html").write_text(page, encoding="utf-8")
    (run_dir / "versions" / "final-001.html").write_text(page, encoding="utf-8")
    state = {
        "schema": "mentis-eval-state/1", "run_id": run_dir.name, "arm": "input-002/M",
        "status": "finished", "model_requests": [
            {"request_id": "call-plan", "ok": True, "mock": True, "http_attempted": False,
             "usage": {"total_tokens": 1}, "error": ""},
            {"request_id": "call-diag", "ok": True, "mock": True, "http_attempted": False,
             "usage": {"total_tokens": 2}, "error": ""}],
        "meta": {"input": {"user_goal": "合成目标（非真实任务）"}, "policy": {}},
        "summary": {
            "plan_version": 1, "plan_sha256": plan_sha,
            "verdict": "no_change", "verdict_basis": "可成立的自主修订 0 条，保留作品（合成）",
            "candidates": [
                {"no": "d1", "phenomenon": "费用信息缺失，显示为占位符“费用待补充 / 人”（合成）",
                 "dimension": "", "decision": "plan_blind_spot",
                 "reason": "对应不到任何计划维度：计划盲区，不自动采纳（ADR-008 第 6 条）；映射 none（得分 0.082）",
                 "matched_by": "none", "mapping_score": 0.082, "guard": ""},
                {"no": "d2", "phenomenon": "报名方式信息不完整（合成）", "dimension": "D3",
                 "decision": "skip_unverifiable", "reason": "对应维度 D3（检查方式 unverifiable）",
                 "matched_by": "keyword_overlap", "mapping_score": 0.147, "guard": ""}],
        }}
    (run_dir / "runner-state.json").write_text(json.dumps(state, ensure_ascii=False),
                                               encoding="utf-8")
    manifest = {"runner_status": "finished", "arm": "input-002/M",
                "artifact": {"path": str(run_dir / "versions" / "final-001.html"),
                             "sha256": hashlib.sha256(page.encode("utf-8")).hexdigest(),
                             "exists": True}}
    (run_dir / "run-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                               encoding="utf-8")
    w = TraceWriter(run_dir, run_dir.name)
    w.emit("mentis", "policy.set", "预算 3 委托/2 修订/4 模型调用", status="ok")
    w.emit("mentis", "evaluation_plan.recorded", "评估计划 v1 已落盘（委托前）",
           refs=[{"path": str(plan_path), "sha256": plan_sha, "role": "plan"}])
    w.emit("mentis", "observation.diagnosis", "诊断（Mock）：有效 2、拒收 0", status="ok")
    w.emit("mentis", "decision.routed",
           "候选审查 d1「费用信息缺失…」→ plan_blind_spot；映射 none（得分 0.082）",
           status="pending")
    w.emit("mentis", "review.recorded", "可成立的自主修订 0 条，保留作品")
    w.finalize()
    return run_dir


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_zero_delegation_chain_readable_and_judgment_preserved(self):
        """零委托链连续可追溯：费用缺失→候选→程序 0.082 判盲区→零委托→no_change。"""
        run_dir = build_synthetic_eval_run(self.root)
        proj = load_projection(run_dir)
        md = render_projection_markdown(proj)
        self.assertEqual(proj["kind"], "eval-run")
        self.assertEqual(proj["counts"]["executor_dispatches"], 0)
        self.assertEqual(proj["counts"]["model_calls"], 2)
        self.assertEqual(proj["verdict"], "no_change")  # 原判定不改写
        self.assertIn("d1（plan_blind_spot", md)
        self.assertIn("费用信息缺失", md)
        self.assertIn("0.082", md)
        self.assertIn("**零委托**", md)
        # 顺序：d1 在 d2 前（模型提案顺序保持）
        self.assertLess(md.index("d1（plan_blind_spot"), md.index("d2（skip_unverifiable"))

    def test_historical_model_request_stage_linkage_and_gaps(self):
        """历史运行：计划实例化经用量精确匹配关联；其余关联如实标 unrecorded。"""
        run_dir = build_synthetic_eval_run(self.root)
        proj = load_projection(run_dir)
        stages = [c["stage"] for c in proj["model_calls"]]
        self.assertIn("plan_instantiation", stages[0])
        self.assertIn("unrecorded", stages[1])
        self.assertTrue(any("model-calls.jsonl" in g for g in proj["gaps"]))

    def test_bad_hash_and_out_of_run_refs_degrade(self):
        """坏哈希标 unverified；越界引用不读取并标注；缺字段标 unrecorded。"""
        run_dir = build_synthetic_eval_run(self.root)
        plan_path = run_dir / "evaluation-plan" / "plan-v1.json"
        plan_path.write_bytes(b'{"plan_id": "tampered"}')  # 篡改 → 坏哈希
        w = TraceWriter(run_dir, run_dir.name)  # 只用于追加事件（读端语义不变）
        w.emit("mentis", "decision.routed", "引用越界示例",
               refs=[{"path": str(self.root / "outside.md"), "sha256": "x" * 64,
                      "role": "observation"}])
        (self.root / "outside.md").write_text("机密", encoding="utf-8")
        proj = load_projection(run_dir)
        md = render_projection_markdown(proj)
        plan_rows = [v for v in proj["versions"] if "plan-v1" in v["label"]]
        self.assertEqual(plan_rows[0]["verify"], "unverified")
        self.assertTrue(any("越界引用" in g for g in proj["gaps"]))
        self.assertNotIn("机密", md)  # 越界文件内容不进入投影
        self.assertIn("unverified", md)

    def test_projection_is_read_only_bytes_unchanged(self):
        """conversation / trace 回放 / 投影读取前后：运行目录逐文件字节不变。"""
        run_dir = build_synthetic_eval_run(self.root)

        def snapshot():
            return {str(p.relative_to(run_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(run_dir.rglob("*")) if p.is_file()}

        before = snapshot()
        render_conversation(run_dir)
        load_projection(run_dir)
        render_projection_markdown(load_projection(run_dir))
        self.assertEqual(before, snapshot())

    def test_unverifiable_dimension_label_not_recomputed(self):
        """投影只搬运既有处置与得分，不重算映射（d2 的 0.147 原样出现）。"""
        run_dir = build_synthetic_eval_run(self.root)
        md = render_projection_markdown(load_projection(run_dir))
        self.assertIn("0.147", md)
        self.assertIn("处置主体：程序", md)


class EvalRunCaptureTests(unittest.TestCase):
    """eval-run 入口的采集一致性（此前缺全文归档的入口）。离线 Fake，已标注。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_eval_arm_archives_agent_message_full_text(self):
        """eval 入口（B-1 臂，必委托）：执行器公开消息全文归档+哈希引用+首尾摘要
        （该入口此前只存 160 字截断摘要）。采集逻辑与 M 臂共用同一 _dispatch。"""
        prepared = make_prepared(self.root)

        class MessagingAdapter(FakeAdapter):
            def _run(self, spec, status_override=None):
                if spec.on_event:
                    spec.on_event({"type": "item.completed", "item": {
                        "type": "agent_message", "text": LONG_CHINESE_MESSAGE}},
                        {"first": False})
                return super()._run(spec, status_override)

        run = make_run(self.root, prepared, "input-002/B-1", MessagingAdapter({"first": "good"}),
                       FakeGateway([]))
        with unittest.mock.patch("mentis.eval_runner.check_admission",
                                 return_value=PASSING_ISO):
            run.run()
        events = load_events(run.run_dir)
        msg_events = [e for e in events if e.get("kind") in ("executor.item", "executor.started")
                      and "最终消息" in str(e.get("summary", ""))]
        self.assertEqual(len(msg_events), 1)
        event = msg_events[0]
        self.assertIn("全文已归档", event["summary"])
        self.assertIn("临时目录删除被审批拦截", event["summary"])  # 尾部保留
        refs = [r for r in event.get("refs", []) if r.get("role") == "executor_message"]
        self.assertEqual(len(refs), 1)
        archived = Path(refs[0]["path"])
        self.assertTrue(archived.is_file())
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), refs[0]["sha256"])
        self.assertEqual(archived.read_text(encoding="utf-8"), LONG_CHINESE_MESSAGE)

    def test_eval_m_arm_records_model_calls_sidecar_with_stage(self):
        """新 eval 运行写 trace/model-calls.jsonl：阶段关联+输入未归档标记。"""
        prepared = make_prepared(self.root)
        run = make_run(self.root, prepared, "input-002/M", FakeAdapter({"first": "good"}),
                       RecordKeepingGateway([DIAGNOSIS_QUALIFIED]))
        with unittest.mock.patch("mentis.eval_runner.check_admission",
                                 return_value=PASSING_ISO):
            run.run()
        rows = load_model_calls(run.run_dir)
        self.assertEqual(len(rows), 2)  # 计划实例化 + 诊断
        stages = [r["stage"] for r in rows]
        self.assertEqual(stages[0], "plan_instantiation")
        self.assertEqual(stages[1], "diagnosis")
        self.assertTrue(all(r["output_refs"] for r in rows))
        # 投影读取 sidecar：阶段关联可读，unrecorded 降级不再出现
        proj = load_projection(run.run_dir)
        self.assertEqual(proj["counts"]["model_calls"], 2)
        self.assertEqual(proj["model_calls"][0]["stage"], "plan_instantiation")

    def test_collab_flow_records_model_calls_with_stages(self):
        """collab 入口（离线 Fake/Mock，已标注）：计划实例化与诊断调用带阶段入 sidecar。"""
        collab = make_collab(adapter=FakeAdapter(behavior={"first": "good"}),
                             gateway=RecordKeepingGateway([DIAGNOSIS_QUALIFIED]),
                             use_model=True, base_dir=self.root / "collab")
        self.assertEqual(collab.run(), "DELIVERED")
        rows = load_model_calls(collab.run_dir)
        stages = [r["stage"] for r in rows]
        self.assertIn("plan_instantiation", stages)
        self.assertIn("diagnosis", stages)
        self.assertTrue(all(r["mock"] for r in rows))  # 全部为 Mock，如实标注

    def test_guidance_flow_records_model_calls_with_stages(self):
        """guide-revise 入口（离线 Fake/Mock，已标注）：合格不改路径下
        计划实例化与诊断阶段入 sidecar；未发生修订即无盲比调用（不虚构）。"""
        from mentis.guidance import GuidanceCycle
        try:
            from tests.test_guidance import stub_capture as g_capture
        except ImportError:
            from test_guidance import stub_capture as g_capture
        cycle = GuidanceCycle(
            baseline_dir=make_baseline(self.root), run_dir=self.root / "run",
            adapter=FakeAdapter(behavior={"first": "fixed"}),
            gateway=RecordKeepingGateway([DIAGNOSIS_QUALIFIED]),
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        self.assertEqual(cycle.run(), "QUALIFIED_NO_CHANGE")
        rows = load_model_calls(cycle.run_dir)
        stages = [r["stage"] for r in rows]
        self.assertEqual(stages[0], "plan_instantiation")
        self.assertIn("diagnosis", stages)
        self.assertNotIn("blind_review", stages)  # 未修订：无盲比调用（不虚构）


if __name__ == "__main__":
    unittest.main()
