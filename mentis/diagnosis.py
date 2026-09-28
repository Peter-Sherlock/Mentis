"""P2.2 诊断候选处理：真实模型输出 → 结构校验 → 可靠候选。

纪律（P2.2 退出条件）：
- 结构合法不等于语义正确：两者分开记录；
- 证据引用必须指向观察记录中真实存在的条目，虚构位置整条拒收；
- 决策请求必须给齐三要素：阻塞原因、用户独有信息或授权依据、已排除的自主路径；
- 有界修复：JSON 解析失败只重问一次，仍失败则如实记录失败。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from mentis.capabilities.selection import CapabilityContext

SEVERITIES = ("blocking", "plan_changing", "minor")
NEXT_ACTIONS = ("agent_decide", "tool_query", "executor_consult", "run_sample", "request_user", "stop")

SYSTEM_PROMPT = (
    "你是网页设计指导助手 Mentis 的诊断模块。只依据给定的观察记录与领域依据输出 JSON，"
    "不得虚构观察记录中不存在的证据位置。每条诊断必须引用观察记录中的条目编号"
    "（如 btn:1、input:2）。观察记录的【边界】部分明确列出你无法验证的方面，"
    "不得对未验证方面下结论。若需要用户决定，必须说明阻塞原因、"
    "为什么该信息只有用户提供、以及已经排除或可以排除的自主路径。"
    "严格输出一个 JSON 对象，字段：diagnoses（数组）、next_action（字符串）、"
    "decision_requests（数组）。diagnoses 元素字段：phenomenon、evidence_refs（数组）、"
    "severity（blocking/plan_changing/minor）、interpretation、modification_candidates（数组）。"
    "decision_requests 元素字段：question、blocking_reason、user_unique_info_or_auth_basis、"
    "excluded_autonomous_paths；这四个字段的值都必须是字符串。"
)


@dataclass
class DiagnosisItem:
    phenomenon: str
    evidence_refs: list
    severity: str
    interpretation: str
    modification_candidates: list


@dataclass
class DecisionRequest:
    question: str
    blocking_reason: str
    user_unique_info_or_auth_basis: str
    excluded_autonomous_paths: str


@dataclass
class DiagnosisBundle:
    goal: str
    observation_source: str
    model: str
    valid_diagnoses: list = field(default_factory=list)      # list[DiagnosisItem]
    invalid_diagnoses: list = field(default_factory=list)    # list[{item, reasons}]
    valid_decision_requests: list = field(default_factory=list)
    invalid_decision_requests: list = field(default_factory=list)
    next_action: str = ""
    next_action_valid: bool = False
    raw_text: str = ""
    call_ids: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    mock: bool = False
    created_at: str = ""


def build_user_prompt(goal: str, user_feedback: str, observation, context: CapabilityContext | None) -> str:
    parts = [f"[目标] {goal}"]
    if user_feedback:
        parts.append(f"[用户反馈（逐字）] {user_feedback}")
    if context is not None:
        rule_lines = []
        for s in context.selections:
            for r in s.rules_loaded:
                rule_lines.append(f"- ({r.rule_id}) {r.text}")
        if rule_lines:
            parts.append(
                "[领域依据（草案，未经人工复核，仅供参考与自查）]\n" + "\n".join(rule_lines)
            )
        if context.declared_conflicts:
            parts.append(
                "[已知声明冲突]\n"
                + "\n".join(f"- {c['between']}：{c['note']}" for c in context.declared_conflicts)
            )
    parts.append(observation.render_text())
    parts.append(
        "[输出要求] 只输出 JSON：diagnoses（引用上述条目编号）、next_action（"
        + "/".join(NEXT_ACTIONS)
        + "）、decision_requests。对未验证方面（观察记录【边界】）不得下结论。"
    )
    return "\n\n".join(parts)


def _validate_diagnosis(item: dict, observation) -> tuple[DiagnosisItem | None, list]:
    reasons = []
    if not isinstance(item, dict):
        return None, ["不是对象"]
    raw_phenomenon = item.get("phenomenon")
    phenomenon = raw_phenomenon.strip() if isinstance(raw_phenomenon, str) else ""
    refs = item.get("evidence_refs")
    severity = item.get("severity")
    raw_interpretation = item.get("interpretation")
    interpretation = raw_interpretation.strip() if isinstance(raw_interpretation, str) else ""
    candidates = item.get("modification_candidates")
    if not phenomenon:
        reasons.append("缺少 phenomenon")
    if not isinstance(refs, list) or not refs:
        reasons.append("evidence_refs 为空或缺失")
    elif not all(isinstance(r, str) and r.strip() for r in refs):
        reasons.append("evidence_refs 必须是非空字符串数组")
    else:
        known = observation.item_ids()
        fabricated = [r for r in refs if r not in known]
        if fabricated:
            reasons.append(f"虚构证据位置（观察记录中不存在）: {fabricated}")
    if severity not in SEVERITIES:
        reasons.append(f"severity 非法: {severity}")
    if not interpretation:
        reasons.append("缺少 interpretation")
    if not isinstance(candidates, list) or not candidates or not all(
        isinstance(c, str) and c.strip() for c in candidates
    ):
        reasons.append("modification_candidates 必须是非空字符串数组")
    if reasons:
        return None, reasons
    return (
        DiagnosisItem(phenomenon, refs, severity, interpretation, candidates),
        [],
    )


def _validate_decision_request(item: dict) -> tuple[DecisionRequest | None, list]:
    reasons = []
    if not isinstance(item, dict):
        return None, ["不是对象"]
    def field_text(key):
        value = item.get(key)
        return value.strip() if isinstance(value, str) else ""

    question = field_text("question")
    blocking = field_text("blocking_reason")
    unique = field_text("user_unique_info_or_auth_basis")
    excluded = field_text("excluded_autonomous_paths")
    if not question:
        reasons.append("缺少 question")
    if not blocking:
        reasons.append("决策请求缺三要素之一：阻塞原因（blocking_reason）")
    if not unique:
        reasons.append("决策请求缺三要素之一：用户独有信息或授权依据")
    if not excluded:
        reasons.append("决策请求缺三要素之一：已排除的自主路径")
    if reasons:
        return None, reasons
    return DecisionRequest(question, blocking, unique, excluded), []


def parse_and_validate(
    raw_text: str,
    observation,
    goal: str,
    model: str,
    call_ids: list,
    usage: dict,
    mock: bool,
) -> DiagnosisBundle:
    bundle = DiagnosisBundle(
        goal=goal,
        observation_source=observation.source_path,
        model=model,
        raw_text=raw_text,
        call_ids=call_ids,
        usage=usage,
        mock=mock,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        bundle.errors.append(f"JSON 解析失败: {exc}（结构不合法，未进入语义处理）")
        return bundle
    if not isinstance(data, dict):
        bundle.errors.append("顶层不是 JSON 对象（结构不合法）")
        return bundle

    for key in ("diagnoses", "decision_requests"):
        if not isinstance(data.get(key), list):
            bundle.errors.append(f"{key} 缺失或不是数组")
    if "next_action" not in data:
        bundle.errors.append("next_action 缺失")
    if bundle.errors:
        return bundle
    for item in data["diagnoses"]:
        valid, reasons = _validate_diagnosis(item, observation)
        if valid is None:
            bundle.invalid_diagnoses.append({"item": item, "reasons": reasons})
        else:
            bundle.valid_diagnoses.append(valid)
    for item in data["decision_requests"]:
        valid, reasons = _validate_decision_request(item)
        if valid is None:
            bundle.invalid_decision_requests.append({"item": item, "reasons": reasons})
        else:
            bundle.valid_decision_requests.append(valid)
    action = data.get("next_action")
    bundle.next_action = str(action)
    bundle.next_action_valid = action in NEXT_ACTIONS
    if action is not None and not bundle.next_action_valid:
        bundle.errors.append(f"next_action 非法: {action}")
    if action == "request_user" and not bundle.valid_decision_requests:
        bundle.next_action_valid = False
        bundle.errors.append("request_user 没有字段完整的决策请求")
    return bundle


def run_diagnosis(
    gateway,
    goal: str,
    user_feedback: str,
    observation,
    context: CapabilityContext | None = None,
) -> DiagnosisBundle:
    """一次真实/Mock 调用 + 有界修复（最多一次）+ 校验。"""
    system = SYSTEM_PROMPT
    user_prompt = build_user_prompt(goal, user_feedback, observation, context)
    record = gateway.complete(system, user_prompt)
    call_ids = [record.request_id]
    usage = dict(record.usage)
    raw = record.response_text
    if record.ok:
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            repair_prompt = (
                f"你上次的输出不是合法 JSON（{exc}）。请重新输出，只输出一个合法 JSON 对象，"
                "不要包含任何其他文字。"
            )
            retry = gateway.complete(system, user_prompt + "\n\n[纠正]\n" + repair_prompt)
            call_ids.append(retry.request_id)
            usage = {
                k: usage.get(k, 0) + retry.usage.get(k, 0)
                for k in set(usage) | set(retry.usage)
            }
            if retry.ok:
                raw = retry.response_text
            else:
                bundle = DiagnosisBundle(
                    goal=goal, observation_source=observation.source_path, model=gateway.config.model,
                    raw_text=raw, call_ids=call_ids, usage=usage,
                    errors=[f"有界修复后仍失败: {retry.error}"], mock=record.mock,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                )
                return bundle
    else:
        return DiagnosisBundle(
            goal=goal, observation_source=observation.source_path, model=gateway.config.model,
            raw_text="", call_ids=call_ids, usage=usage,
            errors=[f"调用失败: {record.error}"], mock=record.mock,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    return parse_and_validate(raw, observation, goal, gateway.config.model, call_ids, usage, record.mock)


def render_bundle_markdown(bundle: DiagnosisBundle) -> str:
    lines = [f"# 诊断候选：{bundle.goal}", ""]
    if bundle.mock:
        origin = "Mock 固定响应（离线验证，非真实模型）"
    elif any(error.startswith("调用失败:") for error in bundle.errors):
        origin = f"真实请求尝试失败（{bundle.model}；未收到有效模型响应）"
    else:
        origin = f"真实模型响应（{bundle.model}）"
    lines.append(f"- 数据来源：**{origin}**；观察来源 {bundle.observation_source}；调用 {bundle.call_ids}")
    lines.append(f"- 用量：{bundle.usage or '（无）'}")
    if bundle.errors:
        lines.append(f"- **错误**：{'；'.join(bundle.errors)}")
    lines.append("")
    lines.append(f"## 结构与引用通过的诊断候选（{len(bundle.valid_diagnoses)} 条；语义未复核）")
    for d in bundle.valid_diagnoses:
        lines.append(f"- [{d.severity}] {d.phenomenon}（证据：{'，'.join(d.evidence_refs)}）")
        lines.append(f"  - 解释：{d.interpretation}")
        for c in d.modification_candidates:
            lines.append(f"  - 候选修改：{c}")
    lines.append(f"## 被拒收候选（{len(bundle.invalid_diagnoses)} 条）——结构或证据不合法")
    for bad in bundle.invalid_diagnoses:
        lines.append(f"- 拒收：{'；'.join(bad['reasons'])}")
        lines.append(f"  - 原始：{json.dumps(bad['item'], ensure_ascii=False)[:160]}")
    lines.append(f"## 决策请求（字段完整 {len(bundle.valid_decision_requests)} / 被拒 {len(bundle.invalid_decision_requests)}；必要性未复核）")
    for r in bundle.valid_decision_requests:
        lines.append(f"- {r.question}")
        lines.append(f"  - 阻塞原因：{r.blocking_reason}")
        lines.append(f"  - 用户独有信息或授权依据：{r.user_unique_info_or_auth_basis}")
        lines.append(f"  - 已排除的自主路径：{r.excluded_autonomous_paths}")
    for bad in bundle.invalid_decision_requests:
        lines.append(f"- 拒收（缺三要素）：{'；'.join(bad['reasons'])}")
    lines.append(
        f"## 下一动作候选：{bundle.next_action or '（未给出）'}"
        + ("（枚举合法；授权、能力和必要性未复核）" if bundle.next_action_valid else "（非法/缺失）")
    )
    lines.append("")
    return "\n".join(lines)
