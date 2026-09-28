"""P3.2 CLI：guide-revise 指导修订循环（真实执行器与模型需显式 --real）。"""

import sys
from pathlib import Path


def run_guide_revise(args) -> int:
    if not args.real:
        print("【未发送数据】真实指导链需要显式加 --real。")
        print("将执行：冻结首稿 → 真实视口截图与渲染测量 → DeepSeek 诊断（含双视口截图输入，"
              "最多每轮 2 次请求）→ 组装修改指令 → 真实 Codex 修订（单次 ≤600 秒）→ 独立复检。")
        print("基线须是 P2.3 运行目录（含 workspace/index.html）；"
              "执行器使用本机 Codex 登录态；无货币硬上限，仅记录用量。")
        print("示例：py -3.14 -m mentis guide-revise --baseline runtime/collab/<运行ID> --real")
        return 2
    from mentis.browser_observation import capture_local_html
    from mentis.guidance import GuidanceCycle

    cycle = GuidanceCycle(
        baseline_dir=args.baseline,
        max_rounds=args.max_rounds,
        timeout_s=args.timeout,
        use_model=True,
        include_images=not args.no_images,
        run_id=args.run_id or "",
    )
    status = cycle.run()
    print(f"\n[运行结束] 状态 {status}")
    print(f"[报告（看结果/看变化/看依据）] {(cycle.run_dir / 'report.md').resolve()}")
    if status == "NEEDS_DECISION":
        state = (cycle.run_dir / "state.json").resolve()
        pending = [ix for ix in cycle.session.interactions
                   if ix.kind.value == "request_decision" and ix.answer_source is None]
        for ix in pending:
            print(f"[需要你决定] {ix.interaction_id}：{ix.message}")
        print(f"[恢复] py -3.14 -m mentis guide-resume --state \"{state}\" "
              "--answer \"<请求ID>:<你的回答>\"")
    return 0 if status in ("IMPROVED", "QUALIFIED_NO_CHANGE") else 1


def run_guide_resume(args) -> int:
    from mentis.guidance import guidance_from_state

    cycle = guidance_from_state(args.state)
    if args.reverify:
        status = cycle.reverify()
    else:
        status = cycle.resume(_parse_answers(args.answer), retry=args.retry)
    print(f"\n[恢复结束] 状态 {status}")
    print(f"[报告] {(cycle.run_dir / 'report.md').resolve()}")
    return 0 if status in ("IMPROVED", "QUALIFIED_NO_CHANGE") else 1


def _parse_answers(pairs: list) -> dict:
    answers = {}
    for pair in pairs or []:
        if ":" not in pair:
            raise ValueError(f"--answer 格式应为 <请求ID>:<回答>，收到 {pair}")
        ix_id, text = pair.split(":", 1)
        answers[ix_id.strip()] = text.strip()
    return answers
