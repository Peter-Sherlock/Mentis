"""缺口分流：把影响下一步的未知按信息归属、重要性、可逆性、成本分流。

五类去向（AGENTS.md §5 / PROJECT_PLAN §10.1）：自决、工具查询、执行器咨询、
小样实验、用户决定；另有受限去向"标记未验证/升级"。

两条硬性纪律（P2.1 退出条件）：
- 常规专业问题不得仅因不确定就转交用户（model_knowledge + 非阻塞 → 自决）；
- 不因追求零提问而省略必要介入（用户持有的保留决定/许可 → 必须请求用户）。

每条分流必须给出可检查的理由（rationale）。
"""

from dataclasses import dataclass

ACTIONS = {
    "agent_decide": "自决（记录来源 agent_selected，不转交用户）",
    "tool_query": "工具查询（读工作区/资料）",
    "executor_consult": "执行器咨询（能力/成本/实现方式）",
    "run_sample": "小样实验（做可比较样例再收敛）",
    "request_user": "请求用户决定（request_decision）",
    "limited_unverifiable": "受限：标记未验证，不基于猜测行动",
    "escalate": "升级：超出自决边界，需外部判断或许可",
}

VALID_HOLDERS = ("user", "user_experience", "workspace", "executor", "model_knowledge")
VALID_IMPORTANCE = ("blocking", "plan_changing", "minor")
VALID_REVERSIBILITY = ("irreversible", "hard_to_reverse", "reversible")


@dataclass
class Gap:
    description: str
    info_holder: str        # VALID_HOLDERS 之一
    importance: str         # VALID_IMPORTANCE 之一
    reversibility: str      # VALID_REVERSIBILITY 之一
    requires_observation: bool = False   # 是否需要看实际产物才能判断
    sample_possible: bool = False        # 能否用低成本小样获取感受类信息
    reserved_decision: bool = False      # 是否属于用户保留决定


@dataclass
class RoutingDecision:
    action: str            # ACTIONS 的键
    blocking: bool         # 是否阻塞依赖动作（对接 P1.2 request_decision）
    rationale: str
    gap: str

    @property
    def action_label(self) -> str:
        return ACTIONS[self.action]


def route_gap(gap: Gap) -> RoutingDecision:
    _validate(gap)
    d = gap.description
    if gap.requires_observation:
        return RoutingDecision(
            "limited_unverifiable", gap.importance == "blocking",
            f"「{d}」需要观察实际产物才能判断；缺观察能力时标记未验证，"
            "不基于猜测宣称或行动（可先记录，待观察能力可用后复检）",
            d,
        )
    if gap.info_holder == "user" or gap.reserved_decision:
        blocking = gap.importance == "blocking"
        why = "用户保留决定" if gap.reserved_decision else "事实/取舍/许可由用户持有"
        return RoutingDecision(
            "request_user", blocking,
            f"「{d}」：{why}；{'阻塞依赖动作直到回答' if blocking else '不阻塞无关动作'}。"
            "这是必要介入，不因追求零提问而省略",
            d,
        )
    if gap.info_holder == "user_experience":
        if gap.importance == "blocking" and gap.reversibility == "irreversible":
            return RoutingDecision(
                "request_user", True,
                f"「{d}」：感受类且不可逆的阻塞取舍，直接请用户决定",
                d,
            )
        if gap.sample_possible:
            return RoutingDecision(
                "run_sample", False,
                f"「{d}」：感受类信息靠抽象询问不可靠，先做低成本可比较样例，"
                "让用户对实际呈现作反应（比问卷更省且更准）",
                d,
            )
        return RoutingDecision(
            "request_user", False,
            f"「{d}」：感受类且无法低成本取样，请用户结合实例表达",
            d,
        )
    if gap.info_holder in ("workspace", "executor"):
        if gap.info_holder == "workspace":
            return RoutingDecision(
                "tool_query", False,
                f"「{d}」：客观事实在工作区/资料里，工具先行，不打扰用户",
                d,
            )
        return RoutingDecision(
            "executor_consult", False,
            f"「{d}」：能力/成本/实现方式是执行器的领域，向执行器咨询",
            d,
        )
    # info_holder == model_knowledge：常规专业判断
    if gap.importance == "blocking" and gap.reversibility == "irreversible":
        return RoutingDecision(
            "escalate", True,
            f"「{d}」：阻塞且不可逆、又缺乏可靠专业依据——超出自决边界，升级处理，"
            "不冒充专家结论",
            d,
        )
    return RoutingDecision(
        "agent_decide", False,
        f"「{d}」：常规专业问题，可逆且不阻塞——系统代决并记录来源 agent_selected，"
        "不因不确定就转交用户；交付时说明依据",
        d,
    )


def _validate(gap: Gap) -> None:
    if gap.info_holder not in VALID_HOLDERS:
        raise ValueError(f"info_holder 非法: {gap.info_holder}")
    if gap.importance not in VALID_IMPORTANCE:
        raise ValueError(f"importance 非法: {gap.importance}")
    if gap.reversibility not in VALID_REVERSIBILITY:
        raise ValueError(f"reversibility 非法: {gap.reversibility}")
