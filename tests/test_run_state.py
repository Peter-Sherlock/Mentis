"""P2.4-O2 回归例：阶段记录、生产者活动信号、最后可靠状态与恢复代次（全部离线）。

验收映射（OBSERVABILITY_PLAN §4/O2 退出门槛的离线部分）：
- 长等待无业务事件：阶段进展与生产者活动被区分显示，来源与有效期明确；
- 超时/异常/活动过期/写失败/损坏行：不假完成，未知保持未知，不据此判失败；
- notify 不阻塞、request_decision 只阻塞依赖动作（等待原因与开始时间可见）；
- 旧收尾后恢复：新代次可读，旧 manifest/旧信号不使 --follow 误报完成；
- CLI/投影/页面共用 load_run_state；未知预算不填 0；历史读取字节不变；
- 旁观器停止不影响生产者继续写记录。
真实模型/执行器调用 0；本文件全部为离线夹具（受控材料目录，非真实 AI 任务）。
"""

import hashlib
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from mentis.run_projection import (load_run_state, render_state_markdown)
from mentis.trace import (Heartbeat, StageTracker, TraceWriter, activity_status,
                          current_generation, follow_timeline, load_events,
                          load_stages)


class O2TestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_run(self, name="run") -> Path:
        run_dir = self.root / name
        writer = TraceWriter(run_dir, name)
        return run_dir, writer


class StageTrackerTests(O2TestBase):
    def test_stage_lifecycle_records_start_end_duration_and_generation(self):
        run_dir, w = self.make_run()
        tracker = w.stage_tracker()
        w.emit("mentis", "goal.recorded", "目标")
        self.assertEqual(tracker.start("plan", detail="组装计划"), 1)
        tracker.finish(status="ok", detail="计划 v1")
        rows = load_stages(run_dir)
        self.assertEqual([r["phase"] for r in rows], ["start", "end"])
        self.assertEqual(rows[0]["stage"], "plan")
        self.assertEqual(rows[0]["generation"], 1)
        self.assertEqual(rows[1]["status"], "ok")
        self.assertIsInstance(rows[1]["duration_s"], float)
        self.assertGreaterEqual(rows[1]["duration_s"], 0)  # 无负耗时

    def test_unbalanced_start_marked_interrupted_not_fake_success(self):
        """异常路径未关闭的阶段：如实记 interrupted，不冒充正常结束。"""
        run_dir, w = self.make_run()
        tracker = w.stage_tracker()
        tracker.start("build", detail="委托制作")
        tracker.start("observe", detail="新阶段")  # build 未关闭 → 自动补 interrupted
        rows = load_stages(run_dir)
        build_end = [r for r in rows if r["stage"] == "build" and r["phase"] == "end"]
        self.assertEqual(build_end[0]["status"], "interrupted")

    def test_reconcile_closes_stale_open_stage_after_recovery(self):
        """恢复代次：旧进程遗留的未关闭阶段由新代次如实关闭。"""
        run_dir, w = self.make_run()
        old = w.stage_tracker()
        old.start("waiting_user", blocking=True, reason="日期冲突")
        w.emit("mentis", "run.resumed", "恢复")  # 新代次
        new_tracker = w.stage_tracker()  # 新进程的记录器
        new_tracker.start("build")
        new_tracker.finish(status="ok")
        rows = load_stages(run_dir)
        ends = {(r["stage"]): r.get("status") for r in rows if r["phase"] == "end"}
        self.assertEqual(ends["waiting_user"], "interrupted")  # 旧遗留如实关闭
        self.assertEqual(ends["build"], "ok")

    def test_negative_duration_never_written(self):
        """时钟异常：不生成负数耗时（回退为未记录）。"""
        run_dir, w = self.make_run()
        tracker = w.stage_tracker()
        tracker.start("plan")
        with unittest.mock.patch("mentis.trace.time.monotonic",
                                 return_value=time.monotonic() - 100):
            tracker.finish(status="ok")
        rows = load_stages(run_dir)
        self.assertIsNone(rows[-1]["duration_s"])


