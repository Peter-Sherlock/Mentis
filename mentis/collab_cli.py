"""P2.3 CLI：collab 新运行 / collab-resume 恢复。"""

import json
import sys
from pathlib import Path

from mentis.collab import Collaboration, collaboration_from_state


def run_collab(args) -> int:
    interactive = (not args.non_interactive) and sys.stdin.isatty()
    collab = Collaboration(
        goal=args.goal,
        mode="design" if args.design else "autonomous",
        direction=args.direction or "",
        interactive=interactive,
        max_repairs=args.max_repairs,
        timeout_s=args.timeout,
    )
    status = _guarded_run(collab, collab.run)
    print(f"\n[运行结束] 状态 {status}")
    print(f"[报告（看结果/看变化/看依据）] {(collab.run_dir / 'report.md').resolve()}")
    print(f"[作品] {(collab.workspace / 'index.html').resolve()}")
    if status == "PENDING":
        state = (collab.run_dir / "state.json").resolve()
        print(f"[恢复] python -m mentis collab-resume --state \"{state}\" "
              "--answer \"<请求ID>:<你的回答>\"")
    return 0 if status in ("DELIVERED", "PENDING") else 1


def _guarded_run(collab, action) -> str:
    """T3：异常已在 Collaboration 内收尾落盘（ERROR）；这里不再向用户抛裸 traceback。"""
    try:
        return action()
    except Exception as exc:
        print(f"[运行异常] 已按异常收尾并落盘（状态 ERROR）；报告与追踪见 "
              f"{collab.run_dir.resolve()}｜{type(exc).__name__}：{str(exc)[:200]}")
        return "ERROR"


def _parse_answers(pairs: list) -> dict:
    answers = {}
    for pair in pairs or []:
        if ":" not in pair:
            raise ValueError(f"--answer 格式应为 <请求ID>:<回答>，收到 {pair}")
        ix_id, text = pair.split(":", 1)
        answers[ix_id.strip()] = text.strip()
    return answers


def run_resume(args) -> int:
    interactive = (not args.non_interactive) and sys.stdin.isatty()
    collab = collaboration_from_state(args.state, interactive=interactive)
    from mentis.contracts import AttemptStatus

    confirm = AttemptStatus(args.confirm_outcome) if args.confirm_outcome else None
    status = _guarded_run(collab, lambda: collab.resume(
        answers=_parse_answers(args.answer),
        feedback=args.feedback or "",
        goal_new=args.goal_new or "",
        revoke=args.revoke,
        confirm_outcome=confirm,
    ))
    print(f"\n[恢复结束] 状态 {status}")
    print(f"[报告] {(collab.run_dir / 'report.md').resolve()}")
    print(f"[作品] {(collab.workspace / 'index.html').resolve()}")
    if status == "PENDING":
        state = (collab.run_dir / "state.json").resolve()
        print(f"[恢复] python -m mentis collab-resume --state \"{state}\" "
              "--answer \"<请求ID>:<你的回答>\"")
    return 0 if status in ("DELIVERED", "PENDING", "WITHDRAWN") else 1
