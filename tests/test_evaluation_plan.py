"""P2.4-V1 回归例：评估计划先行（全部离线；模型响应一律为 Mock/Fake，已标注）。

覆盖（V1 验证要求）：
- 四层来源组装（内核/用户/领域/任务实例），逐条来源可追溯；
- provisional 与 hypothesis 不阻塞交付；
- 计划先于委托：落盘失败时执行器零调用；
- 硬要求读计划：咖啡试点判定回归不变，异事实夹具（不同活动事实）判定正确；
- brief 改版后计划改版（价格条目失效更新），旧版本文件与旧结果不改写；
- 模型虚构来源被拒收并留痕；
- 无凭据时降级为纯确定性组装并如实标注；
- trace 时序：计划事件先于首个委托事件，委托事件引用计划哈希。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mentis.collab import COPY_DECK, Collaboration
from mentis.contracts import Brief, Provenance, SourcedItem
from mentis.evaluation_plan import (EvaluationPlan, PlanPersistenceError,
                                    build_evaluation_plan, derive_task_facts,
                                    execute_structural_check, find_external_refs,
                                    instantiate_domain_rules_with_model, revise_plan)
from mentis.observation import observe_structure

try:
    from tests.test_collab import BAD_PAGE, GOOD_PAGE, FakeAdapter, stub_capture
    from tests.test_guidance import FakeGateway
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import BAD_PAGE, GOOD_PAGE, FakeAdapter, stub_capture
    from test_guidance import FakeGateway

GOAL = "为拾光咖啡工作坊制作活动介绍页"

# 异事实夹具：与试点素材同格式、事实不同（合成素材，虚构品牌）
BOOK_DECK = """# 青梧书店 · 周日绘本共读（合成素材，虚构品牌）

- 活动名称：周日绘本共读
- 日期时间：2027-01-09（周日）10:30—11:30
- 地点：青梧书店 · 梧桐北路店（演示地址：梧桐北路 88 号）
- 价格：¥45/人，限 12 组家庭
- 报名方式：留下家长姓名和联系电话（本地演示，不发送任何数据）
- 氛围要求：安静、亲子友好。
"""
BOOK_GOOD_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>青梧书店 · 周日绘本共读</title></head><body>
<h1>周日绘本共读</h1>
<p>时间：2027-01-09（周日）10:30—11:30</p>
<p>地点：青梧书店 · 梧桐北路店</p>
<p>价格：¥45/组家庭</p>
<form id="signup"><input name="parent" required><input name="phone" required>
<button type="submit">报名</button></form>
</body></html>
"""
BOOK_BAD_PAGE = BOOK_GOOD_PAGE.replace("<p>价格：¥45/组家庭</p>\n", "")


def make_brief(version=1, objective=GOAL, constraints=(), preferences=()):
    return Brief(brief_id=f"brief-t{version}", version=version, objective=objective,
                 constraints=list(constraints), preferences=list(preferences),
                 created_at="2026-10-02T00:00:00")


SYSTEM_CONSTRAINT = SourcedItem(
    "本地单页、无网络、不发送真实数据；改动限于隔离工作区",
    Provenance.SYSTEM, "system-preset:isolation-no-network")
STYLE_PREF = SourcedItem(
    "温暖、简洁，像朋友聚会（暂定默认，可被用户反馈覆盖）",
    Provenance.PROVISIONAL, "default:adr002")


def load_web_resources():
    from mentis.capabilities.registry import load_registry
    from mentis.capabilities.selection import select_for_task

    _, resources = select_for_task(
        load_registry(), task_label="评估计划", task_keywords=["单页活动介绍页"],
        decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
        observation_capabilities={"structure_only": True, "screenshots": True},
        can_execute=True)
    return resources


