"""P2.4-V2 回归例：可验证性路由与盲比复检（全部离线；模型响应一律 Mock，已标注）。

覆盖（V2 退出门槛）：
- 审美/偏好类候选不自动修订（user_option 非阻塞告知）、不授予 improved；
- 不可验证维度候选不修订；对应不到维度=计划盲区不自动采纳；
- 盲比：A/B 随机指派可审计、正反两序一致才判 improved、两序不一致记
  changed_unverified（不判改善）、模型不可用/输出非法不判改善；
- 判定分级六值单元覆盖；
- 虚构维度编号剔除留痕。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mentis.collab import Collaboration
from mentis.eval_review import (VERDICT_CHANGED_UNVERIFIED, VERDICT_IMPROVED,
                                VERDICT_NO_CHANGE, VERDICT_REGRESSION,
                                VERDICT_UNVERIFIED, VERDICT_UNKNOWN,
                                grade_revision, map_candidate_to_dimension,
                                route_by_verifiability, run_blind_review)
from mentis.evaluation_plan import build_evaluation_plan

try:
    from tests.test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from tests.test_guidance import DIAGNOSIS_GENERIC, FakeGateway, _FixedCoin, file_aware_measure
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from test_guidance import DIAGNOSIS_GENERIC, FakeGateway, _FixedCoin, file_aware_measure

GOAL = "为拾光咖啡工作坊制作活动介绍页"


def make_plan(use_model=False, gateway=None):
    from mentis.contracts import Brief, Provenance, SourcedItem

    brief = Brief(brief_id="b", version=1, objective=GOAL, constraints=[
        SourcedItem("本地单页、无网络、不发送真实数据；改动限于隔离工作区",
                    Provenance.SYSTEM, "system-preset:isolation-no-network")],
        preferences=[])
    from mentis.capabilities.registry import load_registry
    from mentis.capabilities.selection import select_for_task

    _, resources = select_for_task(
        load_registry(), task_label="评估计划", task_keywords=["单页活动介绍页"],
        decision_needs=["信息密度", "主操作层级", "表单校验行为"],
        observation_capabilities={"structure_only": True}, can_execute=True)
    return build_evaluation_plan(goal=GOAL, brief=brief, material_text=None,
                                 resources=resources, gateway=gateway, use_model=use_model)


def load_coffee_material() -> str:
    from mentis.collab import COPY_DECK

    return COPY_DECK


class RoutingTests(unittest.TestCase):
    def setUp(self):
        from mentis.contracts import Brief, Provenance, SourcedItem

        brief = Brief(brief_id="b", version=1, objective=GOAL, constraints=[
            SourcedItem("本地单页、无网络、不发送真实数据；改动限于隔离工作区",
                        Provenance.SYSTEM, "system-preset:isolation-no-network")],
            preferences=[])
        self.plan = build_evaluation_plan(goal=GOAL, brief=brief,
                                          material_text=load_coffee_material(),
                                          resources=self._resources())

    def _resources(self):
        from mentis.capabilities.registry import load_registry
        from mentis.capabilities.selection import select_for_task

        _, resources = select_for_task(
            load_registry(), task_label="评估计划", task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为"],
            observation_capabilities={"structure_only": True}, can_execute=True)
        return resources

    def test_aesthetic_candidate_routes_to_user_option(self):
        """审美类候选 → user_judgment 维度 → 非阻塞选项，不自动修订。"""
        entry, match = map_candidate_to_dimension(
            "暖色低饱和配色接近温暖感受；高饱和与折扣元素常被读作促销广告", self.plan)
        self.assertIsNotNone(entry, match)
        self.assertEqual(entry.category, "professional_advice")
        decision, basis = route_by_verifiability(entry)
        self.assertEqual(decision, "user_option")
        self.assertIn("user_judgment", basis)

    def test_unverifiable_dimension_not_revised(self):
        entry, match = map_candidate_to_dimension(
            "正文文字与背景对比度抽检不低于 4.5:1（对比度测量）", self.plan)
        self.assertIsNotNone(entry, match)
        decision, basis = route_by_verifiability(entry)
        self.assertEqual(decision, "skip_unverifiable")
        self.assertIn("无法验证", basis)

    def test_unmapped_candidate_is_plan_blind_spot(self):
        entry, match = map_candidate_to_dimension(
            "量子纠缠布局算法未使用（与任务素材和领域规则均无关的臆造主张）", self.plan)
        self.assertIsNone(entry)
        self.assertEqual(match["matched_by"], "none")
        decision, basis = route_by_verifiability(None)
        self.assertEqual(decision, "plan_blind_spot")
        self.assertIn("计划盲区", basis)

    def test_deterministic_dimension_adoptable(self):
        entry, match = map_candidate_to_dimension(
            "页面正文缺少价格信息（对应计划维度 T1 任务关键事实完整呈现）", self.plan)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.entry_id, "T1")  # 显式维度 id 直接命中
        self.assertEqual(match["matched_by"], "explicit_id")
        decision, _ = route_by_verifiability(entry)
        self.assertEqual(decision, "adopt")

    def test_hypothesis_rule_not_route_target(self):
        """hypothesis 聚合条目 unverifiable：即便被引用也不触发修订、不作修改依据。"""
        entry, _ = map_candidate_to_dimension(
            "折扣、倒计时类元素会带来促销感（待验证假设 IH-6）", self.plan)
        decision, _ = route_by_verifiability(entry)
        self.assertEqual(decision, "skip_unverifiable")


class _AestheticGateway(FakeGateway):
    """诊断返回审美类候选的 Mock 网关（不消耗队列语义，固定响应）。"""
    pass


AESTHETIC_DIAGNOSIS = json.dumps({
    "diagnoses": [{
        "phenomenon": "配色饱和度可再降低，更接近温暖感受（审美方向，用户偏好维度）",
        "evidence_refs": ["h:1"], "severity": "minor",
        "interpretation": "可逆的呈现取舍；属用户偏好而非硬要求",
        "modification_candidates": ["降低暖色饱和度一档"],
    }],
    "next_action": "agent_decide", "decision_requests": [],
}, ensure_ascii=False)


class CollabRoutingFlowTests(unittest.TestCase):
    """collab 全流程：审美候选不自动修订；盲比不一致不判改善。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def make(self, responses, adapter=None, **kw):
        kw.setdefault("measure", file_aware_measure)
        collab = Collaboration(
            goal=GOAL, adapter=adapter or FakeAdapter(behavior={"first": "good"}),
            gateway=FakeGateway(responses), browser_capture=stub_capture,
            use_model=True, interactive=False, base_dir=self.base, **kw)
        collab._blind_rng = _FixedCoin()
        return collab

    def test_aesthetic_candidate_not_revised_not_improved(self):
        """审美候选 → user_option：零修订委托；不授予 improved（无盲比改善证据）。"""
        collab = self.make([AESTHETIC_DIAGNOSIS])
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        self.assertEqual(len(collab.adapter.calls), 1)  # 只有构建委托，无修订
        review_doc = json.loads((collab.run_dir / "professional-review.json")
                                .read_text(encoding="utf-8"))
        self.assertEqual(review_doc["candidates"][0]["decision"], "user_option")
        self.assertEqual(review_doc["routing"]["user_option"], 1)
        self.assertIsNone(collab.last_professional_review["verdict"])  # 未发生修订复检
        self.assertNotEqual(collab.last_professional_review.get("verdict"), "improved")

    def test_blind_inconsistent_yields_changed_unverified(self):
        """盲比两序不一致 → changed_unverified：改了但效果未验证，不判改善、如实交付。"""
        inconsistent_1 = json.dumps({"overall": "X_better", "dimensions": [
            {"entry_id": "T1", "verdict": "X_better", "reason": "Mock：X 好"}]},
            ensure_ascii=False)
        inconsistent_2 = json.dumps({"overall": "X_better", "dimensions": [
            {"entry_id": "T1", "verdict": "X_better", "reason": "Mock：又是 X 好"}]},
            ensure_ascii=False)

        class ChangingAdapter(FakeAdapter):
            """修订轮写不同页面（artifact_changed=True），与盲比响应无关（Mock 语义）。"""

            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                if len(self.calls) >= 2:
                    Path(spec.workspace).joinpath("index.html").write_text(
                        GOOD_PAGE + "<!-- blind-v2 -->", encoding="utf-8")
                return result

        class FlipCoin:
            @staticmethod
            def random():
                return 0.1  # before_first=True：order1 X=before/Y=after

        collab = self.make([DIAGNOSIS_GENERIC, inconsistent_1, inconsistent_2],
                           adapter=ChangingAdapter(behavior={"first": "good"}))
        collab._blind_rng = FlipCoin()  # order1 X=before → X_better=before_better；order2 X=after → after_better
        status = collab.run()
        self.assertEqual(status, "DELIVERED")  # 确定性检查通过，如实交付
        self.assertEqual(collab.last_professional_review["verdict"],
                         VERDICT_CHANGED_UNVERIFIED)
        self.assertNotEqual(collab.last_professional_review["verdict"], VERDICT_IMPROVED)
        verify_doc = json.loads((collab.run_dir / "professional-verify.json")
                                .read_text(encoding="utf-8"))
        self.assertTrue(verify_doc["artifact_changed"])
        self.assertFalse(verify_doc["blind_review"]["consistent"])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn(VERDICT_CHANGED_UNVERIFIED, report)
        self.assertNotIn("**improved**", report)


