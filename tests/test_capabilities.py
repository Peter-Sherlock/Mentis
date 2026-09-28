"""P2.1 回归例：资源加载、按需选择、缺口分流与受限处理。"""

import unittest

from mentis.capabilities.registry import load_registry, load_resource
from mentis.capabilities.routing import ACTIONS, Gap, route_gap
from mentis.capabilities.selection import select_for_task
from mentis.contracts import BlockedByDecision
from mentis.session import TaskSession


def make_session_with_policy():
    from mentis.contracts import DelegationPolicy, Provenance

    session = TaskSession(goal="分流集成测试")
    session.init_brief("目标")
    session.set_policy(
        DelegationPolicy(
            policy_id="policy-1",
            version=1,
            decided_scope=["实现细节"],
            reserved_decisions=["价格"],
            execution_scopes=["workspace:demo"],
            provenance=Provenance.AGENT_SELECTED,
            source_ref="derived",
        )
    )
    session.authorize("workspace:demo")
    return session


class RegistryTests(unittest.TestCase):
    def test_registry_loads_three_resources(self):
        registry = load_registry()
        self.assertEqual(len(registry.resources), 3)
        for meta in registry.resources:
            resource = load_resource(meta)
            self.assertGreaterEqual(len(resource.rules), 4, meta.id)
            self.assertGreaterEqual(len(resource.feedback_patterns), 1, meta.id)
            self.assertEqual(resource.parse_warnings, [], meta.id)

    def test_rules_distinguish_four_natures(self):
        """退出条件：规则区分硬要求/建议/偏好/待验证假设。"""
        registry = load_registry()
        tags = set()
        for meta in registry.resources:
            for rule in load_resource(meta).rules:
                tags.add(rule.tag)
                self.assertTrue(rule.rule_id and rule.text, rule.rule_id)
        self.assertEqual(
            tags, {"hard_constraint", "guideline", "preference", "hypothesis"}
        )
        # 硬要求来自任务族硬要求清单，不把审美当硬要求
        registry_all = [load_resource(m) for m in registry.resources]
        hard_ids = {r.rule_id for res in registry_all for r in res.rules_by_tag("hard_constraint")}
        self.assertIn("VE-1", hard_ids)  # 对比度
        self.assertIn("IH-2", hard_ids)  # 双视口可操作
        self.assertNotIn("VE-5", hard_ids)  # 偏好不得标成硬要求


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry()

    def test_on_demand_selection_matches_needs(self):
        context, loaded = select_for_task(
            self.registry,
            task_label="修改决策",
            task_keywords=["单页活动介绍页"],
            decision_needs=["配色气质", "信息密度", "移动端交互可用性"],
        )
        ids = {s.resource_id for s in context.selections}
        self.assertEqual(
            ids, {"web-information-hierarchy", "web-visual-expression", "web-basic-interaction"}
        )
        self.assertEqual(len(loaded), 3)
        self.assertEqual(context.unknown_needs, [])

    def test_unmatched_need_recorded_as_unknown(self):
        context, _ = select_for_task(
            self.registry,
            task_label="混入未知需要",
            task_keywords=["单页活动介绍页"],
            decision_needs=["配色气质", "分镜节奏"],
        )
        self.assertIn("分镜节奏", context.unknown_needs)
        self.assertIn("配色气质", context.matched_needs)

    def test_off_domain_task_limited(self):
        """未知领域：无资源覆盖 → 明确受限，不冒充专业能力。"""
        context, loaded = select_for_task(
            self.registry,
            task_label="为公司 logo 做 3D 渲染动画",
            task_keywords=["3D 渲染"],
            decision_needs=["镜头节奏", "材质表现"],
        )
        self.assertEqual(context.selections, [])
        self.assertTrue(context.off_domain)
        labels = context.ability_labels()
        self.assertFalse(labels["has_materials"])
        self.assertIn("仅能辅助讨论", labels["product_label"])
        self.assertIn("未验证", labels["verified_scope"])
        self.assertEqual(loaded, [])

    def test_declared_conflict_surfaced(self):
        context, _ = select_for_task(
            self.registry,
            task_label="触发声明冲突",
            task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "配色气质"],
        )
        self.assertEqual(len(context.declared_conflicts), 1)
        self.assertEqual(set(context.declared_conflicts[0]["between"]), {"IH-2", "VE-2"})

    def test_missing_observation_recorded(self):
        context, _ = select_for_task(
            self.registry,
            task_label="缺观察能力",
            task_keywords=["单页活动介绍页"],
            decision_needs=["配色气质"],
            observation_capabilities={"viewport_screenshot": False},
            can_execute=False,
        )
        self.assertFalse(context.can_observe)
        labels = context.ability_labels()
        self.assertFalse(labels["can_observe"])

    def test_context_serializable(self):
        context, _ = select_for_task(
            self.registry,
            task_label="序列化",
            task_keywords=["单页活动介绍页"],
            decision_needs=["配色气质"],
            observation_capabilities={"viewport_screenshot": True},
            can_execute=True,
        )
        data = context.to_dict()
        self.assertEqual(data["ability_labels"]["can_observe"], True)
        self.assertEqual(data["selections"][0]["resource_id"], "web-information-hierarchy")