class HeartbeatTests(O2TestBase):
    def test_heartbeat_records_identity_generation_ttl_and_source(self):
        run_dir, w = self.make_run()
        w.emit("mentis", "goal.recorded", "目标")  # 有事件流后代次可导出
        hb = Heartbeat(w, producer_role="eval-run:input-002/M", ttl_s=30)
        hb.touch()
        status, line = activity_status(run_dir)
        self.assertEqual(status, "fresh")
        self.assertIn("eval-run:input-002/M", line)
        from mentis.trace import load_heartbeat
        data = load_heartbeat(run_dir)
        self.assertEqual(data["schema"], "mentis-heartbeat/1")
        self.assertEqual(data["generation"], 1)
        self.assertEqual(data["ttl_s"], 30)
        self.assertEqual(data["producer"]["pid"], __import__("os").getpid())
        self.assertIn("独立于旁观器", data["source"])

    def test_stale_heartbeat_means_uncertain_not_failed(self):
        """信号过期：类别 stale，说明「无法确认活跃」，不断言失败。"""
        run_dir, w = self.make_run()
        hb = Heartbeat(w, ttl_s=1)
        hb.touch()
        time.sleep(1.2)
        status, line = activity_status(run_dir)
        self.assertEqual(status, "stale")
        self.assertIn("无法确认生产者仍活跃", line)
        self.assertIn("不判失败", line)

    def test_old_generation_heartbeat_does_not_represent_current_producer(self):
        """晚到的旧代次信号：标 old_generation，不覆盖新代次状态。"""
        run_dir, w = self.make_run()
        w.emit("mentis", "goal.recorded", "目标")  # 有事件流后代次可导出
        hb_old = Heartbeat(w, producer_role="旧进程", ttl_s=120)
        hb_old.touch()
        w.emit("mentis", "run.resumed", "恢复；新代次")
        status, line = activity_status(run_dir)
        self.assertEqual(status, "old_generation")
        self.assertIn("旧代次 1", line)
        self.assertIn("当前代次 2", line)

    def test_corrupt_and_missing_heartbeat_degrade_explicitly(self):
        run_dir, w = self.make_run()
        self.assertEqual(activity_status(run_dir)[0], "missing")
        (run_dir / "trace" / "heartbeat.json").write_text("{半行坏 JSON", encoding="utf-8")
        self.assertEqual(activity_status(run_dir)[0], "corrupt")

    def test_heartbeat_write_failure_counts_integrity(self):
        run_dir, w = self.make_run()
        blocked = run_dir / "trace" / "hb-dir"
        blocked.mkdir()
        hb = Heartbeat(w, ttl_s=30)
        hb.writer.heartbeat_path = blocked  # 同名目录挡路
        self.assertFalse(hb.touch())
        self.assertGreaterEqual(w.write_failures, 1)

    def test_stop_then_stale_no_fake_stopped_record(self):
        """停止信号后不写「已停止」记录：文件保持最后状态，随后自然过期。"""
        run_dir, w = self.make_run()
        hb = Heartbeat(w, ttl_s=1)
        hb.start(interval_s=0.5)
        hb.stop()
        from mentis.trace import load_heartbeat
        data = load_heartbeat(run_dir)
        self.assertEqual(data["read"], "ok")  # 没有伪造的终止记录
        self.assertNotIn("已停止", json.dumps(data, ensure_ascii=False))