class BlindReviewUnitTests(unittest.TestCase):
    def setUp(self):
        self.plan = make_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "blind.json"

    def _packs(self, same=True):
        """构造最小观察包替身（render_text 所需字段）。"""
        def pack(text):
            structure = SimpleNamespace(
                render_text=lambda: f"[结构] {text}",
                sha256=text, source_path="x")
            measurement = SimpleNamespace(render_text=lambda: "[测量] 溢出 0px")
            return SimpleNamespace(structure=structure, measurement=measurement,
                                   image_names=[], sha256=text)
        return pack("before"), pack("after")

    def test_inconsistent_orders_marked_not_consistent(self):
        before, after = self._packs()

        class Gw:
            config = SimpleNamespace(model="mock")

            def has_credentials(self):
                return True

            def complete(self, system, prompt, json_mode=True, mock=False, images=None):
                # 不看指派，恒判 X_better → 两序必然不一致（正反对照）
                return SimpleNamespace(ok=True, mock=True, model="mock", usage={},
                                       error="", response_text=json.dumps(
                                           {"overall": "X_better", "dimensions": []}),
                                       image_refs=[], request_id="f")

        result = run_blind_review(Gw(), self.plan, before, [], after, [], self.out,
                                  use_model=True, rng=_FixedCoin())
        self.assertFalse(result["consistent"])
        self.assertEqual(result["overall"], "inconsistent")
        self.assertTrue(self.out.is_file())
        self.assertTrue(result["mock"])  # Mock 标注

    def test_model_unavailable_records_unknown(self):
        before, after = self._packs()

        class NoCred:
            def has_credentials(self):
                return False

        result = run_blind_review(NoCred(), self.plan, before, [], after, [], self.out,
                                  use_model=True, rng=_FixedCoin())
        self.assertEqual(result["overall"], VERDICT_UNKNOWN)
        self.assertIn("盲比未执行", result["note"])

    def test_fabricated_dimension_ids_dropped(self):
        before, after = self._packs()
        response = json.dumps({"overall": "X_better", "dimensions": [
            {"entry_id": "ZZ-9", "verdict": "X_better", "reason": "虚构维度"}]},
            ensure_ascii=False)

        class Gw:
            config = SimpleNamespace(model="mock")

            def has_credentials(self):
                return True

            def complete(self, system, prompt, json_mode=True, mock=False, images=None):
                return SimpleNamespace(ok=True, mock=True, model="mock", usage={},
                                       error="", response_text=response,
                                       image_refs=[], request_id="f")

        result = run_blind_review(Gw(), self.plan, before, [], after, [], self.out,
                                  use_model=True, rng=_FixedCoin())
        self.assertIn("ZZ-9", result["dropped_refs"])
        # 无有效维度判定：全部标准维度记 unknown（逐维度如实呈现）
        self.assertTrue(all(v == "unknown" for v in result["per_dimension"].values()))
        self.assertTrue(self.out.is_file())