class RoutingTests(unittest.TestCase):
    def test_five_routes_with_rationale(self):
        cases = [
            (Gap("标题字体选择", "model_knowledge", "minor", "reversible"), "agent_decide"),
            (Gap("是否已有品牌色变量", "workspace", "plan_changing", "reversible"), "tool_query"),
            (Gap("实现成本如何", "executor", "plan_changing", "reversible"), "executor_consult"),
            (Gap("米色还是白色更温暖", "user_experience", "plan_changing", "reversible", sample_possible=True), "run_sample"),
            (Gap("日期改到下周", "user", "blocking", "irreversible", reserved_decision=True), "request_user"),
        ]
        for gap, expected in cases:
            decision = route_gap(gap)
            self.assertEqual(decision.action, expected, gap.description)
            self.assertTrue(decision.rationale)
            self.assertIn(decision.action, ACTIONS)

    def test_routine_professional_question_not_asked(self):
        """退出条件：常规专业问题不得仅因不确定就转交用户。"""
        decision = route_gap(Gap("次要文案断句", "model_knowledge", "minor", "reversible"))
        self.assertNotEqual(decision.action, "request_user")
        self.assertEqual(decision.action, "agent_decide")
        self.assertIn("不因不确定就转交用户", decision.rationale)

    def test_necessary_intervention_not_suppressed(self):
        """退出条件：不因追求零提问而省略必要介入。"""
        decision = route_gap(
            Gap("是否对外开放", "user", "blocking", "irreversible", reserved_decision=True)
        )
        self.assertEqual(decision.action, "request_user")
        self.assertTrue(decision.blocking)
        self.assertIn("必要介入", decision.rationale)

    def test_subjective_prefers_sample_over_abstract_ask(self):
        decision = route_gap(
            Gap("两种底色哪个更温暖", "user_experience", "plan_changing", "reversible", sample_possible=True)
        )
        self.assertEqual(decision.action, "run_sample")
        self.assertIn("样例", decision.rationale)

    def test_missing_observation_limited(self):
        decision = route_gap(
            Gap("修改后移动端是否溢出", "workspace", "blocking", "reversible", requires_observation=True)
        )
        self.assertEqual(decision.action, "limited_unverifiable")
        self.assertIn("未验证", decision.rationale)

    def test_blocking_irreversible_knowledge_escalates(self):
        decision = route_gap(
            Gap("肖像版权风险结论", "model_knowledge", "blocking", "irreversible")
        )
        self.assertEqual(decision.action, "escalate")

    def test_route_integrates_with_session(self):
        """request_user 分流对接 P1.2：请求未回答时依赖动作被阻塞。"""
        session = make_session_with_policy()
        decision = route_gap(
            Gap("价格调整", "user", "blocking", "irreversible", reserved_decision=True)
        )
        self.assertEqual(decision.action, "request_user")
        ix = session.request_decision("价格调整是否同意？", blocking_action="apply-price")
        with self.assertRaises(BlockedByDecision):
            session.dispatch("apply-price", "workspace:demo")
        session.answer_interaction(ix.interaction_id, "同意", source="user")
        attempt = session.dispatch("apply-price", "workspace:demo")
        self.assertIsNotNone(attempt)


if __name__ == "__main__":
    unittest.main()