class GenerationAndFollowTests(O2TestBase):
    def _drain(self, run_dir, lines, stop_flag, poll_s=0.05):
        t = threading.Thread(target=lambda: follow_timeline(
            run_dir, out=lines.append, poll_s=poll_s,
            stop_check=lambda: stop_flag["on"]), daemon=True)
        t.start()
        return t

    def test_old_finalize_then_resume_does_not_fool_follow(self):
        """旧收尾 + 同一运行已恢复：--follow 不退出、不误报完成，新代次可见。

        复现顺序对齐真实风险：恢复发生在旁观器附着之前（重开窗续办后的核对场景）。
        """
        run_dir, w = self.make_run()
        w.emit("mentis", "goal.recorded", "目标")
        w.emit("mentis", "run.finalized", "ERROR 收尾（第一代）", status="failed")
        w.finalize("ERROR")
        # 同一运行恢复：新代次开始（先于旁观器附着）
        w2 = TraceWriter(run_dir, run_dir.name)
        w2.emit("mentis", "run.resumed", "从落盘状态恢复；新代次开始")
        w2.emit("mentis", "observation.diagnosis", "新代次诊断活动")
        lines = []
        t = self._drain(run_dir, lines, {"on": False})
        time.sleep(0.6)
        joined = "\n".join(lines)
        self.assertNotIn("[运行已收尾]", joined)  # 旧收尾+旧 manifest 不使 --follow 退出
        self.assertIn("新代次诊断活动", joined)
        self.assertIn("[当前状态] 代次 2", joined)
        self.assertIn("旧代次收尾与活动信号不再作为当前状态依据", joined)
        # 新代次真正收尾后才退出
        w2.emit("mentis", "run.finalized", "正常收尾（第二代）", status="ok")
        w2.finalize("DELIVERED")
        t.join(timeout=5)
        joined = "\n".join(lines)
        self.assertIn("[运行已收尾]", joined)
        self.assertFalse(t.is_alive())

    def test_resumed_run_quiet_fallback_disabled(self):
        """恢复后事件静默：不触发「按 manifest 静默收尾」兜底（旧 manifest 不算数）。"""
        run_dir, w = self.make_run()
        w.emit("mentis", "run.finalized", "ERROR", status="failed")
        w.finalize("ERROR")
        w2 = TraceWriter(run_dir, run_dir.name)
        w2.emit("mentis", "run.resumed", "恢复")
        lines = []
        stop = {"on": False}
        t = self._drain(run_dir, lines, stop, poll_s=0.05)
        time.sleep(1.2)  # 远超 4 个静默轮
        stop["on"] = True
        t.join(timeout=3)
        self.assertNotIn("按 manifest，事件流静默", "\n".join(lines))
        self.assertIn("静默", "\n".join(lines))  # 如实说明静默不代表无活动

    def test_plain_run_still_exits_on_quiet_manifest(self):
        """从未恢复的旧式运行：manifest 静默兜底保持原语义（兼容）。"""
        run_dir, w = self.make_run()
        w.emit("mentis", "goal.recorded", "目标")
        w.finalize("DELIVERED")  # 只有 manifest，无 run.finalized 事件（旧写入器）
        lines = []
        t = self._drain(run_dir, lines, {"on": False}, poll_s=0.05)
        t.join(timeout=5)
        self.assertIn("按 manifest，事件流静默", "\n".join(lines))

    def test_observer_stop_does_not_affect_producer(self):
        """旁观器停止后，生产者仍可继续写事件与阶段（互不阻塞）。"""
        run_dir, w = self.make_run()
        w.emit("mentis", "goal.recorded", "目标")
        lines = []
        stop = {"on": False}
        t = self._drain(run_dir, lines, stop)
        time.sleep(0.2)
        stop["on"] = True
        t.join(timeout=3)
        self.assertTrue(any("旁观停止" in line for line in lines))
        w.emit("mentis", "review.recorded", "旁观停止后生产者继续记录")
        self.assertEqual(load_events(run_dir)[-1]["summary"], "旁观停止后生产者继续记录")


