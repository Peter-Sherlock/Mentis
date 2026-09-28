"""入口检查：可导入、版本一致、子进程真实运行。"""

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
        self.assertIn("DEEPSEEK_API_KEY", result.stdout)
        self.assertIn("demo-contract", result.stdout)
        self.assertIn("diagnose", result.stdout)


if __name__ == "__main__":
    unittest.main()
