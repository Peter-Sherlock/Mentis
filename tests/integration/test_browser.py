"""真实 Chrome 双视口检查；无模型/执行器调用，不证明真实协作效果。"""
import os
from pathlib import Path
import tempfile
import unittest

from mentis.browser_observation import capture_local_html
from mentis.render_measurement import measure_page


@unittest.skipUnless(os.environ.get("MENTIS_BROWSER_INTEGRATION") == "1",
                     "真实浏览器检查需显式设置 MENTIS_BROWSER_INTEGRATION=1")
class BrowserIntegrationTests(unittest.TestCase):
    def test_frozen_page_two_viewports_and_screenshots(self):
        page = Path(__file__).resolve().parents[2] / "docs/evaluation/e1/samples/E1-02/index.html"
        with tempfile.TemporaryDirectory() as directory:
            measured = measure_page(page.resolve(), Path(directory) / "measure")
            self.assertEqual({v.name: v.inner_width for v in measured.viewports},
                             {"mobile": 390, "desktop": 1280})
            self.assertTrue(all(v.overflow_px == 0 and not v.errors for v in measured.viewports))
            captured = capture_local_html(page.resolve(), Path(directory) / "capture")
            self.assertEqual(len(captured.views), 2)
            self.assertTrue(all(v.screenshot_path and not v.error for v in captured.views))