class RunStateTests(O2TestBase):
    def test_long_wait_distinguishes_stage_progress_from_activity(self):
        """长等待无业务事件：阶段进行中 + 活动信号新鲜，两者来源分列。"""
        run_dir, w = self.make_run()
        hb = w.start_heartbeat(producer_role="collab:test", interval_s=0.2, ttl_s=5)
        tracker = w.stage_tracker()
        tracker.start("build", detail="委托制作中")
        time.sleep(0.5)  # 长等待：无任何业务事件
        state = load_run_state(run_dir)
        self.assertEqual(state["current_stage"]["stage"], "build")
        self.assertTrue(state["current_stage"]["open"])
        self.assertEqual(state["activity"]["category"], "fresh")
        self.assertNotEqual(state["last_collected"], "unrecorded")
        self.assertIn("读取方时刻", render_state_markdown(state))
        w.stop_heartbeat()

    def test_waiting_user_block_depends_only_on_decision(self):
        """request_decision 阻塞依赖动作并显示原因；notify 不产生等待。"""
        run_dir, w = self.make_run()
        tracker = w.stage_tracker()
        w.emit("mentis", "decision.routed", "通知：非阻塞选项已留档（notify 不阻塞）")
        tracker.start("waiting_user", blocking=True, reason="活动日期需要你确认",
                      detail="依赖 build 暂停")
        state = load_run_state(run_dir)
        self.assertEqual(len(state["open_waits"]), 1)
        wait = state["open_waits"][0]
        self.assertIn("日期", wait["reason"])
        self.assertTrue(wait["blocking"])
        self.assertIn("依赖该决定的工作暂停", wait["depends"])
        self.assertIn("通知类消息不阻塞", wait["depends"])
        md = render_state_markdown(state)
        self.assertIn("等待用户决定", md)
        self.assertIn("活动日期需要你确认", md)

    def test_current_generation_finalized_vs_old(self):
        """当前代次收尾与旧代次收尾分列；判定保持原值。"""
        run_dir, w = self.make_run()
        w.emit("mentis", "run.finalized", "ERROR（第一代）", status="failed")
        state = load_run_state(run_dir)
        self.assertTrue(state["finalized"]["in_current_generation"])
        w2 = TraceWriter(run_dir, run_dir.name)
        w2.emit("mentis", "run.resumed", "恢复")
        state = load_run_state(run_dir)
        self.assertFalse(state["finalized"]["in_current_generation"])
        self.assertIn("不得据此视为完成", state["finalized"]["note"])

    def test_unknown_budget_not_filled_with_zero(self):
        """collab/guidance 无预算结构：上限/剩余 unrecorded，不写 0。"""
        run_dir, w = self.make_run()
        (run_dir / "session.json").write_text("{}", encoding="utf-8")
        state = load_run_state(run_dir)
        self.assertEqual(state["counts"]["model_calls"], 0)
        self.assertIn("unrecorded", state["budget"]["note"])
        self.assertEqual(state["budget"]["remaining"], {})
        md = render_state_markdown(state)
        self.assertIn("未结构化记录预算", md)

    def test_eval_budget_remaining_only_when_known(self):
        """eval 运行：上限+已用齐备才给剩余；未知不虚构。"""
        run_dir, w = self.make_run()
        (run_dir / "runner-state.json").write_text(json.dumps({
            "schema": "mentis-eval-state/1", "status": "in_flight",
            "budget": {"max_attempts": 3, "max_revisions": 2, "max_model_calls": 4,
                       "per_turn_timeout_s": 600,
                       "used": {"attempts": 1, "revisions": 0, "model_calls": 2}},
            "model_requests": [
                {"request_id": "call-1", "ok": True, "mock": True,
                 "http_attempted": False, "usage": {"total_tokens": 1}, "error": ""},
                {"request_id": "call-2", "ok": True, "mock": True,
                 "http_attempted": True, "usage": {"total_tokens": 2}, "error": ""}],
            "meta": {"input": {"user_goal": "目标"}}, "summary": {}}), encoding="utf-8")
        state = load_run_state(run_dir)
        self.assertEqual(state["budget"]["remaining"],
                         {"attempts": 2, "revisions": 2, "model_calls": 2})
        self.assertEqual(state["counts"]["model_calls"], 2)  # 历史降级计数（无 sidecar）
        self.assertEqual(state["counts"]["http_attempted"], 1)

    def test_projection_and_page_share_same_state_source(self):
        """CLI（status）与页面共用 load_run_state：同一目录同一结论。"""
        from mentis.run_projection import load_projection
        from mentis.run_view import render_content
        run_dir, w = self.make_run()
        hb = Heartbeat(w, producer_role="drill", ttl_s=30)
        hb.touch()
        tracker = w.stage_tracker()
        tracker.start("observe", detail="观察中")
        state = load_run_state(run_dir)
        md = render_state_markdown(state)
        page_fragment = render_content(load_projection(run_dir), load_events(run_dir),
                                       "", {}, state)
        self.assertIn("观察中", md)
        self.assertIn("观察中", page_fragment)
        self.assertIn("drill", page_fragment)
        w.stop_heartbeat()

    def test_state_read_never_writes_run_materials(self):
        """状态读取前后运行目录字节不变（读取不写回）。"""

        def snapshot(run_dir):
            return {str(p.relative_to(run_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(run_dir.rglob("*")) if p.is_file()}

        run_dir, w = self.make_run()
        hb = Heartbeat(w, ttl_s=30)
        hb.touch()
        w.stage_tracker().start("plan")
        before = snapshot(run_dir)
        load_run_state(run_dir)
        render_state_markdown(load_run_state(run_dir))
        self.assertEqual(before, snapshot(run_dir))
        w.stop_heartbeat()


class FlowIntegrationTests(O2TestBase):
    def test_eval_m_arm_writes_stages_and_heartbeat_lifecycle(self):
        """eval M 臂（离线夹具）：计划→诊断→处置各阶段入记录；收尾后信号自然过期。"""
        try:
            from tests.test_eval_runner import (PASSING_ISO, make_prepared, make_run)
            from tests.test_collab import FakeAdapter
            from tests.test_guidance import DIAGNOSIS_QUALIFIED, FakeGateway
        except ImportError:
            from test_eval_runner import (PASSING_ISO, make_prepared, make_run)
            from test_collab import FakeAdapter
            from test_guidance import DIAGNOSIS_QUALIFIED, FakeGateway
        root = self.root
        prepared = make_prepared(root)
        run = make_run(root, prepared, "input-002/M", FakeAdapter({"first": "good"}),
                       FakeGateway([DIAGNOSIS_QUALIFIED]))
        with unittest.mock.patch("mentis.trace.HEARTBEAT_TTL_S", 1.0):
            with unittest.mock.patch("mentis.eval_runner.check_admission",
                                     return_value=PASSING_ISO):
                run.run()
        stages = [r["stage"] for r in load_stages(run.run_dir)]
        self.assertIn("plan", stages)
        self.assertIn("diagnose", stages)
        self.assertIn("adjudicate", stages)
        self.assertIn("finalize", stages)
        self.assertNotIn("build", stages)  # 合格不改零委托：无制作阶段（不虚构）
        time.sleep(1.2)  # TTL 过后：信号自然过期=状态不确定（不写「已停止」冒充终态）
        self.assertEqual(activity_status(run.run_dir)[0], "stale")
        state = load_run_state(run.run_dir)
        self.assertEqual(state["verdict"], "no_change")  # 业务判定不变

    def test_guidance_need_decision_leaves_waiting_open(self):
        """guide-revise 待决路径（离线夹具）：等待用户阶段保持打开，状态可读。"""
        try:
            from tests.test_guidance import (DIAGNOSIS_NEEDS_USER, FakeAdapter,
                                             FakeGateway, _FixedCoin, file_aware_measure,
                                             make_baseline)
            from tests.test_guidance import stub_capture as g_capture
            from mentis.guidance import GuidanceCycle
        except ImportError:
            from test_guidance import (DIAGNOSIS_NEEDS_USER, FakeAdapter,
                                       FakeGateway, _FixedCoin, file_aware_measure,
                                       make_baseline)
            from test_guidance import stub_capture as g_capture
            from mentis.guidance import GuidanceCycle
        cycle = GuidanceCycle(
            baseline_dir=make_baseline(self.root), run_dir=self.root / "run",
            adapter=FakeAdapter(behavior={"first": "fixed"}),
            gateway=FakeGateway([DIAGNOSIS_NEEDS_USER]),
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        with unittest.mock.patch("mentis.trace.HEARTBEAT_TTL_S", 1.0):
            self.assertEqual(cycle.run(), "NEEDS_DECISION")
        time.sleep(1.2)  # TTL 过后：生产者已退出，信号自然过期=状态不确定
        rows = load_stages(self.root / "run")
        waiting = [r for r in rows if r["stage"] == "waiting_user"]
        self.assertEqual(waiting[0]["phase"], "start")  # 保持打开：等待跨进程持续
        self.assertNotIn("end", [r.get("phase") for r in waiting])
        state = load_run_state(self.root / "run")
        self.assertEqual(state["current_stage"]["stage"], "waiting_user")
        self.assertEqual(state["activity"]["category"], "stale")
        self.assertTrue(any("等待用户决定" in line
                            for line in render_state_markdown(state).splitlines()))


if __name__ == "__main__":
    unittest.main()
