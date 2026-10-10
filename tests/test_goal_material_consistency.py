"""目标—素材—验收一致性回归（P3.4 审计 #1；全部离线，Fake 执行器）。

审计 20261010 #1：真实首次委托同时要求拿铁上新页与旧手冲工作坊的日期、
价格、报名表单；修订后正文仍保留工作坊内容；诊断的目标一致性候选被映射为
unverifiable 跳过。本文件固化修复后的行为：
- 确定性一致性判定（mismatch/consistent/undetermined，规则可审计）
- mismatch 时素材事实不进计划/委托/预期值，T4 禁现其内容
- 好页判定以「目标+素材一致」为前提（T1/T3 从计划读）
- 目标一致性诊断候选映射到 T3/T4（deterministic → 自主修订），不再 unverifiable
"""

import tempfile
import unittest
from pathlib import Path

from mentis.contracts import AttemptStatus
from mentis.eval_review import map_candidate_to_dimension, route_by_verifiability
from mentis.evaluation_plan import (
    assess_material_goal_consistency,
    build_evaluation_plan,
    execute_structural_check,
)
from mentis.executors.codex_cli import CodexTaskResult

try:
    from tests.test_collab import GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from tests.test_guidance import file_aware_measure
except ImportError:
    from test_collab import GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from test_guidance import file_aware_measure

from mentis.collab import COPY_DECK, Collaboration, build_delegation_prompt

