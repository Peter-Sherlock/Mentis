"""Mentis 开发入口。

P2.2 起含真实模型诊断命令（需 DEEPSEEK_API_KEY，可由环境或项目 .env 提供）；其余为离线演示与验证。
"""

import argparse
import json
import platform
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mentis import STAGE, __version__

BANNER = """\
Mentis v{version} — 开发入口（当前：{stage}）
真实模型命令需要 DEEPSEEK_API_KEY（环境变量或项目 .env）；演示与离线验证无需凭据。
命令：
  python -m mentis                本说明
  python -m mentis --version      版本号
  python -m mentis demo-contract  最小任务与反馈契约演示（脚本化数据，含拒绝路径）
  python -m mentis demo-routing   首领域资源按需加载与缺口分流演示（确定性规则）
  python -m mentis diagnose       真实模型页面诊断（结构观察→候选→校验；--fixture 为 Mock 模式）
  python -m mentis observe-page   本地 HTML 双视口截图（P3.1，静态初始画面）
  python -m mentis collab         默认自主委托的协作运行（真实执行器+截图；--design 为共同设计）
  python -m mentis collab-resume  恢复运行：回答请求 / 反馈修订 / 改目标 / 撤回
  python -m mentis guide-revise   P3.2 指导修订循环：冻结首稿→观察测量→诊断→指令→修订→复检
  python -m mentis guide-resume   回答诊断提出的用户决定后继续修订
路线与状态见 docs/DEVELOPMENT_STATUS.md。
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mentis", description="Mentis 开发入口")
    parser.add_argument("--version", action="version", version=f"mentis {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo-contract", help="P1.2 契约演示（脚本化数据）")
    sub.add_parser("demo-routing", help="P2.1 资源选择与缺口分流演示（确定性规则）")
    diag = sub.add_parser("diagnose", help="P2.2 模型诊断（真实调用需凭据；--fixture 为 Mock）")
    diag.add_argument("--page", required=True, help="要诊断的 HTML 页面文件路径")
    diag.add_argument("--goal", default="", help="本轮目标（可留空）")
    diag.add_argument("--feedback", default="", help="用户反馈原话（可留空）")
    diag.add_argument("--fixture", default="", help="Mock 固定响应文件（离线验证模式）")
    diag.add_argument("--real", action="store_true", help="明确允许本次调用将页面结构、目标和反馈发送给 DeepSeek；最多两次请求")
    observe = sub.add_parser("observe-page", help="本地 HTML 双视口截图；不执行点击或提交")
    observe.add_argument("--page", required=True, help="本地 HTML 文件路径")
    observe.add_argument("--out", default="", help="输出目录；默认 runtime/observations/<运行编号>")
    col = sub.add_parser("collab", help="P2.3 协作运行：目标→自主委托→产物→观察（需 Codex 登录态）")
    col.add_argument("--goal", required=True, help="本轮网页目标")
    col.add_argument("--design", action="store_true", help="共同设计模式（先确认风格方向；默认自主）")
    col.add_argument("--direction", default="", help="直接给出风格方向（跳过方向提问）")
    col.add_argument("--non-interactive", action="store_true", help="不等待输入：待决事项保持未决")
    col.add_argument("--max-repairs", type=int, default=1, help="自动修复轮数上限（默认 1）")
    col.add_argument("--timeout", type=int, default=420, help="单次执行器超时秒数（1—600）")
    res = sub.add_parser("collab-resume", help="恢复协作运行：回答/反馈/改目标/撤回")
    res.add_argument("--state", required=True, help="runtime/collab/<run>/state.json")
    res.add_argument("--answer", action="append", default=[], help="<请求ID>:<回答>，可多次")
    res.add_argument("--feedback", default="", help="用户修正（进入新 brief 版本并续接修订）")
    res.add_argument("--goal-new", default="", help="改变目标（旧版本结果不再适用）")
    res.add_argument("--revoke", action="store_true", help="撤回授权（后续委托全部拒绝）")
    res.add_argument("--confirm-outcome", default="", choices=["", "completed", "failed"],
                     help="unknown 的人工核对确认（须先自行核对执行器与产物）")
    res.add_argument("--non-interactive", action="store_true", help="不等待输入")
    guide = sub.add_parser("guide-revise", help="P3.2 指导修订循环（真实运行需 --real）")
    guide.add_argument("--baseline", required=True, help="P2.3 运行目录（含 workspace/index.html）")
    guide.add_argument("--real", action="store_true",
                       help="明确允许：发送结构/测量/截图至 DeepSeek，并调用本机 Codex 修订")
    guide.add_argument("--max-rounds", type=int, default=2, help="自动修改轮数上限（默认 2）")
    guide.add_argument("--timeout", type=int, default=600, help="单次执行器超时秒数（1—600）")
    guide.add_argument("--no-images", action="store_true", help="诊断不发送截图（仅结构与测量）")
    guide.add_argument("--run-id", default="", help="指定运行目录 ID（默认时间戳）")
    gres = sub.add_parser("guide-resume", help="回答诊断请求的用户决定后续办，或核对后续办失败轮")
    gres.add_argument("--state", required=True, help="runtime/guidance/<run>/state.json")
    gres.add_argument("--answer", action="append", default=[], help="<请求ID>:<回答>，可多次")
    gres.add_argument("--retry", action="store_true",
                      help="核对后续办失败轮（确认无活跃写入者；计入修订预算）")
    gres.add_argument("--reverify", action="store_true",
                      help="用修正后的确定性工具对已有 v2 重新复检（透明更正，不重新委托）")
    args = parser.parse_args(argv)

    if args.command == "demo-contract":
        from mentis.demo_contract import run_demo

        return run_demo()
    if args.command == "demo-routing":
        from mentis.capabilities.demo_routing import run_demo

        return run_demo()
    if args.command == "diagnose":
        from mentis.diagnose_cli import run_diagnose

        return run_diagnose(args)
    if args.command == "observe-page":
        from mentis.browser_observation import capture_local_html

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(args.out) if args.out else Path("runtime/observations") / f"{stamp}_{uuid4().hex[:6]}"
        try:
            observation = capture_local_html(args.page, out)
        except (ValueError, FileNotFoundError) as exc:
            print(f"观察失败：{exc}", file=sys.stderr)
            return 2
        manifest = Path(out) / "observation.json"
        manifest.write_text(json.dumps(asdict(observation), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[观察记录] {manifest}")
        for view in observation.views:
            print(f"[{view.name}] {view.actual_width}×{view.actual_height} "
                  f"{view.screenshot_path or view.error}")
        print("[边界] " + "；".join(observation.limitations))
        return 0 if all(view.screenshot_path and not view.error for view in observation.views) else 1
    if args.command == "collab":
        from mentis.collab_cli import run_collab

        return run_collab(args)
    if args.command == "collab-resume":
        from mentis.collab_cli import run_resume

        return run_resume(args)
    if args.command == "guide-revise":
        from mentis.guidance_cli import run_guide_revise

        return run_guide_revise(args)
    if args.command == "guide-resume":
        from mentis.guidance_cli import run_guide_resume

        return run_guide_resume(args)

    print(BANNER.format(version=__version__, stage=STAGE).rstrip())
    print(f"Python {platform.python_version()} on {platform.system()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
