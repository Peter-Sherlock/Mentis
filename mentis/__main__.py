"""Mentis 开发入口。

P2.2 起含真实模型诊断命令（需 DEEPSEEK_API_KEY）；其余为离线演示与验证。
"""

import argparse
import platform
import sys

from mentis import STAGE, __version__

BANNER = """\
Mentis v{version} — 开发入口（当前：{stage}）
真实模型命令需要 DEEPSEEK_API_KEY；演示与离线验证无需凭据。
命令：
  python -m mentis                本说明
  python -m mentis --version      版本号
  python -m mentis demo-contract  最小任务与反馈契约演示（脚本化数据，含拒绝路径）
  python -m mentis demo-routing   首领域资源按需加载与缺口分流演示（确定性规则）
  python -m mentis diagnose       真实模型页面诊断（结构观察→候选→校验；--fixture 为 Mock 模式）
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

    print(BANNER.format(version=__version__, stage=STAGE).rstrip())
    print(f"Python {platform.python_version()} on {platform.system()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
