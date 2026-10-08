"""O2/C1 审查修正回归（runtime/reviews/p2.4-o2-c1-audit 四项缺陷，全部离线）。

审查复现 → 修复对应：
- A（O2）：已回答的决定仍显示待决——请求/答复按稳定 interaction_id 关联，
  答复只关闭对应项；未回答保留；通知不阻塞；waiting_user 阶段同步；
  历史缺关联标识时如实标缺口，不臆造、不清空全部等待。
- B（C1）：改版覆盖旧资源快照——快照按内容哈希不可变保存；旧配置、旧快照、
  绑定证据在新版本写入前后字节不变，新旧版本分别可核验、可恢复；
  旧布局（按文件名）可读兼容，读取不改写。
- C（C1）：缓存复用跳过完整性检查——同一实例每次 ensure 都重新核验
  配置/快照/绑定；核验失败后依赖配置的委托为 0 次（Fake 执行器证明）。
- D（C1）：首次恢复忽略新目标——冷恢复第一次 ensure 即比较磁盘配置与
  当前条件并完成有原因改版；阶段/调用关联一致。

真实模型/执行器调用 0；FakeAdapter/FakeGateway/直接事件夹具，均已标注。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mentis.trace import TraceWriter, load_events, load_model_calls

try:
    from tests.test_collab import FakeAdapter, make_collab
    from tests.test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y,
                                     DIAGNOSIS_NEEDS_USER, FakeGateway,
                                     file_aware_measure, make_baseline)
    from tests.test_trace import RecordingGuidanceAdapter
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import FakeAdapter, make_collab
    from test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y,
                               DIAGNOSIS_NEEDS_USER, FakeGateway,
                               file_aware_measure, make_baseline)
    from test_trace import RecordingGuidanceAdapter

from mentis.professional import (ConfigManager, ConfigPersistenceError,
                                 config_dir, load_bindings)
from mentis.run_projection import (load_professional_view, load_projection,
                                   load_run_state, render_projection_markdown,
                                   render_state_markdown)

REGISTRY = {
    "registry_version": "t1", "provenance": "test-draft", "evidence_note": "",
    "resources": [{
        "id": "web-landing", "version": "v0.1-test", "provenance": "test-draft",
        "domain": "web", "applies_to": ["单页活动介绍页"], "excludes": [],
        "decision_questions": ["信息密度"], "observation_requirements": [],
        "file": "web-landing.md",
    }],
    "declared_conflicts": [],
}
RESOURCE_V1 = ("<!-- mentis-resource {\"status\": \"pending_human\"} -->\n"
               "## 判断依据\n- [hard_constraint] WA-1：规则一\n")
RESOURCE_V2 = ("<!-- mentis-resource {\"status\": \"pending_human\"} -->\n"
               "## 判断依据\n- [hard_constraint] WA-1：规则一（修订版）\n")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _registry(self, name="reg"):
        reg = self.root / name
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "registry.json").write_text(
            json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
        (reg / "web-landing.md").write_text(RESOURCE_V1, encoding="utf-8")
        return reg

    def _manager(self, run, reg, **kw) -> ConfigManager:
        base = dict(run_dir=run, trace=TraceWriter(run, "fix-test"),
                    registry_path=reg / "registry.json")
        base.update(kw)
        return ConfigManager(**base)


class WaitCorrelationTests(_Base):
    """A：请求/答复按稳定标识关联；答复只关闭对应项。"""

    def _write_flow(self, run, answered_ids=(), requests=None, with_stage=True):
        tw = TraceWriter(run, "fix-a")
        st = tw.stage_tracker()
        tw.emit("mentis", "goal.recorded", "目标")
        requests = requests or [
            ("IX-1", "请求决定 IX-1（阻塞 review-fix）：是否调整版式？｜阻塞原因：审美取舍"),
            ("IX-2", "请求决定 IX-2（阻塞 review-fix）：是否增加预约入口？｜阻塞原因：需要素材"),
        ]
        for ix, text in requests:
            tw.emit("mentis", "decision.requested", text, status="pending",
                    interaction_id=ix)
            if with_stage:
                st.start("waiting_user", blocking=True, reason=text.split("：")[1],
                         detail=f"请求 {ix}", interaction_id=ix)
        for ix, answer in answered_ids:
            tw.emit("user", "decision.answered", f"回答 {ix}：「{answer}」",
                    interaction_id=ix)
            if with_stage:
                st.finish_waiting(status="ok", detail=f"用户回答 {ix}",
                                  interaction_id=ix)
        return tw

    def test_answered_decision_no_longer_pending(self):
        run = self.root / "a1"
        tw = self._write_flow(run, answered_ids=[("IX-1", "保持现状")])
        tw.emit("mentis", "run.finalized", "运行收尾：状态 DELIVERED；交付 是")
        tw.finalize("DELIVERED")
        state = load_run_state(run)
        self.assertEqual(len(state["open_waits"]), 1)  # 仅未回答的 IX-2
        self.assertIn("IX-2", state["open_waits"][0]["reason"])
        self.assertTrue(state["open_waits"][0].get("identified"))
        # 事件与阶段不矛盾：回答过的等待不再以任何形式列为待决
        self.assertNotIn("是否调整版式", state["open_waits"][0]["reason"])

    def test_only_corresponding_decision_closed(self):
        run = self.root / "a2"
        self._write_flow(run, answered_ids=[("IX-2", "暂不增加")])
        state = load_run_state(run)
        self.assertEqual([w["reason"] for w in state["open_waits"]],
                         [w["reason"] for w in state["open_waits"]])
        reasons = " | ".join(w["reason"] for w in state["open_waits"])
        self.assertIn("IX-1", reasons)
        self.assertNotIn("IX-2", reasons)

    def test_unanswered_kept_and_notify_never_blocks(self):
        run = self.root / "a3"
        tw = self._write_flow(run)  # 无人回答
        # notify_progress 类通知（decision.routed）不形成等待
        tw.emit("mentis", "decision.routed", "agent_decide：可逆取舍，自主决定")
        state = load_run_state(run)
        self.assertEqual(len(state["open_waits"]), 2)
        self.assertTrue(all(w["blocking"] for w in state["open_waits"]))

    def test_unlinkable_answer_records_gap_keeps_waits(self):
        """历史记录缺关联信息：不臆造对应、不清空全部等待，只标缺口。"""
        run = self.root / "a4"
        tw = self._write_flow(run, with_stage=False)
        tw.emit("user", "decision.answered", "用户作答（旧格式，无关联标识）")
        state = load_run_state(run)
        self.assertEqual(len(state["open_waits"]), 2)  # 不清空
        self.assertTrue(any("无法关联" in n for n in state["notes"]), state["notes"])

    def test_guidance_flow_answered_closes_wait_end_to_end(self):
        """端到端：guidance NEEDS_DECISION → resume 回答 → 等待关闭（Fake 执行器）。"""
        from mentis.guidance import GuidanceCycle
        try:
            from tests.test_guidance import stub_capture as g_capture
        except ImportError:
            from test_guidance import stub_capture as g_capture
        cycle = GuidanceCycle(
            baseline_dir=make_baseline(self.root), run_dir=self.root / "g-flow",
            adapter=RecordingGuidanceAdapter(behavior={"first": "fixed"}),
            gateway=FakeGateway([DIAGNOSIS_NEEDS_USER, BLIND_AFTER_X, BLIND_AFTER_Y]),
            browser_capture=g_capture, measure=file_aware_measure,
            use_model=True, include_images=True, timeout_s=60, max_rounds=1)
        self.assertEqual(cycle.run(), "NEEDS_DECISION")
        state = load_run_state(cycle.run_dir)
        self.assertEqual(len(state["open_waits"]), 1)
        pending = [i for i in cycle.session.interactions
                   if i.kind.value == "request_decision" and i.answer_source is None]
        cycle.resume(answers={pending[0].interaction_id: "需要，纳入直播信息"})
        state = load_run_state(cycle.run_dir)
        self.assertEqual(state["open_waits"], [])  # 已回答：不再待决
        # 阶段同步：waiting_user 不再处于打开状态
        open_waits_stages = [s for s in state["stages"]
                             if s.get("stage") == "waiting_user" and s.get("open")]
        self.assertEqual(open_waits_stages, [])
        # 事件带关联标识
        answered = [e for e in load_events(cycle.run_dir)
                    if e.get("kind") == "decision.answered"]
        self.assertTrue(answered and answered[0].get("interaction_id"))

    def test_cli_and_page_projection_consistent_on_waits(self):
        """CLI/页面共用投影：等待与配置状态一致（同一读取函数）。"""
        run = self.root / "a6"
        tw = self._write_flow(run, answered_ids=[("IX-1", "保持现状")])
        state = load_run_state(run)
        md = render_state_markdown(state)
        self.assertIn("IX-2", md)
        # 已回答的 IX-1 不再出现在任何「等待用户决定」行（回答事件仍如实展示）
        wait_lines = [l for l in md.splitlines() if l.startswith("- **等待用户决定**")]
        self.assertEqual(len(wait_lines), 1)
        self.assertIn("IX-2", wait_lines[0])
        self.assertNotIn("IX-1", wait_lines[0])
        proj = load_projection(run)
        self.assertEqual(proj["professional"]["status"], "unrecorded")
        self.assertIn("专业配置：未采集", render_state_markdown(state))


class SnapshotImmutabilityTests(_Base):
    """B：快照按内容哈希不可变保存；旧布局兼容可读。"""

    def test_revision_with_resource_change_keeps_v1_complete(self):
        reg = self._registry()
        run = self.root / "b1"
        manager = self._manager(run, reg)
        brief1 = SimpleNamespace(version=1, objective="旧目标")
        cfg1, ctx1, _ = manager.ensure(brief=brief1)
        v1_json = (config_dir(run) / "config-v1.json").read_bytes()
        v1_snap_path = config_dir(run) / cfg1.grounds[0]["snapshot"]
        v1_bytes = v1_snap_path.read_bytes()
        v1_bindings = load_bindings(run)
        (reg / "web-landing.md").write_text(RESOURCE_V2, encoding="utf-8")
        brief2 = SimpleNamespace(version=2, objective="新目标")
        cfg2, ctx2, _ = manager.ensure(brief=brief2)
        # 旧配置 JSON、旧快照、旧绑定证据字节不变
        self.assertEqual((config_dir(run) / "config-v1.json").read_bytes(), v1_json)
        self.assertEqual(v1_snap_path.read_bytes(), v1_bytes)
        self.assertEqual(load_bindings(run)[:len(v1_bindings)], v1_bindings)
        # 新旧版本分别可核验、可恢复，规则内容各自正确
        restored1 = self._manager(run, reg)._load_and_verify(1)
        restored2 = self._manager(run, reg)._load_and_verify(2)
        self.assertEqual(restored1.grounds[0]["content_sha256"],
                         hashlib.sha256(v1_bytes).hexdigest())
        rules2 = [r.rule_id for s in ctx2.selections for r in s.rules_loaded]
        self.assertEqual(rules2, ["WA-1"])
        # 两个版本引用不同快照路径（内容不同→不同文件）
        self.assertNotEqual(cfg1.grounds[0]["snapshot"], cfg2.grounds[0]["snapshot"])

    def test_revision_without_resource_change_shares_immutable_snapshot(self):
        reg = self._registry()
        run = self.root / "b2"
        manager = self._manager(run, reg)
        cfg1, _, _ = manager.ensure(brief=SimpleNamespace(version=1, objective="目标甲"))
        before = (config_dir(run) / cfg1.grounds[0]["snapshot"]).read_bytes()
        cfg2, _, _ = manager.ensure(brief=SimpleNamespace(version=2, objective="目标乙"))
        # 内容相同 → 同一内容哈希文件；字节不变（幂等，不重写历史）
        self.assertEqual(cfg1.grounds[0]["snapshot"], cfg2.grounds[0]["snapshot"])
        self.assertEqual((config_dir(run) / cfg1.grounds[0]["snapshot"]).read_bytes(),
                         before)
        self.assertEqual(len(list((config_dir(run) / "resources").iterdir())), 1)

    def test_legacy_flat_layout_readable_without_rewrite(self):
        """已有可核验旧格式（按文件名布局）保持可读；读取不改写历史文件。"""
        reg = self._registry()
        run = self.root / "b3"
        manager = self._manager(run, reg)
        cfg, _, _ = manager.ensure(brief=SimpleNamespace(version=1, objective="旧目标"))
        # 手工把该版本 grounds 改回旧布局（模拟 C1 早期运行），并保持哈希一致
        cfg_path = config_dir(run) / "config-v1.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        legacy_rel = "resources/web-landing.md"
        (config_dir(run) / legacy_rel).parent.mkdir(parents=True, exist_ok=True)
        (config_dir(run) / legacy_rel).write_text(RESOURCE_V1, encoding="utf-8")
        if data["grounds"][0]["snapshot"] != legacy_rel:
            (config_dir(run) / data["grounds"][0]["snapshot"]).unlink()
        data["grounds"][0]["snapshot"] = legacy_rel
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        cfg_path.write_bytes(payload)
        # 绑定行期望的是“当前字节”的哈希：更新绑定以匹配旧布局内容本身可核验
        # （此处直接验证：读取按记录路径解析，不按文件名猜目录）
        sha = hashlib.sha256(payload).hexdigest()
        rows = load_bindings(run)
        with (config_dir(run) / "bindings.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                if r.get("event") == "bind":
                    r["config_sha256"] = sha
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        manager2 = self._manager(run, reg)
        restored = manager2._load_and_verify(1)
        self.assertEqual(restored.grounds[0]["snapshot"], legacy_rel)
        legacy_bytes = (config_dir(run) / legacy_rel).read_bytes()
        view = load_professional_view(run)
        self.assertEqual(view["status"], "recorded")
        # 读取不改写历史文件
        self.assertEqual((config_dir(run) / legacy_rel).read_bytes(), legacy_bytes)
        self.assertEqual((config_dir(run) / "config-v1.json").read_bytes(), payload)


class CachedVerificationTests(_Base):
    """C：同一实例每次 ensure 都核验；失败后相关委托 0 次。"""

    def test_cached_reuse_verifies_config_snapshot_bindings(self):
        reg = self._registry()
        run = self.root / "c1"
        manager = self._manager(run, reg)
        manager.ensure(brief=SimpleNamespace(version=1, objective="目标"))
        probe = SimpleNamespace(version=1, objective="目标")
        # 基线：两边都接受
        self._manager(run, reg).ensure(brief=probe)
        manager.ensure(brief=probe)
        # 配置字节被改
        (config_dir(run) / "config-v1.json").write_bytes(
            (config_dir(run) / "config-v1.json").read_bytes() + b" ")
        with self.assertRaises(ConfigPersistenceError):
            manager.ensure(brief=probe)
        # 重建场景：快照被改
        run2 = self.root / "c2"
        manager2 = self._manager(run2, self._registry("reg2"))
        manager2.ensure(brief=probe)
        snap = config_dir(run2) / manager2._config.grounds[0]["snapshot"]
        snap.write_text("被篡改\n", encoding="utf-8")
        with self.assertRaises(ConfigPersistenceError):
            manager2.ensure(brief=probe)
        # 绑定被删
        run3 = self.root / "c3"
        manager3 = self._manager(run3, self._registry("reg3"))
        manager3.ensure(brief=probe)
        (config_dir(run3) / "bindings.jsonl").unlink()
        with self.assertRaises(ConfigPersistenceError):
            manager3.ensure(brief=probe)

    def test_verification_failure_yields_zero_fake_delegations(self):
        """Fake 执行器证明：核验失败后，委托入口不发出任何执行器调用。"""
        base = self.root / "collab-runs"
        collab = make_collab(base_dir=base)
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        prompts_before = len(collab.adapter.calls)
        self.assertGreater(prompts_before, 0)
        # 篡改已冻结配置的字节 → 委托入口必须拒绝
        cfg_path = config_dir(collab.run_dir) / "config-v1.json"
        cfg_path.write_bytes(cfg_path.read_bytes() + b" ")
        with self.assertRaises(ConfigPersistenceError):
            collab._dispatch_codex("review-fix-x", "测试：这不应发出")
        self.assertEqual(len(collab.adapter.calls), prompts_before)  # 0 次新增
        # 快照篡改同样拒绝（同一实例缓存复用路径）
        collab2 = make_collab(base_dir=self.root / "collab-runs-2")
        collab2.run()
        prompts2 = len(collab2.adapter.calls)
        snap = config_dir(collab2.run_dir) / collab2.professional._config.grounds[0]["snapshot"]
        snap.write_text("篡改\n", encoding="utf-8")
        with self.assertRaises(ConfigPersistenceError):
            collab2._dispatch_codex("review-fix-y", "测试：这也不应发出")
        self.assertEqual(len(collab2.adapter.calls), prompts2)


class ColdRestoreTests(_Base):
    """D：冷恢复第一次 ensure 即使用当前条件完成改版。"""

    def test_first_ensure_after_restore_uses_new_goal(self):
        reg = self._registry()
        run = self.root / "d1"
        self._manager(run, reg).ensure(brief=SimpleNamespace(version=1, objective="旧目标"))
        cold = self._manager(run, reg)
        cfg, ctx, _ = cold.ensure(brief=SimpleNamespace(version=2, objective="新目标"))
        self.assertEqual(cfg.version, 2)
        self.assertEqual(cfg.goal, "新目标")
        self.assertEqual(cfg.reason_kind, "goal_change")
        # 第二次调用稳定在 v2（不依赖第二次调用纠错）
        cfg_again, _, _ = cold.ensure(brief=SimpleNamespace(version=2, objective="新目标"))
        self.assertEqual(cfg_again.version, 2)
        # 阶段与调用关联引用新配置
        self.assertEqual(cold.binding("plan")["version"], 2)
        self.assertEqual(cold.binding("reverify")["sha256"], cfg.sha256)
        st = TraceWriter(run, "fix-d").stage_tracker()
        st.start("diagnose", config=cold.binding("diagnose"))
        rows = [json.loads(l) for l in
                (run / "trace" / "stages.jsonl").read_text(encoding="utf-8").splitlines()]
        # 上面 stage_tracker 用了新 writer；行属新文件——校验 config 版本为 2
        self.assertEqual(rows[-1]["config"]["version"], 2)
        kinds = [e.get("kind") for e in load_events(run)]
        self.assertEqual(kinds.count("professional_config.revised"), 1)
        # 旧计划（goal 不匹配）不再被静默复用：绑定记录含新版本
        self.assertIn("professional_config.revised", kinds)

    def test_restored_context_rules_come_from_bound_snapshot(self):
        reg = self._registry()
        run = self.root / "d2"
        self._manager(run, reg).ensure(brief=SimpleNamespace(version=1, objective="旧目标"))
        (reg / "web-landing.md").write_text(RESOURCE_V2, encoding="utf-8")
        cold = self._manager(run, reg)
        cfg, ctx, _ = cold.ensure(brief=SimpleNamespace(version=2, objective="新目标"))
        # 改版重新快照当前内容（记录在案的新版本），规则可从新快照恢复
        # （期望值取注册表文件实际字节——Windows 文本写入会做换行转换）
        self.assertEqual(cfg.grounds[0]["content_sha256"],
                         hashlib.sha256((reg / "web-landing.md").read_bytes()).hexdigest())
        self.assertEqual([r.rule_id for s in ctx.selections for r in s.rules_loaded],
                         ["WA-1"])
        # 旧版本仍可核验
        self._manager(run, reg)._load_and_verify(1)


if __name__ == "__main__":
    unittest.main()
