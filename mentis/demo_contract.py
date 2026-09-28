"""P1.2 演示：最小任务与反馈契约的实际行为。

数据为脚本化演示，产物为真实生成的文件（内容是样例文案）；
不涉及真实模型或执行器。演示刻意包含两条拒绝路径，
展示不变量而不只展示成功。
"""

import os

from mentis.contracts import (
    DelegationPolicy,
    Disposition,
    Provenance,
    SourcedItem,
)
from mentis.export import render_session_markdown
from mentis.persist import save_session
from mentis.session import TaskSession

DEMO_DIR = os.path.join("runtime", "demo")

PAGE_V1 = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>拾光咖啡 · 周六手冲工作坊（演示样例 v1）</title></head>
<body><h1>周六手冲入门工作坊</h1>
<p>时间：2026-11-14（周六）14:00—16:00｜地点：拾光咖啡（演示地址）｜价格：¥99｜限 8 人</p>
<p>带着好奇来就好——我们像朋友聚会一样聊咖啡、练冲煮。</p>
<p><a href="#signup" id="signup">报名：留下姓名和联系方式（演示表单，不发送数据）</a></p>
</body></html>
"""


def run_demo() -> int:
    os.makedirs(DEMO_DIR, exist_ok=True)
    print("=" * 62)
    print("【演示】P1.2 最小任务与反馈契约 —— 数据为脚本化演示，")
    print("产物为真实生成的文件；不涉及真实模型或执行器。")
    print("演示刻意包含两条拒绝路径（第 4、5 步）。")
    print("=" * 62)

    session = TaskSession(goal="演示本地样例文件的委托、版本与交付契约")

    # 1. 目标与任务定义（含三种来源）
    brief = session.init_brief("写出一份本地 HTML 样例供后续诊断；本包只演示契约，不验收网页质量")
    brief.constraints.append(
        SourcedItem("单页、本地运行、不发送真实数据", Provenance.USER_SELECTED, "user-msg:1")
    )
    brief.preferences.append(
        SourcedItem("温暖、简洁，像朋友聚会不像促销", Provenance.USER_SELECTED, "user-msg:1")
    )
    brief.preferences.append(
        SourcedItem("首屏用大图，日期放显眼位置", Provenance.AGENT_SELECTED, "derived:policy-v1 视觉实现细节")
    )
    brief.preferences.append(
        SourcedItem("可能适合米色底（待首稿反馈验证）", Provenance.PROVISIONAL, "agent-hypothesis:1")
    )
    print("\n[1] Brief v1 已建立：约束与偏好分开记录，来源标注 用户选定/系统推导/暂定。")

    # 2. 委托策略：从用户现有指令推导，不要求填表
    session.set_policy(
        DelegationPolicy(
            policy_id="policy-1",
            version=1,
            decided_scope=["视觉与布局实现细节", "文案措辞微调"],
            reserved_decisions=["活动日期或价格变更", "对外发布"],
            execution_scopes=["workspace:demo"],
            budget={"max_auto_revisions": 2},
            intervention_triggers=["需要新授权范围", "保留决定被触及"],
            provenance=Provenance.AGENT_SELECTED,
            source_ref="derived:user-instruction「你先做，重要决定来问我」",
        )
    )
    print("[2] DelegationPolicy v1 已从用户指令推导：常规细节代决，日期/价格保留给用户。")

    session.authorize("workspace:demo")
    print("[3] 授权 workspace:demo 已由用户授予。")

    # 通知不阻塞
    session.notify_progress("已确认目标与策略，开始制作首稿")
    print("[3a] 通知已记录 —— notify_progress 不阻塞任何动作，无需等待回复。")

    # 3. 首稿：派发 → 真实写出文件 → 完成 → 检查通过 → 接受（与用户满意分离）
    attempt1 = session.dispatch("draft-v1", "workspace:demo")
    session.mark_running(attempt1.attempt_id)
    page_path = os.path.join(DEMO_DIR, "event_page_v1.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(PAGE_V1)
    session.complete_attempt(attempt1.attempt_id, artifact_path=page_path, artifact_producer="executor:simulated(演示)")
    print(f"[3b] attempt-1 执行完成，产物已真实写入 {page_path}（记录哈希）。")

    file_check_passed = os.path.isfile(page_path) and os.path.getsize(page_path) > 0
    session.record_review(
        attempt1.attempt_id,
        findings=[
            "本地 HTML 文件存在且非空" if file_check_passed else "本地 HTML 文件不存在或为空",
            "未执行网页表单、视觉或对比度检查；此处仅演示文件交付契约",
        ],
        passed=file_check_passed,
        disposition=Disposition.ACCEPT,
    )
    session.accept(attempt1.attempt_id)
    session.record_user_feedback(attempt1.attempt_id, satisfied=True, note="演示：用户表示可以")
    print("[3c] 样例文件存在性检查通过 → accept（仅契约演示，网页质量未验证）；")
    print("     用户满意是脚本化反馈，单独记录（完成≠检查通过≠满意）。")

    # 4. 拒绝路径一：用户沉默不能批准
    session.request_decision("价格从 ¥99 调整为 ¥129，是否同意？", blocking_action="apply-price-change")
    print("\n[4] 已发出请求决定（阻塞动作 apply-price-change），保持未回答……")
    try:
        session.dispatch("apply-price-change", "workspace:demo")
        print("    !! 不应到达此处")
    except Exception as exc:
        print(f"    拒绝：{exc}")

    # 5. 拒绝路径二：旧授权不能批准新范围
    print("[5] 尝试以旧授权派发新范围 workspace:demo+network:fetch ……")
    try:
        session.dispatch("fetch-external-assets", "workspace:demo+network:fetch")
        print("    !! 不应到达此处")
    except Exception as exc:
        print(f"    拒绝：{exc}")

    session.notify_progress("演示结束：成功与拒绝路径均已记录")
    print("[6] 结束通知已记录 —— 依然不阻塞。")

    # 导出：JSON + Markdown（同一份数据）
    json_path = save_session(session, os.path.join(DEMO_DIR, "session_demo.json"))
    md_path = os.path.join(DEMO_DIR, "session_demo.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_session_markdown(session, data_origin="脚本化演示数据（非真实模型/执行器）"))
    print(f"\n[7] 会话已保存：{json_path}")
    print(f"    可读导出：{md_path}（内容同下）")
    print("=" * 62)
    print(render_session_markdown(session, data_origin="脚本化演示数据（非真实模型/执行器）"))
    return 0
