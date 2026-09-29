"""P5.2a 机制冒烟：field 会话包装的记录能力（全部离线，FakeAdapter）。

纪律：这些是**开发者机制验证**，标注为机制检查，不冒充真实使用证据；
P5.2a 的真实使用记录只能来自项目负责人本人的操作。
"""

import json
import tempfile
import unittest
from pathlib import Path

from mentis.field import FieldSession, record_feedback, run_field_feedback, run_field_start

try:
    from tests.test_collab import FakeAdapter, stub_capture
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import FakeAdapter, stub_capture


def make_factory(adapter, design: bool = False, base_dir=None):
    def factory():
        from mentis.collab import Collaboration

        return Collaboration(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）",
            mode="design" if design else "autonomous",
            adapter=adapter, gateway=None, browser_capture=stub_capture,
            use_model=False, interactive=False, base_dir=base_dir)
    return factory


class FieldSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_field_start_records_session_and_usage_record(self):
        """机制检查：操作、系统行为、研究者提示、阻塞、证据入口全部入档。"""
        status, session_dir, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）",
            interactive=False, base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(), base_dir=self.root / "collab"))
        self.assertEqual(status, "DELIVERED")
        record = (session_dir / "usage-record.md").read_text(encoding="utf-8")
        for section in ("实际操作", "系统行为", "阻塞与等待", "用户原话", "证据入口"):
            self.assertIn(section, record)
        self.assertIn("研究者提示", record)
        self.assertIn("field-start", record)
        self.assertIn("负责人自用", record)
        self.assertIn("尚未提交", record)  # 无反馈时如实写未提交，不虚构
        self.assertIn("委托 build-page", record)  # 系统行为来自运行记录
        self.assertIn("agent_selected", record)  # 系统贡献带来源标注
        log = (session_dir / "session-log.txt").read_text(encoding="utf-8")
        self.assertIn("[会话开始]", log)
        self.assertIn("[通知]", log)  # 系统实际输出留痕
        self.assertIn("[会话结束]", log)
        # 运行目录产物未被会话包装改动（report/state 原样）
        self.assertTrue((run_dir / "state.json").is_file())

    def test_feedback_verbatim_bound_and_persisted(self):
        """机制检查：逐字反馈入会话记录（绑定尝试）并持久化往返。"""
        _status, _session_dir, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）",
            interactive=False, base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(behavior={"first": "good"}), base_dir=self.root / "collab"))
        entry = record_feedback(
            run_dir, satisfied=False,
            quote="首屏太挤了，价格我没看到；改回插画大一点",
            blocks="等构建的时候不知道进展到哪一步")
        self.assertEqual(entry["bound_attempt"], "attempt-1")
        session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        feedback = session["user_feedback"]["attempt-1"]
        self.assertFalse(feedback["satisfied"])
        self.assertIn("首屏太挤", feedback["note"])  # 原话逐字
        feedback_md = (run_dir / "field-feedback.md").read_text(encoding="utf-8")
        self.assertIn("等构建的时候不知道进展到哪一步", feedback_md)
        # 持久化往返不丢
        from mentis.persist import session_from_dict
        restored = session_from_dict(session)
        self.assertIn("首屏太挤", restored.user_feedback["attempt-1"]["note"])

    def test_feedback_without_attempts_degrades_honestly(self):
        """机制检查：待决中（零尝试）的运行不伪造契约绑定，反馈只入档案并标注。"""
        _status, _s, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）", design=True,
            interactive=False, base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(), design=True, base_dir=self.root / "collab"))
        self.assertEqual(_status, "PENDING")
        entry = record_feedback(run_dir, satisfied=False, quote="还没出结果，先不评价")
        self.assertEqual(entry["bound_attempt"], "（无）")
        feedback_md = (run_dir / "field-feedback.md").read_text(encoding="utf-8")
        self.assertIn("没有已完成尝试", feedback_md)
        session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["user_feedback"], {})  # 契约记录未被伪造

    def test_field_feedback_rejects_empty_quote(self):
        _status, _s, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）",
            interactive=False, base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(), design=True, base_dir=self.root / "collab"))
        with self.assertRaises(ValueError):
            run_field_feedback(run_dir, satisfied=True, quote="   ", base_dir=self.root / "field")

    def test_blocking_lines_list_pending_decisions(self):
        """机制检查：待决请求作为阻塞项列出（沉默≠批准）。"""
        _status, _s, run_dir = run_field_start(
            goal="为拾光咖啡工作坊制作活动介绍页（机制冒烟）", design=True,
            interactive=False, base_dir=self.root / "field", collab_base_dir=self.root / "collab",
            collab_factory=make_factory(FakeAdapter(), design=True, base_dir=self.root / "collab"))
        fs = FieldSession(label="probe", base_dir=self.root / "field")
        lines = fs._blocking_lines(run_dir)
        fs.end_capture()
        fs._log.close()
        self.assertTrue(any("待决" in line and "沉默≠批准" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
