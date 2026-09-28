"""P1.1 必要开发检查：入口可导入、版本一致、子进程真实运行。"""

import subprocess
import sys
import unittest

from mentis import __version__


class EntryTests(unittest.TestCase):
    def test_version_format(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_module_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "mentis", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)

    def test_module_default_run(self):
        result = subprocess.run(
            [sys.executable, "-m", "mentis"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)
        self.assertIn("P1.1", result.stdout)
        self.assertIn("未接入模型", result.stdout)


if __name__ == "__main__":
    unittest.main()