class AssemblyTests(unittest.TestCase):
    """四层来源组装与维度字段完整性。"""

    def setUp(self):
        self.resources = load_web_resources()
        self.brief = make_brief(constraints=[SYSTEM_CONSTRAINT], preferences=[STYLE_PREF])

    def build(self, material=COPY_DECK, **kw):
        return build_evaluation_plan(goal=GOAL, brief=self.brief, material_text=material,
                                     resources=self.resources, **kw)

    def test_four_layers_present_with_sources(self):
        plan = self.build()
        layers = {e.source_layer for e in plan.entries}
        self.assertEqual(layers, {"core", "user", "domain", "task_instance"})
        for entry in plan.entries:
            # 每个维度至少写明：含义/类别/来源/观察/检查方式/通过线/阻塞/处置
            for field in ("meaning", "category", "source_refs", "observation",
                          "check_method", "pass_line"):
                self.assertTrue(getattr(entry, field), entry.entry_id)
            self.assertTrue(entry.disposition or entry.check_method == "deterministic",
                            entry.entry_id)
            self.assertIn(entry.category, ("hard_requirement", "professional_advice",
                                           "user_preference", "hypothesis"))
            self.assertIn(entry.check_method, ("deterministic", "pairwise_blind",
                                               "user_judgment", "unverifiable"))
        # 内核来源
        core_ids = {e.entry_id for e in plan.entries if e.source_layer == "core"}
        self.assertTrue(any("约束" in e.name for e in plan.entries if e.entry_id in core_ids))
        self.assertTrue(any("无回归" in e.name for e in plan.entries if e.entry_id in core_ids))
        # 用户来源：沿用 brief 条目的 provenance/source_ref
        user_entry = next(e for e in plan.entries
                          if e.source_layer == "user" and e.category == "hard_requirement")
        self.assertIn("system-preset:isolation-no-network", "；".join(user_entry.source_refs))
        self.assertIn("provenance:system", "；".join(user_entry.source_refs))
        # 领域来源：id@版本 + 规则性质；假设规则聚合条目列明规则 id
        domain_refs = "；".join(r for e in plan.entries if e.source_layer == "domain"
                                for r in e.source_refs)
        self.assertIn("web-visual-expression@0.1-draft VE-1", domain_refs)
        self.assertIn("rule-nature:hard_constraint", domain_refs)
        hyp_entry = next(e for e in plan.entries if e.category == "hypothesis")
        self.assertIn("IH-6", "；".join(hyp_entry.source_refs))
        self.assertIn("VE-6", "；".join(hyp_entry.source_refs))
        # 任务实例来源：素材推导的具体事实
        t1 = plan.entry("T1")
        self.assertEqual(t1.check_spec["needles"],
                         {"活动名称": "周六手冲入门工作坊", "日期时间": "2026-11-14",
                          "地点": "梧桐街店", "价格": "¥99"})
        self.assertIn("task-material:input/copy.md", "；".join(t1.source_refs))
        t2 = plan.entry("T2")
        self.assertEqual(t2.check_spec["kind"], "form_fields_present")
        self.assertEqual(t2.check_spec["required_named"], ["姓名", "联系方式"])
        # 计划级字段
        self.assertTrue(plan.tradeoffs and plan.stop_conditions and plan.out_of_scope)

    def test_provisional_and_hypothesis_never_block(self):
        plan = self.build(gateway=FakeGateway([]), use_model=True)  # Fake/Mock 响应，已标注
        model_entries = [e for e in plan.entries if e.provisional]
        self.assertTrue(model_entries, "模型实例化条目应存在（Mock 响应）")
        for entry in model_entries:
            self.assertTrue(entry.agent_selected)
            self.assertFalse(entry.blocking, entry.entry_id)
        hypothesis = [e for e in plan.entries if e.category == "hypothesis"]
        self.assertTrue(hypothesis)
        for entry in hypothesis:
            self.assertFalse(entry.blocking, entry.entry_id)
            self.assertIn("不作为修改依据", entry.pass_line + entry.disposition + entry.meaning)
        # 计划元数据标注 Mock 调用
        self.assertTrue(plan.model_instantiation.get("mock"))

    def test_plan_is_short_and_result_not_implementation(self):
        plan = self.build()
        self.assertLessEqual(len(plan.entries), 12)
        t1 = plan.entry("T1")
        self.assertNotIn("字号", t1.pass_line)
        self.assertNotIn("px", t1.pass_line.replace("4.5:1", ""))