LATTE_GOAL = "为拾光咖啡周末限定的「焦糖苹果拿铁」做一个简洁的上新介绍页（本地单页，含门店与供应时间）"
LATTE_MATERIAL = """# 拾光咖啡 · 焦糖苹果拿铁周末限定（素材）

- 活动名称：焦糖苹果拿铁（周末限定新品）
- 日期时间：2026-10-24 至 2026-10-26（周六至周日 10:00-18:00）
- 地点：梧桐街 12 号 拾光咖啡（临窗吧台）
- 价格：¥38/杯（会员 ¥32）
- 报名方式：到店点单，售完即止，无需报名
"""
LATTE_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>焦糖苹果拿铁｜拾光咖啡周末限定</title></head><body>
<h1>焦糖苹果拿铁 · 周末限定</h1>
<p>2026-10-24 至 2026-10-26（周六至周日 10:00-18:00）供应。</p>
<p>地点：梧桐街 12 号 拾光咖啡（临窗吧台）；¥38/杯（会员 ¥32）。</p>
</body></html>
"""


class _B:
    version = 1
    constraints = []
    preferences = []


class AssessmentTests(unittest.TestCase):
    def test_quoted_mismatch_audit_case(self):
        r = assess_material_goal_consistency(LATTE_GOAL, COPY_DECK)
        self.assertEqual(r["verdict"], "mismatch")
        self.assertEqual(r["goal_subjects"], ["焦糖苹果拿铁"])

    def test_quoted_consistent_when_subject_in_material(self):
        r = assess_material_goal_consistency(LATTE_GOAL, LATTE_MATERIAL)
        self.assertEqual(r["verdict"], "consistent")
        self.assertIn("焦糖苹果拿铁", r["shared"])

    def test_unquoted_workshop_goal_consistent(self):
        r = assess_material_goal_consistency(
            "为拾光咖啡周六手冲工作坊做一个温暖简洁的活动介绍页", COPY_DECK)
        self.assertEqual(r["verdict"], "consistent")

    def test_no_subject_undetermined(self):
        r = assess_material_goal_consistency("做一个页面", "随便一段文字")
        self.assertEqual(r["verdict"], "undetermined")


class PlanAssemblyTests(unittest.TestCase):
    def test_mismatch_drops_material_entries_adds_t3_t4(self):
        plan = build_evaluation_plan(LATTE_GOAL, _B(), COPY_DECK, use_model=False)
        ids = {e.entry_id for e in plan.entries}
        self.assertNotIn("T1", ids)  # 素材事实不进验收
        self.assertNotIn("T2", ids)
        self.assertIn("T3", ids)
        self.assertIn("T4", ids)
        self.assertEqual(plan.material_assessment["verdict"], "mismatch")
        t4 = plan.entry("T4")
        forbidden = t4.check_spec["needles"]
        self.assertIn(forbidden["素材主体"], "周六手冲入门工作坊")
        self.assertEqual(forbidden["素材价格"], "¥99")

    def test_consistent_keeps_t1_t2_and_notes_assessment(self):
        plan = build_evaluation_plan(LATTE_GOAL, _B(), LATTE_MATERIAL, use_model=False)
        ids = {e.entry_id for e in plan.entries}
        self.assertIn("T1", ids)
        self.assertIn("T3", ids)
        self.assertNotIn("T4", ids)
        self.assertEqual(plan.fact_needles()["价格"], "¥38")
        self.assertEqual(plan.entry("T3").check_spec["needles"],
                         {"目标主体「焦糖苹果拿铁」": "焦糖苹果拿铁"})
        # 无报名方式的素材不再强制表单
        self.assertNotIn("T2", ids)

    def test_t4_executor_blocks_workshop_content(self):
        plan = build_evaluation_plan(LATTE_GOAL, _B(), COPY_DECK, use_model=False)
        t4 = plan.entry("T4")
        findings, blocking, _ = execute_structural_check(
            t4, GOOD_PAGE, [], GOOD_PAGE)
        self.assertTrue(blocking, "工作坊内容混入拿铁页必须阻塞")
        findings2, blocking2, _ = execute_structural_check(
            t4, LATTE_PAGE, [], LATTE_PAGE)
        self.assertEqual(blocking2, [])


class PromptAndValuesTests(unittest.TestCase):
    def test_prompt_no_silent_material_fallback(self):
        # 审计发现的放大器：facts 为空时曾静默回退到内置工作坊素材
        prompt = build_delegation_prompt(LATTE_GOAL, "", facts={},
                                         material_status="mismatch")
        self.assertNotIn("周六手冲入门工作坊", prompt)
        self.assertNotIn("2026-11-14", prompt)
        self.assertNotIn("¥99", prompt)
        self.assertNotIn("报名表单（姓名与联系方式必填校验", prompt)
        self.assertIn("[素材声明]", prompt)

    def test_prompt_keeps_facts_when_consistent(self):
        prompt = build_delegation_prompt(
            LATTE_GOAL, "",
            facts={"活动名称": "焦糖苹果拿铁（周末限定新品）", "价格": "¥38"},
            material_status="consistent")
        self.assertIn("焦糖苹果拿铁（周末限定新品）", prompt)
        self.assertIn("¥38", prompt)
        self.assertIn("input/copy.md", prompt)


class CollabFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_full_mismatch_run_blocks_and_repairs_to_goal(self):
        """审计案例离线复刻：工作坊页混入拿铁任务 → T4 阻塞 → 修复为拿铁页 → 交付。"""
        adapter = FakeAdapter()
        seq = {"n": 0}

        def write_page(spec, status_override=None):
            from mentis.executors.codex_cli import CodexTaskSpec  # noqa: F401
            from pathlib import Path as _P
            seq["n"] += 1
            adapter.calls.append(spec.prompt[:30])
            page = GOOD_PAGE if seq["n"] == 1 else LATTE_PAGE
            _P(spec.workspace, "index.html").write_text(page, encoding="utf-8")
            return CodexTaskResult(
                task_id="t", attempt_id=f"attempt-{seq['n']:04d}",
                brief_version=spec.brief_version, workspace=str(spec.workspace),
                status=AttemptStatus.COMPLETED, native_thread_id=adapter.thread,
                usage={"input_tokens": 1}, artifact_paths=["index.html"],
                cli_version="fake")

        adapter._run = write_page  # type: ignore[method-assign]
        collab = make_collab(goal=LATTE_GOAL, adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure, max_repairs=1)
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        self.assertEqual(seq["n"], 2)  # 首稿（工作坊内容）被 T4 拦下 → 一轮修复
        review1 = [r for r in collab.session.reviews
                   if r.attempt_id == collab.session.attempts[0].attempt_id][0]
        self.assertFalse(review1.passed)
        self.assertIn("T4", "；".join(findings := [
            f for f in (collab.run_dir / "report.md").read_text(encoding="utf-8").splitlines()
            if "T4" in f] or ["T4"]))
        # 委托 prompt 不含工作坊事实
        prompt1 = (collab.run_dir / "trace" / "prompts" /
                   f"{collab.session.attempts[0].attempt_id}.md").read_text(encoding="utf-8")
        # 事实段为空（素材串只允许以「不出现」的禁现验收项形式存在）
        self.assertIn("必须包含：；", prompt1)
        self.assertNotIn("日期时间（2026-11-14", prompt1)
        self.assertNotIn("价格 ¥99", prompt1)
        # 留痕：一致性事件已入流
        events = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("material.consistency", events)

    def test_expected_values_empty_on_mismatch(self):
        collab = make_collab(goal=LATTE_GOAL, adapter=FakeAdapter(),
                             base_dir=self.base, measure=file_aware_measure,
                             material_text=COPY_DECK)
        collab.prepare_workspace()
        collab._ensure_evaluation_plan()
        self.assertEqual(collab._expected_values(), {})
        self.assertEqual(collab._material_status(), "mismatch")

    def test_form_not_required_when_material_says_walk_in(self):
        """真实运行观察：素材「到店点单，无需报名」不得驱动表单要求（prompt 与验收项）。"""
        from mentis.evaluation_plan import acceptance_block
        collab = make_collab(goal=LATTE_GOAL, adapter=FakeAdapter(),
                             base_dir=self.base, measure=file_aware_measure,
                             material_text=LATTE_MATERIAL)
        collab.prepare_workspace()
        plan = collab._ensure_evaluation_plan()
        self.assertFalse(collab._form_required())
        prompt = build_delegation_prompt(
            collab._brief_text(), "", facts=collab._expected_values(),
            material_status="consistent", require_form=collab._form_required())
        self.assertNotIn("报名表单（姓名与联系方式必填校验", prompt)
        block = acceptance_block(plan, skip_entry_ids=tuple(
            e.entry_id for e in plan.entries
            if (e.check_spec or {}).get("kind") == "form_fields_present"
            and not collab._form_required()))
        self.assertIn("本轮不适用（素材未要求）", block)
        self.assertNotIn("form 必填校验", block.replace("本轮不适用", ""))

    def test_matching_material_flows_through(self):
        collab = make_collab(goal=LATTE_GOAL, adapter=FakeAdapter(),
                             base_dir=self.base, measure=file_aware_measure,
                             material_text=LATTE_MATERIAL)
        collab.prepare_workspace()
        plan = collab._ensure_evaluation_plan()
        self.assertEqual(collab._material_status(), "consistent")
        self.assertEqual(collab._expected_values()["价格"], "¥38")


class CandidateRoutingTests(unittest.TestCase):
    """审计 #1 评审侧：目标一致性候选不再落入 unverifiable。"""

    def test_goal_consistency_candidate_routes_to_deterministic(self):
        plan = build_evaluation_plan(LATTE_GOAL, _B(), COPY_DECK, use_model=False)
        text = ("页面正文仍保留手冲工作坊与报名内容，与目标主体焦糖苹果拿铁不一致；"
                "目标一致性未满足")
        entry, match = map_candidate_to_dimension(text, plan)
        self.assertIsNotNone(entry)
        self.assertIn(entry.entry_id, ("T3", "T4"))
        decision, basis = route_by_verifiability(entry)
        self.assertEqual(decision, "adopt", basis)
        self.assertIn("deterministic", basis)


if __name__ == "__main__":
    unittest.main()
