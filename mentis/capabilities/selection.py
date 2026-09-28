"""能力选择：按当前任务与决策需要加载资源，记录 CapabilityContext。

按需加载以资源为单位（v0.1 规则级过滤在实际提示词构建时再做）；
未知需求、声明冲突、观察能力缺口都显式记录，不静默处理。
能力四分：有资料 / 能执行 / 能观察 / 已验证判断范围——不得合并成 expert=true。
"""

from dataclasses import dataclass, field
from datetime import datetime

from mentis.capabilities.registry import Registry, Resource, load_resource


@dataclass
class ResourceSelection:
    resource_id: str
    version: str
    rationale: list                    # 命中理由
    matched_decision_questions: list
    rules_loaded: list                 # list[Rule]
    observation_requirements: list


@dataclass
class CapabilityContext:
    task_label: str
    selections: list = field(default_factory=list)          # list[ResourceSelection]
    matched_needs: list = field(default_factory=list)
    unknown_needs: list = field(default_factory=list)       # 没有资源覆盖的决策需要
    declared_conflicts: list = field(default_factory=list)  # 双方规则都在本次加载中的声明冲突
    observation_capabilities: dict = field(default_factory=dict)  # 工具名 -> bool
    can_execute: bool = False                               # 执行器 P3.1 接入前为 False
    created_at: str = ""

    @property
    def can_observe(self) -> bool:
        return any(self.observation_capabilities.values())

    @property
    def off_domain(self) -> bool:
        return not self.selections and bool(self.unknown_needs)

    def ability_labels(self) -> dict:
        """能力四分；描述必须对应证据（DOMAIN_STRATEGY §8）。"""
        return {
            "has_materials": bool(self.selections),
            "can_execute": self.can_execute,
            "can_observe": self.can_observe,
            "verified_scope": (
                "未知领域：仅能辅助讨论；能力未验证" if self.off_domain
                else "未验证——资源为草案且无运行证据"
            ),
            "product_label": (
                "试验范围（资源草案、无执行与观察能力）" if not self.off_domain
                else "未知领域：仅能辅助讨论，不作专业结论"
            ),
        }

    def to_dict(self) -> dict:
        return {
            "task_label": self.task_label,
            "created_at": self.created_at,
            "selections": [
                {
                    "resource_id": s.resource_id,
                    "version": s.version,
                    "rationale": s.rationale,
                    "matched_decision_questions": s.matched_decision_questions,
                    "rules_loaded": [r.rule_id for r in s.rules_loaded],
                    "observation_requirements": s.observation_requirements,
                }
                for s in self.selections
            ],
            "matched_needs": self.matched_needs,
            "unknown_needs": self.unknown_needs,
            "declared_conflicts": self.declared_conflicts,
            "observation_capabilities": self.observation_capabilities,
            "can_execute": self.can_execute,
            "ability_labels": self.ability_labels(),
        }


def select_for_task(
    registry: Registry,
    task_label: str,
    task_keywords: list,
    decision_needs: list,
    observation_capabilities: dict | None = None,
    can_execute: bool = False,
    registry_path=None,
) -> tuple[CapabilityContext, list]:
    """返回 (CapabilityContext, 已加载资源列表[Resource])。

    匹配规则（透明、可解释）：
    - 任务关键词命中 applies_to → 该资源进入候选；
    - 决策需要与 decision_questions 互相包含 → 记录命中理由；
    - 两者都不中的决策需要进入 unknown_needs；
    - 注册表声明冲突中，若双方规则都在本次加载的资源里 → 记录冲突待呈现。
    """
    context = CapabilityContext(
        task_label=task_label,
        observation_capabilities=dict(observation_capabilities or {}),
        can_execute=can_execute,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    loaded: list[Resource] = []
    selected_ids = []
    for meta in registry.resources:
        reasons = [kw for kw in task_keywords if any(kw in a for a in meta.applies_to)]
        matched = [
            need
            for need in decision_needs
            if any(need in q or q in need for q in meta.decision_questions)
        ]
        if not reasons and not matched:
            continue
        resource = load_resource(meta, registry_path)
        loaded.append(resource)
        selected_ids.append(meta.id)
        context.selections.append(
            ResourceSelection(
                resource_id=meta.id,
                version=meta.version,
                rationale=reasons or ["命中决策问题匹配"],
                matched_decision_questions=matched,
                rules_loaded=resource.rules,
                observation_requirements=meta.observation_requirements,
            )
        )
        context.matched_needs.extend(matched)

    context.matched_needs = list(dict.fromkeys(context.matched_needs))
    context.unknown_needs = [
        need for need in decision_needs if need not in context.matched_needs
    ]

    rule_ids = {r.rule_id for res in loaded for r in res.rules}
    for conflict in registry.declared_conflicts:
        if all(rid in rule_ids for rid in conflict["between"]):
            context.declared_conflicts.append(conflict)
    return context, loaded