class FactsAndCheckTests(unittest.TestCase):
    """任务事实推导与结构阶段确定性检查。"""

    def test_derive_coffee_facts(self):
        derived = derive_task_facts(COPY_DECK)
        self.assertEqual(derived["needles"]["价格"], "¥99")
        self.assertEqual(derived["needles"]["地点"], "梧桐街店")
        self.assertEqual(derived["form_fields"], ["姓名", "联系方式"])

    def test_brief_revision_updates_price(self):
        v2 = make_brief(version=2, preferences=[
            SourcedItem("用户修正：价格从 ¥99 改为 ¥129", Provenance.USER_SELECTED, "user-feedback")])
        derived = derive_task_facts(COPY_DECK, [v2])
        self.assertEqual(derived["needles"]["价格"], "¥129")
        self.assertTrue(derived["price_updates"])

    def test_external_ref_detection(self):
        self.assertEqual(find_external_refs(GOOD_PAGE), [])
        dirty = GOOD_PAGE.replace("</head>",
                                  '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head>')
        self.assertTrue(find_external_refs(dirty))


class HardRequirementReadsPlanTests(unittest.TestCase):
    """硬要求检查读计划：试点回归不变 + 异事实夹具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def make(self, material=COPY_DECK, **kw):
        defaults = dict(goal=GOAL, adapter=FakeAdapter(), gateway=None,
                        browser_capture=stub_capture, use_model=False,
                        interactive=False, base_dir=self.base, material_text=material)
        defaults.update(kw)
        return Collaboration(**defaults)

    def test_coffee_pilot_regression_unchanged(self):
        """试点判定回归不变：好页全过，缺价格的坏页阻塞在价格。"""
        good = self.make()
        self.assertEqual(good.run(), "DELIVERED")
        bad = self.make(adapter=FakeAdapter(behavior={"first": "bad", "second": "bad"}),
                        max_repairs=1)
        self.assertEqual(bad.run(), "NOT_DELIVERED")
        self.assertIn("价格", bad.session.reviews[-1].findings.__str__())
        self.assertEqual(bad.repairs_used, 1)

    def test_different_facts_fixture_judged_correctly(self):
        """异事实夹具：书店活动（¥45/2027-01-09/梧桐北路店）同样正确判定。"""
        good = self.make(material=BOOK_DECK,
                         adapter=FakeAdapter(behavior={"first": "good"}))
        # FakeAdapter 写的是咖啡页；手工写入书店好页后再检查
        good.prepare_workspace()
        plan = good._ensure_evaluation_plan()
        self.assertEqual(plan.fact_needles()["价格"], "¥45")
        self.assertEqual(plan.fact_needles()["地点"], "梧桐北路店")
        self.assertEqual(plan.fact_needles()["日期时间"], "2027-01-09")
        (good.workspace / "index.html").write_text(BOOK_GOOD_PAGE, encoding="utf-8")
        findings, blocking = good.hard_requirement_review(
            observe_structure(str(good.workspace / "index.html")))
        self.assertEqual(blocking, [], findings)
        (good.workspace / "index.html").write_text(BOOK_BAD_PAGE, encoding="utf-8")
        findings, blocking = good.hard_requirement_review(
            observe_structure(str(good.workspace / "index.html")))
        self.assertEqual(blocking, ["价格"])
        # 咖啡页对书店计划不合格（事实不同）
        (good.workspace / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
        _, blocking = good.hard_requirement_review(
            observe_structure(str(good.workspace / "index.html")))
        self.assertIn("价格", blocking)
        self.assertIn("活动名称", blocking)

    def test_no_external_refs_entry_executes(self):
        collab = self.make()
        collab.prepare_workspace()
        collab._ensure_evaluation_plan()
        (collab.workspace / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
        _, blocking = collab.hard_requirement_review(
            observe_structure(str(collab.workspace / "index.html")))
        self.assertEqual(blocking, [])
        dirty = GOOD_PAGE.replace(
            "</head>", '<script src="https://cdn.example.com/x.js"></script></head>')
        (collab.workspace / "index.html").write_text(dirty, encoding="utf-8")
        findings, blocking = collab.hard_requirement_review(
            observe_structure(str(collab.workspace / "index.html")))
        self.assertTrue(any("外部引用" in f for f in findings), findings)
        self.assertTrue(blocking)


class OrderingAndRevisionTests(unittest.TestCase):
    """计划先于委托、落盘失败零调用、brief 改版改版与旧结果不改写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def make(self, **kw):
        defaults = dict(goal=GOAL, adapter=FakeAdapter(), gateway=None,
                        browser_capture=stub_capture, use_model=False,
                        interactive=False, base_dir=self.base)
        defaults.update(kw)
        return Collaboration(**defaults)

    @staticmethod
    def _events(collab):
        from mentis.trace import load_events

        return load_events(collab.run_dir)

    def test_plan_precedes_delegation_and_prompt_carries_items(self):
        collab = self.make()
        self.assertEqual(collab.run(), "DELIVERED")
        events = self._events(collab)
        kinds = [(e["kind"], e["seq"]) for e in events]
        plan_seq = next(seq for kind, seq in kinds if kind == "evaluation_plan.recorded")
        delegation_seq = next(seq for kind, seq in kinds if kind == "delegation.prepared")
        self.assertLess(plan_seq, delegation_seq)
        delegation = next(e for e in events if e["kind"] == "delegation.prepared")
        plan_refs = [r for r in delegation["refs"] if r.get("role") == "plan"]
        self.assertEqual(len(plan_refs), 1)
        self.assertTrue(plan_refs[0]["sha256"])
        on_disk = (collab.run_dir / "evaluation-plan" / "plan-v1.json").read_bytes()
        import hashlib

        self.assertEqual(hashlib.sha256(on_disk).hexdigest(), plan_refs[0]["sha256"])
        # 验收项写入确切指令（归档 prompt = 实际发送）
        prompts = sorted((collab.run_dir / "trace" / "prompts").glob("*.md"))
        self.assertTrue(prompts)
        prompt = prompts[0].read_text(encoding="utf-8")
        self.assertIn("[本轮验收项", prompt)
        self.assertIn("T1 任务关键事实完整呈现", prompt)
        self.assertIn(plan_refs[0]["sha256"][:12], prompt)
        # 报告"看依据"列出计划与来源
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("评估计划：v1", report)
        self.assertIn("task-material:input/copy.md", report)

    def test_plan_write_failure_zero_executor_calls(self):
        collab = self.make()
        collab.prepare_workspace()
        # 占位为同名文件：计划目录无法创建 → 落盘失败 → 委托不发起
        (collab.run_dir / "evaluation-plan").write_text("blocked", encoding="utf-8")
        with self.assertRaises(PlanPersistenceError):
            collab.run()
        self.assertEqual(collab.adapter.calls, [])
        self.assertEqual(collab.status, "ERROR")
        kinds = [e["kind"] for e in self._events(collab)]
        self.assertNotIn("delegation.prepared", kinds)
        self.assertNotIn("evaluation_plan.recorded", kinds)  # 落盘失败不记计划事件

    def test_brief_revision_revises_plan_keeps_old_version(self):
        class PriceRevisionAdapter(FakeAdapter):
            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                if len(self.calls) >= 2:  # 修订轮写 ¥129 页
                    Path(spec.workspace).joinpath("index.html").write_text(
                        GOOD_PAGE.replace("¥99", "¥129"), encoding="utf-8")
                return result

        collab = self.make(adapter=PriceRevisionAdapter(behavior={"first": "good"}),
                           gateway=FakeGateway([]), use_model=True)  # Mock 响应，已标注
        self.assertEqual(collab.run(), "DELIVERED")
        plan_dir = collab.run_dir / "evaluation-plan"
        v1_bytes = (plan_dir / "plan-v1.json").read_bytes()
        self.assertIsNone(json.loads(v1_bytes)["revision"] or None)
        v1_review = json.loads(
            (collab.run_dir / "professional-review.json").read_text(encoding="utf-8"))
        self.assertEqual(v1_review["evaluation_plan"]["version"], 1)

        status = collab.resume(feedback="价格从 ¥99 改为 ¥129，其余不变")
        self.assertEqual(status, "DELIVERED")
        # 旧版本文件不改写；新版本带原因与受影响条目
        self.assertEqual((plan_dir / "plan-v1.json").read_bytes(), v1_bytes)
        v2_data = json.loads((plan_dir / "plan-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(v2_data["revision"]["reason_kind"], "brief_revision")
        self.assertIn("¥99", "；".join(v2_data["revision"]["affected_entries"]))
        self.assertIn("¥129", "；".join(v2_data["revision"]["affected_entries"]))
        v2 = EvaluationPlan.from_dict(v2_data)
        self.assertEqual(v2.fact_needles()["价格"], "¥129")
        # 新评审记录绑定 v2；trace 追加事件中两轮各自带版本，不改写
        v2_review = json.loads(
            (collab.run_dir / "professional-review.json").read_text(encoding="utf-8"))
        self.assertEqual(v2_review["evaluation_plan"]["version"], 2)
        events = self._events(collab)
        revisions = [e for e in events if e["kind"] == "evaluation_plan.revised"]
        self.assertEqual(len(revisions), 1)
        self.assertIn("brief 改版", revisions[0]["summary"])
        review_events = [e for e in events if e["kind"] == "review.recorded"
                         and "依据评估计划" in e["summary"]]
        self.assertIn("v1", review_events[0]["summary"])
        self.assertIn("v2", review_events[-1]["summary"])

    def test_goal_change_revises_plan(self):
        collab = self.make()
        self.assertEqual(collab.run(), "DELIVERED")
        status = collab.resume(goal_new="改做个人作品展示页（同样是本地单页）")
        self.assertEqual(status, "DELIVERED")
        v2 = json.loads((collab.run_dir / "evaluation-plan" / "plan-v2.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(v2["revision"]["reason_kind"], "goal_change")
        self.assertEqual(v2["goal"], "改做个人作品展示页（同样是本地单页）")

    def test_restore_keeps_latest_plan_without_rewrite(self):
        collab = self.make()
        self.assertEqual(collab.run(), "DELIVERED")
        v1_bytes = (collab.run_dir / "evaluation-plan" / "plan-v1.json").read_bytes()
        from mentis.collab import collaboration_from_state

        restored = collaboration_from_state(collab.run_dir / "state.json")
        self.assertIsNotNone(restored.evaluation_plan)
        self.assertEqual(restored.evaluation_plan.version, 1)
        self.assertEqual(restored.evaluation_plan.sha256,
                         __import__("hashlib").sha256(v1_bytes).hexdigest())
        # 恢复后无需改版即可继续（不重复生成 v2）
        plan = restored._ensure_evaluation_plan()
        self.assertEqual(plan.version, 1)
        self.assertFalse((collab.run_dir / "evaluation-plan" / "plan-v2.json").exists())


class ModelInstantiationTests(unittest.TestCase):
    """模型实例化：虚构来源拒收留痕；无凭据降级；确定性建议降级。"""

    def setUp(self):
        self.resources = load_web_resources()
        self.brief = make_brief(constraints=[SYSTEM_CONSTRAINT])

    def test_fabricated_source_rejected_and_recorded(self):
        fabricated = json.dumps({"entries": [
            {"name": "真实来源条目", "meaning": "引用已加载规则",
             "category": "professional_advice", "source_refs": ["IH-4"],
             "observation": "截图", "check_method": "pairwise_blind",
             "pass_line": "首屏四问可回答", "disposition": "不阻塞"},
            {"name": "虚构来源条目", "meaning": "引用不存在的规则",
             "category": "professional_advice", "source_refs": ["XX-99（自创标准）"],
             "observation": "截图", "check_method": "user_judgment",
             "pass_line": "（虚构）", "disposition": "不阻塞"},
        ]}, ensure_ascii=False)

        class ScriptedGateway:
            config = SimpleNamespace(model="mock-model")

            def has_credentials(self):
                return True

            def complete(self, system, user_prompt, json_mode=True, mock=False, images=None):
                return SimpleNamespace(ok=True, mock=True, model="mock-model",
                                       usage={"total_tokens": 1}, error="",
                                       response_text=fabricated, image_refs=[],
                                       request_id="fake")

        entries, meta = instantiate_domain_rules_with_model(
            ScriptedGateway(), GOAL, self.brief, COPY_DECK, self.resources)
        self.assertEqual(len(entries), 1)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(len(meta["rejected"]), 1)
        self.assertIn("虚构来源", meta["rejected"][0]["reason"])
        self.assertIn("XX-99", meta["rejected"][0]["reason"])
        for entry in entries:
            self.assertTrue(entry.provisional and not entry.blocking)

    def test_model_deterministic_suggestion_downgraded(self):
        response = json.dumps({"entries": [
            {"name": "声称确定性", "meaning": "模型自称可确定性检查",
             "category": "hard_requirement", "source_refs": ["BI-2"],
             "observation": "产物", "check_method": "deterministic",
             "pass_line": "无溢出", "disposition": "阻塞"},
        ]}, ensure_ascii=False)

        class ScriptedGateway:
            config = SimpleNamespace(model="mock-model")

            def has_credentials(self):
                return True

            def complete(self, system, user_prompt, json_mode=True, mock=False, images=None):
                return SimpleNamespace(ok=True, mock=True, model="mock-model",
                                       usage={}, error="", response_text=response,
                                       image_refs=[], request_id="fake")

        entries, meta = instantiate_domain_rules_with_model(
            ScriptedGateway(), GOAL, self.brief, COPY_DECK, self.resources)
        self.assertEqual(entries[0].check_method, "unverifiable")
        self.assertFalse(entries[0].blocking)
        self.assertIn("降级", entries[0].note)

    def test_no_credentials_degrades(self):
        class NoCredGateway:
            config = SimpleNamespace(model="mock-model")

            def has_credentials(self):
                return False

        entries, meta = instantiate_domain_rules_with_model(
            NoCredGateway(), GOAL, self.brief, COPY_DECK, self.resources)
        self.assertEqual(entries, [])
        self.assertEqual(meta["status"], "degraded")
        self.assertIn("无凭据", meta["note"])
        # 组装不阻塞：计划照常生成并如实标注
        plan = build_evaluation_plan(goal=GOAL, brief=self.brief, material_text=COPY_DECK,
                                     resources=self.resources, gateway=NoCredGateway(),
                                     use_model=True)
        self.assertEqual(plan.model_instantiation["status"], "degraded")
        self.assertTrue(plan.entries)

    def test_gateway_failure_degrades(self):
        class FailingGateway:
            config = SimpleNamespace(model="mock-model")

            def has_credentials(self):
                return True

            def complete(self, system, user_prompt, json_mode=True, mock=False, images=None):
                return SimpleNamespace(ok=False, mock=False, model="mock-model",
                                       usage={}, error="HTTP 500", response_text="",
                                       image_refs=[], request_id="fake")

        plan = build_evaluation_plan(goal=GOAL, brief=self.brief, material_text=COPY_DECK,
                                     resources=self.resources, gateway=FailingGateway(),
                                     use_model=True)
        self.assertEqual(plan.model_instantiation["status"], "degraded")
        self.assertIn("模型调用失败", plan.model_instantiation["note"])


class ReviseReasonGuardTests(unittest.TestCase):
    def test_invalid_reason_rejected(self):
        plan = build_evaluation_plan(goal=GOAL, brief=make_brief(),
                                     material_text=COPY_DECK)
        with self.assertRaisesRegex(ValueError, "原因不合法"):
            revise_plan(previous=plan, brief=make_brief(), material_text=COPY_DECK,
                        reason_kind="看到产物后想改", detail="不允许")


if __name__ == "__main__":
    unittest.main()
