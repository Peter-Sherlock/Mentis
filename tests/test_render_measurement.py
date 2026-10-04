"""渲染测量异常退出的真进程复现（P2.4-E2 运行前准备·审计轮，2026-10-03）。

审计场景：受限 shell 中浏览器异常退出 → 双视口测量逐视口报错 → 端到端流程
如实判 UNVERIFIED（设计行为"观察不可信则不验收"，非缺陷）。本文件用**真实
子进程**复现该路径（python.exe 充当必然失败的"浏览器"：不认 --headless=new，
非零退出，零 Mock、零 Fake runner），并覆盖同轮修复的两处缺陷：

1. 浏览器无法启动（OSError，如沙箱拒绝创建进程）此前会炸穿观察链到 ERROR
   收尾——现在逐视口记账"浏览器无法启动"，由下游按测量不可信处理。
2. Windows 上 chrome.exe --version 曾打印界面提示且退出码 0，提示文本被当作
   browser_version 记入测量记录——现在只有版本号样式的输出才采信。

本文件不含任何模型/执行器调用；python.exe 进程是真实的，属离线测试。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mentis.render_measurement import extract_browser_version, measure_viewport

_PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>t</title></head>"
         "<body><p style=\"width:2000px\">宽段落</p></body></html>\n")


class RealAbnormalExitTests(unittest.TestCase):
    """真实子进程异常退出：逐视口记账；零值伴随错误，不得解读为"零溢出通过"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.page = self.out / "page.html"
        self.page.write_text(_PAGE, encoding="utf-8")

    def test_real_process_abnormal_exit_recorded_not_pass(self):
        # 真实进程非零退出（python.exe 拒绝未知选项），stdout 为空、无测量报告
        result = measure_viewport(self.page, "mobile", 390, 844, self.out,
                                  Path(sys.executable), 30, subprocess.run)
        self.assertTrue(result.errors, "异常退出必须逐视口记账错误")
        self.assertTrue(any("退出码" in e for e in result.errors))
        self.assertEqual(result.inner_width, 0)  # 无数据不得冒充测量值
        self.assertEqual(result.overflow_px, 0)

    def test_launch_oserror_recorded_per_viewport_not_raised(self):
        # 沙箱禁止进程创建：runner 抛 OSError（修复前会炸穿观察链到 ERROR 收尾）
        def denying_runner(cmd, **kw):
            raise PermissionError(5, "模拟沙箱拒绝创建进程")

        result = measure_viewport(self.page, "desktop", 1280, 800, self.out,
                                  Path("C:/nonexistent/browser.exe"), 30, denying_runner)
        self.assertTrue(any("浏览器无法启动" in e for e in result.errors))
        self.assertEqual(result.inner_width, 0)
        self.assertEqual(result.overflow_px, 0)


class BrowserVersionValidationTests(unittest.TestCase):
    def test_ui_message_and_failure_never_mistaken_as_version(self):
        # 2026-10-03 本机真实复现：chrome.exe --version 退出码 0、stdout 是界面提示
        self.assertEqual(extract_browser_version("已在现有的浏览器会话中打开。", 0),
                         "unknown")
        self.assertEqual(extract_browser_version("", 0), "unknown")
        self.assertEqual(extract_browser_version("Chrome 154.0.8037.58", 1), "unknown")

    def test_version_like_output_kept(self):
        self.assertEqual(extract_browser_version("Microsoft Edge 154.0.8037.58", 0),
                         "Microsoft Edge 154.0.8037.58")
        self.assertEqual(extract_browser_version("Python 3.14.0", 0), "Python 3.14.0")


if __name__ == "__main__":
    unittest.main()
