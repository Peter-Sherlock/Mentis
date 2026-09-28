"""P2.1 演示：首领域资源与行动依据。

规则与分流是确定性决策表；资源为草案（pending_human）。
演示不调用模型或执行器；分流去向中需要执行/观察能力的动作
在当前阶段尚不可用，按受限处理如实标注。
"""

import json
import os

from mentis.capabilities.registry import RULE_TAG_LABEL, load_registry
from mentis.capabilities.routing import ACTIONS, Gap, route_gap
from mentis.capabilities.selection import select_for_task

DEMO_DIR = os.path.join("runtime", "demo")


def run_demo() -> int:
    os.makedirs(DEMO_DIR, exist_ok=True)
    print("=" * 62)
    print("【演示】P2.1 首领域资源与行动依据 —— 确定性规则演示；")
    print("资源为草案（pending_human），加载资料不证明专业能力。")
    print("=" * 62)

    registry = load_registry()
    print(f"\n[1] 注册表 v{registry.registry_version}：{len(registry.resources)} 份资源")
    total = {}
    for meta in registry.resources:
        from mentis.capabilities.registry import load_resource
        res = load_resource(meta)
        for r in res.rules:
            total[r.tag] = total.get(r.tag, 0) + 1
        print(
            f"    - {meta.id} v{meta.version}：规则 {len(res.rules)} 条，"
            f"反馈模式 {len(res.feedback_patterns)} 条，正/反例 "
            f"{len(res.positive_examples)}/{len(res.negative_examples)}"
        )
    print("    规则性质分布：" + "，".join(f"{RULE_TAG_LABEL[k]}×{v}" for k, v in total.items()))

    context, loaded = select_for_task(
        registry,
        task_label="拾光咖啡活动页：首稿反馈后的修改决策（演示）",
        task_keywords=["单页活动介绍页"],
        decision_needs=["配色气质", "信息密度", "移动端交互可用性", "分镜节奏"],
        observation_capabilities={"viewport_screenshot": False, "interaction_record": False},
        can_execute=False,
    )
    print(f"\n[2] 按需加载（任务关键词 + 决策需要匹配）：")
    for s in context.selections:
        print(
            f"    - {s.resource_id}：命中决策问题 {'、'.join(s.matched_decision_questions)}；"
            f"加载规则 {len(s.rules_loaded)} 条"
        )
    print(f"    未被任何资源覆盖的需要：{context.unknown_needs}（记录为未知，不冒充覆盖）")
    if context.declared_conflicts:
        for c in context.declared_conflicts:
            print(f"    声明冲突：{c['between']} —— {c['note']}")
    labels = context.ability_labels()
    print(
        f"    能力四分：有资料={labels['has_materials']}，能执行={labels['can_execute']}，"
        f"能观察={labels['can_observe']}，验证范围={labels['verified_scope']}"
    )

    gaps = [
        Gap("正文标题用衬线还是无衬线字体", "model_knowledge", "minor", "reversible"),
        Gap("工作区里是否已有品牌色变量", "workspace", "plan_changing", "reversible"),
        Gap("表单校验在该静态页实现上的成本与限制", "executor", "plan_changing", "reversible"),
        Gap("米色底还是白底更符合「温暖」", "user_experience", "plan_changing", "reversible", sample_possible=True),
        Gap("活动日期从 11-14 改到 11-21", "user", "blocking", "irreversible", reserved_decision=True),
        Gap("移动端修改后是否仍有横向溢出", "workspace", "blocking", "reversible", requires_observation=True),
        Gap("宣传图人物肖像的版权风险结论", "model_knowledge", "blocking", "irreversible"),
    ]
    print("\n[3] 缺口分流（每条含可检查理由）：")
    for gap in gaps:
        decision = route_gap(gap)
        print(f"    - [{decision.action}] {decision.action_label}")
        print(f"      {decision.rationale}")

    print("\n[4] 反模式自检（写入决策表的两条纪律）：")
    routine = route_gap(Gap("次要文案的断句方式", "model_knowledge", "minor", "reversible"))
    assert routine.action == "agent_decide"
    print("    - 常规专业问题（含不确定）→ 自决，不转交用户 ✓")
    reserved = route_gap(Gap("是否对外开放页面", "user", "blocking", "irreversible", reserved_decision=True))
    assert reserved.action == "request_user" and reserved.blocking
    print("    - 用户保留决定 → 必要介入不被省略，阻塞依赖动作 ✓")

    ctx_path = os.path.join(DEMO_DIR, "capability_context_demo.json")
    with open(ctx_path, "w", encoding="utf-8") as f:
        json.dump(context.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"\n[5] CapabilityContext 已保存：{ctx_path}")
    print("    说明：分流去向中 run_sample / executor_consult 需要的能力（执行器、观察）")
    print("    在 P3.1 前不可用——执行时将按受限路径记录，不伪造结果。")
    print("=" * 62)
    return 0