class GradingTests(unittest.TestCase):
    def test_six_grades(self):
        blind_ok = {"overall": "after_better", "consistent": True, "per_dimension": {"T1": "after_better"}}
        blind_equal = {"overall": "equal", "consistent": True, "per_dimension": {}}
        blind_worse = {"overall": "before_better", "consistent": True, "per_dimension": {}}
        blind_inconsistent = {"overall": "inconsistent", "consistent": False, "per_dimension": {}}
        cases = [
            ((["价格缺失"], [], True, blind_ok), VERDICT_REGRESSION),
            (([], ["测量不可信"], True, blind_ok), VERDICT_UNVERIFIED),
            (([], [], False, blind_ok), VERDICT_NO_CHANGE),
            (([], [], True, blind_ok), VERDICT_IMPROVED),
            (([], [], True, blind_equal), VERDICT_NO_CHANGE),
            (([], [], True, blind_worse), VERDICT_NO_CHANGE),
            (([], [], True, blind_inconsistent), VERDICT_CHANGED_UNVERIFIED),
            (([], [], True, None), VERDICT_UNKNOWN),
        ]
        for (regression, unknown, changed, blind), expected in cases:
            verdict, _ = grade_revision(regression, unknown, changed, blind)
            self.assertEqual(verdict, expected)


class WriterGatePrecisionTests(unittest.TestCase):
    """V2 真实运行观察到并修复：IDE 常驻 codex app-server/computer-use 进程
    曾使存活门控永久误报，人工核对路径无法进行。门控脚本必须排除这两类。"""

    def test_gate_script_excludes_non_writer_processes(self):
        from unittest.mock import patch

        from mentis.collab import Collaboration

        collab = Collaboration.__new__(Collaboration)
        captured = {}

        def fake_run(cmd, **kw):
            captured["script"] = cmd[3]  # ["powershell","-NoProfile","-Command",script]
            return SimpleNamespace(stdout="[]")

        with patch("subprocess.run", side_effect=fake_run):
            result = collab._codex_processes_since("2026-10-02 06:12:04")
        self.assertEqual(result, [])
        script = captured["script"]
        self.assertIn("Get-CimInstance", script)
        self.assertIn("-notmatch 'app-server'", script)
        self.assertIn("-notmatch 'computer-use'", script)
        self.assertIn("codex*", script)

    def test_gate_script_parses_single_process_object(self):
        from unittest.mock import patch

        from mentis.collab import Collaboration

        collab = Collaboration.__new__(Collaboration)

        def fake_run(cmd, **kw):
            return SimpleNamespace(stdout='{"ProcessId": 42}')

        with patch("subprocess.run", side_effect=fake_run):
            result = collab._codex_processes_since("2026-10-02 06:12:04")
        self.assertEqual(len(result), 1)  # 单对象包装为列表（潜在写入者 → 拒绝确认）


if __name__ == "__main__":
    unittest.main()
