"""Mentis P1.1 最小运行入口。

只显示版本与启动信息，用于验证工程起点；不接模型、不假装完整系统。
"""

import argparse
import platform
import sys

from mentis import __version__

BANNER = """\
Mentis v{version} — P1.1 最小运行入口
当前阶段：未接入模型、执行器与存储；本入口仅用于验证工程起点。
路线与状态见 docs/DEVELOPMENT_STATUS.md。
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mentis", description="Mentis 最小运行入口")
    parser.add_argument("--version", action="version", version=f"mentis {__version__}")
    parser.parse_args(argv)
    print(BANNER.format(version=__version__).rstrip())
    print(f"Python {platform.python_version()} on {platform.system()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
