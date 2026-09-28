"""P1.2 可读导出：同一份会话数据渲染为 Markdown，供"看依据"式检查。

导出不复制状态——所有内容来自 TaskSession；摘要不构成第二事实来源。
"""

from mentis.contracts import (
    AttemptStatus,
    Disposition,
    InteractionKind,
    PROVENANCE_LABEL,
    Provenance,
)
from mentis.persist import session_to_dict

_DISPOSITION_LABEL = {
    Disposition.ACCEPT: "通过",
    Disposition.REVISE: "需修订",
    Disposition.ASK_USER: "问用户",
    Disposition.ESCALATE: "升级",
}

_STATUS_LABEL = {
    AttemptStatus.PLANNED: "已计划",
    AttemptStatus.DISPATCHED: "已派发",
    AttemptStatus.RUNNING: "执行中",
    AttemptStatus.COMPLETED: "已完成",
    AttemptStatus.FAILED: "失败",
    AttemptStatus.INTERRUPTED: "已中断",
    AttemptStatus.UNKNOWN: "未知（未确认外部状态，不自动重发）",
}


def render_session_markdown(session, data_origin: str = "真实记录") -> str:
    d = session_to_dict(session)
    lines = []
    add = lines.append

    add(f"# Mentis 任务记录：{session.goal}")
    add("")
    add(f"- 数据来源：**{data_origin}**；契约版本 {d['contract_version']}；创建于 {d['created_at']}")
    add("")

    add("## 目标与任务定义（版本化）")
    for b in d["briefs"]:
        state = f"已被 v{b['superseded_by']} 替代" if b["superseded_by"] else "当前版本"
        add(f"- **{b['brief_id']}（v{b['version']}，{state}）**：{b['objective']}")
        for item in b["constraints"]:
            add(_item_line("约束", item))
        for item in b["preferences"]:
            add(_item_line("偏好", item))
    add("")

    add("## 委托策略")
    for p in d["policies"]:
        add(f"- **{p['policy_id']}（v{p['version']}）**")
        add(f"  - 可代决：{'；'.join(p['decided_scope']) or '（无）'}")
        add(f"  - 保留给用户决定：{'；'.join(p['reserved_decisions']) or '（无）'}")
        add(f"  - 执行范围：{'，'.join(p['execution_scopes'])}")
        add(f"  - 预算：{p['budget']}")
        add(f"  - 介入触发：{'；'.join(p['intervention_triggers']) or '（无）'}")
        add(
            f"  - 来源：{PROVENANCE_LABEL[Provenance(p['provenance'])]}"
            f"（{p['source_ref']}）"
        )
    add("")

    add("## 授权")
    for a in d["authorizations"]:
        add(f"- {a['auth_id']}：范围 {a['scope']}，状态 **{a['status']}**，授予者 {a['granted_by']}（{a['granted_at']}）")
    add("")

    add("## 交互记录（通知不等待；请求决定只阻塞依赖动作）")
    for ix in d["interactions"]:
        if ix["kind"] == InteractionKind.NOTIFY_PROGRESS.value:
            add(f"- [通知] {ix['message']}（{ix['created_at']}）——不阻塞任何动作")
        else:
            if ix["answer_source"] is None:
                state = "**未回答——沉默不构成批准**，依赖动作保持阻塞"
            elif ix["answer_source"] == "user":
                state = f"已由用户回答：「{ix['answer_text']}」"
            else:
                state = f"由既有授权覆盖：「{ix['answer_text']}」"
            add(
                f"- [请求决定] {ix['message']}（阻塞动作：{ix['blocking_action']}）——{state}"
            )
    add("")

    add("## 执行、产物与审核（完成 ≠ 通过 ≠ 用户满意）")
    for a in d["attempts"]:
        accepted = "已接受" if a["accepted"] else "未接受"
        add(
            f"- **{a['attempt_id']}**：动作 {a['action_key']}，brief v{a['brief_version']}，"
            f"范围 {a['execution_scope']}，原生身份 {a['native_task_id']}，"
            f"状态 **{_STATUS_LABEL[AttemptStatus(a['status'])]}**，{accepted}"
        )
        for x in d["artifacts"]:
            if x["attempt_id"] == a["attempt_id"]:
                add(f"  - 产物 {x['artifact_id']}：`{x['path']}`（sha256 前 12 位 {x['sha256'][:12]}，生成者 {x['producer']}）")
        for r in d["reviews"]:
            if r["attempt_id"] == a["attempt_id"]:
                verdict = "通过" if r["passed"] else "未通过"
                add(
                    f"  - 检查 {r['review_id']}：{verdict}，处置 {_DISPOSITION_LABEL[Disposition(r['disposition'])]}"
                    f"（发现：{'；'.join(r['findings']) or '无'}）"
                )
        fb = d["user_feedback"].get(a["attempt_id"])
        if fb:
            feeling = "满意" if fb["satisfied"] else "不满意"
            add(f"  - 用户反馈（单独记录）：{feeling}——{fb['note'] or '（无备注）'}（{fb['at']}）")
    add("")

    add("## 修订链（哪条反馈触发、从哪个尝试到哪个尝试）")
    if d["revisions"]:
        for r in d["revisions"]:
            add(
                f"- {r['revision_id']}（第 {r['round_no']} 轮）：反馈 {r['feedback_interaction_id']}"
                f" → {r['from_attempt_id']} → {r['to_attempt_id'] or '（待派发）'}；"
                f"指令：{r['instruction']}"
            )
    else:
        add("- （无修订）")
    add("")
    return "\n".join(lines)


def _item_line(kind: str, item: dict) -> str:
    label = PROVENANCE_LABEL[Provenance(item["provenance"])]
    return f"  - {kind}：{item['text']}（{label}，{item['source_ref']}）"
