"""Mentis 开发入口。

当前阶段不接真实模型、执行器与存储；命令用于工程验证与演示。
"""

import argparse
import platform
import sys

from mentis import STAGE, __version__

BANNER = """\
Mentis v{version} — 开发入口（当前：{stage}）
未接入真实模型、执行器与存储；本入口用于工程验证。
命令：
  python -m mentis                本说明
  python -m mentis --version      版本号
  python -m mentis demo-contract  最小任务与反馈契约演示（脚本化数据，含拒绝路径）
  python -m mentis demo-routing   首领域资源按需加载与缺口分流演示（确定性规则）
路线与状态见 docs/DEVELOPMENT_STATUS.md。
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mentis", description="Mentis 开发入口")
    parser.add_argument("--version", action="version", version=f"mentis {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo-contract", help="P1.2 契约演示（脚本化数据）")
    sub.add_parser("demo-routing", help="P2.1 资源选择与缺口分流演示（确定性规则）")
    args = parser.parse_args(argv)

    if args.command == "demo-contract":
        from mentis.demo_contract import run_demo

        return run_demo()
    if args.command == "demo-routing":
        from mentis.capabilities.demo_routing import run_demo

        return run_demo()

    print(BANNER.format(version=__version__, stage=STAGE).rstrip())
    print(f"Python {platform.python_version()} on {platform.system()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
