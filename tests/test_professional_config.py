"""P2.4-C1 回归例：最小任务专业配置冻结（全部离线，真实模型/执行器调用 0）。

验收映射（LOCAL_PRODUCT_ROADMAP §2/§3 C1 退出门槛的离线部分）：
- 配置先落盘：首次使用前冻结 v1（一次选择+资源快照+绑定行+事件），失败 fail-closed；
- 同任务阶段同一有效版本：计划/诊断取同一配置与同一份快照规则；
- 更新有原因、旧版本不改写：brief/目标变化 → v2（reason_kind 记录原因），v1 字节不变；
- 注册表后变不静默换内容：漂移记一次事件，继续使用快照；
- 恢复/复用：新实例从运行目录读回配置并核验；快照/配置/绑定被篡改 → 不声称已绑定，
  依赖配置的委托不发起（ConfigPersistenceError，collab 计划资源路径不吞掉该错误）；
- 盲评隔离：盲比输入只有计划标准与观察，配置只留关联信息（版本/哈希/职责）；
- 投影/状态/交流/页面共用读取：recorded/unverified/unrecorded 展示标签，历史运行不回填。
本文件全部为离线夹具（临时注册表、Fake/Mock 网关），不构成真实模型证据。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mentis.trace import TraceWriter, load_events, render_conversation

try:
    from tests.test_eval_review import make_plan
    from tests.test_guidance import FakeGateway
except ImportError:  # discover -s tests 以顶层模块导入
    from test_eval_review import make_plan
    from test_guidance import FakeGateway

from mentis.professional import (BINDING_SCHEMA, CONFIG_SCHEMA, ConfigManager,
                                 ConfigPersistenceError, STAGE_RESPONSIBILITY,
                                 config_dir, list_config_versions, load_bindings)

REGISTRY = {
    "registry_version": "t1", "provenance": "test-draft", "evidence_note": "",
    "resources": [{
        "id": "web-landing", "version": "v0.1-test", "provenance": "test-draft",
        "domain": "web", "applies_to": ["单页活动介绍页"], "excludes": [],
        "decision_questions": ["信息密度", "主操作层级"],
        "observation_requirements": ["screenshots"], "file": "web-landing.md",
    }],
    "declared_conflicts": [],
}

RESOURCE_MD = """<!-- mentis-resource {"status": "pending_human"} -->
# 测试资源（草案）
## 判断依据
- [hard_constraint] WA-1：测试规则一（价格信息不得虚构）
## 正例
- 正例一行
## 反例
- 反例一行
## 反馈模式
- 现象: x | 检查: y | 候选: z
"""


class ProfessionalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reg = self.root / "reg"
        self.reg.mkdir()
        (self.reg / "registry.json").write_text(
            json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
        self.resource = self.reg / "web-landing.md"
        self.resource.write_text(RESOURCE_MD, encoding="utf-8")
        self.run = self.root / "run1"
        self.writer = TraceWriter(self.run, "c1-test")
        self.session = SimpleNamespace()

    def _manager(self, **kw) -> ConfigManager:
        base = dict(run_dir=self.run, session=None, goal="做一页活动介绍页",
                    trace=self.writer, registry_path=self.reg / "registry.json",
                    user_source="c1-test")
        base.update(kw)
        return ConfigManager(**base)

    def _kinds(self) -> list:
        return [e.get("kind") for e in load_events(self.run)]

    # ---- 配置先落盘 ----

    def test_freeze_before_use_writes_config_snapshot_binding_event(self):
        cfg, ctx, resources = self._manager().ensure()
        path = config_dir(self.run) / "config-v1.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], CONFIG_SCHEMA)
        self.assertNotIn("sha256", data)  # 避免循环哈希：文件不含自身哈希
        # 资源快照内容与源一致，哈希记录正确（路径按内容哈希寻址，读配置记录）
        snap = config_dir(self.run) / cfg.grounds[0]["snapshot"]
        self.assertTrue(snap.is_file())
        self.assertEqual(snap.read_bytes(), self.resource.read_bytes())
        ground = data["grounds"][0]
        self.assertEqual(ground["content_sha256"],
                         hashlib.sha256(self.resource.read_bytes()).hexdigest())
        self.assertEqual(ground["review_status"], "pending_human")
        self.assertEqual(ground["source_file"], "web-landing.md")
        # 绑定行 + 事件（refs 绑定配置哈希）
        rows = load_bindings(self.run)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "bind")
        self.assertEqual(rows[0]["config_sha256"], cfg.sha256)
        kinds = self._kinds()
        self.assertIn("professional_config.recorded", kinds)
        event = next(e for e in load_events(self.run)
                     if e.get("kind") == "professional_config.recorded")
        ref = event["refs"][0]
        self.assertEqual(ref["role"], "professional_config")
        self.assertEqual(ref["sha256"], cfg.sha256)

    def test_unwritable_config_dir_fails_closed(self):
        manager = self._manager()
        # 占住 config-v1.json 的路径（目录）：写文件必然失败
        blocked = config_dir(self.run)
        blocked.mkdir(parents=True)
        (blocked / "config-v1.json").mkdir()
        with self.assertRaises(ConfigPersistenceError):
            manager.ensure()
        kinds = self._kinds()
        self.assertNotIn("professional_config.recorded", kinds)  # 未声称已绑定

    # ---- 同任务阶段同一有效版本 ----

    def test_stages_share_same_version_and_snapshot_rules(self):
        manager = self._manager()
        _, ctx1, res1 = manager.ensure()
        plan_binding = manager.binding("plan")
        # 模拟另一阶段（诊断）再次取用
        _, ctx2, res2 = manager.ensure()
        diag_binding = manager.binding("diagnose")
        self.assertEqual(plan_binding["version"], diag_binding["version"])
        self.assertEqual(plan_binding["sha256"], diag_binding["sha256"])
        self.assertNotEqual(plan_binding["stage_responsibility"],
                            diag_binding["stage_responsibility"])
        rules1 = [r.rule_id for s in ctx1.selections for r in s.rules_loaded]
        rules2 = [r.rule_id for s in ctx2.selections for r in s.rules_loaded]
        self.assertEqual(rules1, rules2 == ["WA-1"] and rules1, ["WA-1"])
        self.assertEqual(manager.loaded_resources(), res1)
        self.assertEqual(len(res1), len(res2))

    # ---- 注册表后变：漂移一次、继续用快照 ----

    def test_registry_drift_keeps_snapshot_and_records_once(self):
        manager = self._manager()
        _, ctx_a, _ = manager.ensure()
        self.resource.write_text(RESOURCE_MD + "- [guideline] WA-2：新规则\n",
                                 encoding="utf-8")
        _, ctx_b, _ = manager.ensure()
        _, ctx_c, _ = manager.ensure()
        for ctx in (ctx_b, ctx_c):
            rules = [r.rule_id for s in ctx.selections for r in s.rules_loaded]
            self.assertEqual(rules, ["WA-1"])  # 快照内容，不静默换
        self.assertEqual(self._kinds().count("professional_config.drift"), 1)
        cfg = manager.binding("plan")
        self.assertEqual(cfg["version"], 1)  # 漂移不产生新版本

    # ---- 更新有原因、旧版本不改写 ----

    def test_brief_change_revises_with_reason_keeps_old_bytes(self):
        brief = SimpleNamespace(version=1, objective="做一页活动介绍页")
        manager = self._manager()
        cfg1, _, _ = manager.ensure(brief=brief)
        v1_bytes = (config_dir(self.run) / "config-v1.json").read_bytes()
        brief2 = SimpleNamespace(version=2, objective="做一页活动介绍页（补充预约入口）")
        cfg2, _, _ = manager.ensure(brief=brief2)
        self.assertEqual(cfg2.version, 2)
        self.assertEqual(cfg2.reason_kind, "goal_change")
        self.assertEqual(cfg2.supersedes, 1)
        self.assertEqual((config_dir(self.run) / "config-v1.json").read_bytes(), v1_bytes)
        self.assertIn("professional_config.revised", self._kinds())
        rows = load_bindings(self.run)
        self.assertEqual([r["event"] for r in rows], ["bind", "bind"])
        # 未变化的 brief 继续复用 v2
        cfg3, _, _ = manager.ensure(brief=brief2)
        self.assertEqual(cfg3.version, 2)

    def test_note_plan_binds_config_to_plan_once(self):
        manager = self._manager()
        manager.ensure()
        plan = SimpleNamespace(version=1, sha256="a" * 64)
        self.assertTrue(manager.note_plan(plan))
        self.assertFalse(manager.note_plan(plan))  # 同版计划不重复记
        rows = load_bindings(self.run)
        plan_rows = [r for r in rows if r["event"] == "bind_plan"]
        self.assertEqual(len(plan_rows), 1)
        self.assertEqual(plan_rows[0]["plan_sha256"], "a" * 64)
        self.assertEqual(plan_rows[0]["config_version"], 1)

    # ---- 恢复/复用与 fail-closed 核验 ----

    def test_restore_reuses_config_without_live_registry(self):
        manager = self._manager()
        cfg1, ctx1, _ = manager.ensure()
        # 恢复场景：live 注册表删除（快照仍在运行目录）
        (self.reg / "web-landing.md").unlink()
        (self.reg / "registry.json").unlink()
        manager2 = self._manager()
        cfg2, ctx2, res2 = manager2.ensure()
        self.assertEqual(cfg2.version, 1)
        self.assertEqual(cfg2.sha256, cfg1.sha256)
        self.assertEqual([r.rule_id for s in ctx2.selections for r in s.rules_loaded],
                         ["WA-1"])
        self.assertEqual(len(res2), 1)

    def test_tampered_snapshot_fails_closed(self):
        cfg, _, _ = self._manager().ensure()
        snap = config_dir(self.run) / cfg.grounds[0]["snapshot"]
        snap.write_text("被篡改的内容\n", encoding="utf-8")
        with self.assertRaises(ConfigPersistenceError):
            self._manager().ensure()

    def test_tampered_config_fails_closed(self):
        self._manager().ensure()
        path = config_dir(self.run) / "config-v1.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ConfigPersistenceError):
            self._manager().ensure()

    def test_missing_bindings_fails_closed(self):
        self._manager().ensure()
        (config_dir(self.run) / "bindings.jsonl").unlink()
        with self.assertRaises(ConfigPersistenceError):
            self._manager().ensure()

    def test_collab_plan_resources_do_not_swallow_config_error(self):
        # collab._plan_resources 的宽 except 不得吞掉 ConfigPersistenceError
        from mentis.collab import Collaboration

        obj = Collaboration.__new__(Collaboration)
        obj.session = SimpleNamespace(current_brief=SimpleNamespace(version=1, objective="g"))
        manager = self._manager()
        (config_dir(self.run)).mkdir(parents=True)
        (config_dir(self.run) / "config-v1.json").mkdir()  # 让写入必然失败
        obj.professional = manager
        with self.assertRaises(ConfigPersistenceError):
            obj._plan_resources()

    # ---- 调用关联字段 ----

    def test_model_call_and_stage_rows_carry_config_binding(self):
        writer = TraceWriter(self.root / "run2", "c1-sidecar")
        call = SimpleNamespace(request_id="r-1", model="m", host="", created_at="t",
                               latency_ms=1, ok=True, usage={}, error="", mock=True,
                               http_attempted=False, prompt_sha256="p", image_refs=[])
        binding = {"version": 1, "sha256": "abc", "stage_responsibility": "指导诊断"}
        writer.record_model_call(call, stage="diagnosis", config=binding)
        writer.record_model_call(call, stage="diagnosis")  # 无关联：不留字段
        rows = [json.loads(line) for line in
                (self.root / "run2" / "trace" / "model-calls.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["config_version"], 1)
        self.assertEqual(rows[0]["config_sha256"], "abc")
        self.assertEqual(rows[0]["stage_responsibility"], "指导诊断")
        self.assertNotIn("config_version", rows[1])
        stages = writer.stage_tracker()
        stages.start("diagnose", config=binding)
        stage_rows = [json.loads(line) for line in
                      (self.root / "run2" / "trace" / "stages.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(stage_rows[0]["config"]["version"], 1)
        stages2 = TraceWriter(self.root / "run3", "c1-nobind").stage_tracker()
        stages2.start("diagnose")
        legacy = [json.loads(line) for line in
                  (self.root / "run3" / "trace" / "stages.jsonl")
                  .read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("config", legacy[0])  # 未绑定不留字段，不回填

    def test_binding_requires_frozen_config(self):
        manager = self._manager()
        self.assertIsNone(manager.binding("plan"))  # 未冻结：不填 0、不虚构

    # ---- 盲评隔离 ----

    def test_blind_review_receives_association_not_content(self):
        from mentis.eval_review import run_blind_review

        plan = make_plan()
        def pack(text):
            return SimpleNamespace(
                structure=SimpleNamespace(render_text=lambda: f"[结构] {text}",
                                          sha256=text, source_path="x"),
                measurement=SimpleNamespace(render_text=lambda: "[测量] 溢出 0px"),
                image_names=[], sha256=text)
        gateway = FakeGateway([json.dumps({
            "overall": "X_better",
            "dimensions": [{"entry_id": plan.entries[0].entry_id,
                            "verdict": "X_better", "reason": "x"}]},
            ensure_ascii=False), json.dumps({
            "overall": "Y_better",
            "dimensions": [{"entry_id": plan.entries[0].entry_id,
                            "verdict": "Y_better", "reason": "y"}]},
            ensure_ascii=False)])
        coin = SimpleNamespace(random=lambda: 0.0)
        out = self.root / "blind.json"
        result = run_blind_review(
            gateway, plan, pack("before"), [], pack("after"), [], out,
            use_model=True, rng=coin,
            professional={"version": 2, "sha256": "d" * 64,
                          "stage_responsibility": STAGE_RESPONSIBILITY["reverify"]})
        # 输入只有计划标准与观察：配置规则与诊断内容不得进入盲比提示词
        for call in gateway.calls:
            self.assertNotIn("WA-1", call["prompt"])
            self.assertNotIn("测试规则一", call["prompt"])
        # 结果 JSON 只含关联信息（版本/哈希/职责），不含配置内容
        assoc = result.get("professional_config")
        self.assertEqual(assoc["version"], 2)
        self.assertEqual(assoc["sha256"], "d" * 64)
        self.assertNotIn("grounds", assoc)
        self.assertNotIn("responsibilities", assoc)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["professional_config"]["version"], 2)

    # ---- 投影/交流/状态共用读取 ----

    def test_projection_view_recorded_unverified_unrecorded(self):
        from mentis.run_projection import load_professional_view

        # 未采集
        empty = TraceWriter(self.root / "run-e", "c1-empty")
        view = load_professional_view(self.root / "run-e")
        self.assertEqual(view["status"], "unrecorded")
        self.assertIn("不回填", view["note"])
        del empty
        # recorded
        self._manager().ensure()
        view = load_professional_view(self.run)
        self.assertEqual(view["status"], "recorded")
        self.assertEqual(view["current"]["version"], 1)
        self.assertEqual(view["grounds"][0]["resource_id"], "web-landing")
        # 篡改快照 → unverified（展示标签，不是业务判定）
        snap = config_dir(self.run) / view["grounds"][0]["snapshot"]
        snap.write_text("篡改\n", encoding="utf-8")
        view = load_professional_view(self.run)
        self.assertEqual(view["status"], "unverified")
        self.assertTrue(any("快照" in p for p in view["problems"]))

    def test_projection_markdown_and_conversation_render_config(self):
        from mentis.run_projection import (load_projection, load_run_state,
                                           render_projection_markdown,
                                           render_state_markdown)

        manager = self._manager()
        manager.ensure()
        plan = SimpleNamespace(version=1, sha256="b" * 64)
        manager.note_plan(plan)
        proj = load_projection(self.run)
        self.assertEqual(proj["professional"]["status"], "recorded")
        md = render_projection_markdown(proj)
        self.assertIn("## 专业配置（C1）", md)
        self.assertIn("web-landing@v0.1-test", md)
        self.assertIn("计划关联", md)
        state = load_run_state(self.run)
        self.assertEqual(state["professional"]["status"], "recorded")
        self.assertIn("专业配置：v1", render_state_markdown(state))
        conv = render_conversation(self.run)
        self.assertIn("专业配置（首次使用前冻结）", conv)
        # 历史运行（无配置）在三个视图均如实显示未采集
        bare = TraceWriter(self.root / "run-h", "c1-hist")
        bare.emit("mentis", "goal.recorded", "旧目标")
        proj_h = load_projection(self.root / "run-h")
        self.assertEqual(proj_h["professional"]["status"], "unrecorded")
        self.assertIn("未采集", render_projection_markdown(proj_h))
        self.assertIn("专业配置：未采集",
                      render_state_markdown(load_run_state(self.root / "run-h")))

    # ---- 能力范围不越界 ----

    def test_capability_scope_records_not_claims(self):
        cfg, ctx, _ = self._manager().ensure()
        scope = cfg.capability_scope
        self.assertTrue(scope["has_materials"])
        self.assertIn("未验证", scope["verified_scope"])
        self.assertIn("pending_human",
                      [g["review_status"] for g in cfg.grounds])
        self.assertIn("source_note", cfg.responsibilities)
        self.assertTrue(cfg.responsibilities["unsupported"])  # 明确不支持范围


if __name__ == "__main__":
    unittest.main()
