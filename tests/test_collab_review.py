"""P2.4-T4 补修回归例：collab 交付前专业评审与指导修订（全部离线，注入替身）。

覆盖（用户验证要求）：
- 有据问题触发修订（候选审查采纳 → 修订指令 → 复检 improved → 交付）；
- 不成立（证据与测量矛盾）或仅"保持现状"的候选不触发修改；
- 合格不改（零候选/采纳 0 → 交付并留接受依据）；
- 缺证据不冒充评审通过（无截图=视觉未验证；测量不可信=复检 unverified，不判通过）；
- 修订回归、预算耗尽、修订执行 unknown；
- 目标、约束、旧版产物与修改依据可追溯。
诊断候选与模型复查响应为脚本化 Mock；测试反馈不含负责人原话。
"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mentis.collab import Collaboration
from mentis.contracts import AttemptStatus
from mentis.render_measurement import (RenderMeasurement, ViewportMeasurement,
                                       measure_page)

try:
    from tests.test_collab import GOOD_PAGE, BAD_PAGE
    from tests.test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                                     DIAGNOSIS_GENERIC, DIAGNOSIS_NEEDS_USER,
                                     DIAGNOSIS_OVERFLOW, DIAGNOSIS_QUALIFIED,
                                     FakeGateway, file_aware_measure, stub_capture)
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import GOOD_PAGE, BAD_PAGE
    from test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                               DIAGNOSIS_GENERIC, DIAGNOSIS_NEEDS_USER,
                               DIAGNOSIS_OVERFLOW, DIAGNOSIS_QUALIFIED,
                               FakeGateway, file_aware_measure, stub_capture)

GOAL = "为拾光咖啡工作坊制作活动介绍页"
V2_MARKER = "<!-- review-fix-applied -->"


class ReviewAdapter:
    """假执行器：首轮写好页（结构通过）；修订轮写修订页；记录确切指令。"""

    cli_version = "fake-0"

    def __init__(self, revision_page=None, revision_status=None, thread="t-fake-review"):
        self.revision_page = revision_page
        self.revision_status = revision_status
        self.thread = thread
        self.prompts: list = []

    def _emit(self, spec, status):
        from mentis.executors.codex_cli import CodexTaskResult

        return CodexTaskResult(
            task_id="task-review", attempt_id=f"attempt-{len(self.prompts):04d}",
            brief_version=spec.brief_version, workspace=str(spec.workspace),
            status=status, native_thread_id=self.thread,
            usage={"input_tokens": 100, "output_tokens": 10},
            artifact_paths=["index.html"], cli_version="fake")

    def _run(self, spec):
        self.prompts.append(spec.prompt)
        if len(self.prompts) == 1:
            Path(spec.workspace).joinpath("index.html").write_text(GOOD_PAGE, encoding="utf-8")
            return self._emit(spec, AttemptStatus.COMPLETED)
        if self.revision_status is not None:
            return self._emit(spec, self.revision_status)
        Path(spec.workspace).joinpath("index.html").write_text(
            self.revision_page or GOOD_PAGE + V2_MARKER, encoding="utf-8")
        return self._emit(spec, AttemptStatus.COMPLETED)

    def start_task(self, spec):
        return self._run(spec)

    def continue_task(self, thread, task_id, spec):
        assert thread == self.thread
        return self._run(spec)


def broken_measure(page, out_dir):
    """测量替身：全部视口报错且视口尺寸异常（不可信）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    viewports = []
    for name, (w, h) in (("mobile", (390, 844)), ("desktop", (1280, 800))):
        viewports.append(ViewportMeasurement(
            name=name, width=w, height=h, inner_width=0, scroll_width=0, overflow_px=0,
            errors=["注入的测量错误"]))
    return RenderMeasurement(
        source_path=str(page),
        source_sha256=hashlib.sha256(Path(page).read_bytes()).hexdigest(),
        browser="fake", browser_version="0", viewports=viewports,
        limitations=["测试替身：不可信测量"], created_at="2026-09-30T00:00:00")


DIAGNOSIS_REJECT_AND_KEEP = json.dumps({
    "diagnoses": [
        {"phenomenon": "移动 390 视口横向溢出 80px",
         "evidence_refs": ["mv:mobile"], "severity": "blocking",
         "interpretation": "候选主张存在横向溢出",
         "modification_candidates": ["修复溢出"]},
        {"phenomenon": "当前信息密度与视觉层级合适",
         "evidence_refs": ["h:1"], "severity": "minor",
         "interpretation": "当前版面状态良好",
         "modification_candidates": ["保持现状，不做调整"]},
    ],
    "next_action": "agent_decide", "decision_requests": [],
}, ensure_ascii=False)


class ProfessionalReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, gateway, adapter, measure=None, browser_capture=stub_capture, **kw):
        collab = Collaboration(
            goal=GOAL, adapter=adapter, gateway=gateway,
            browser_capture=browser_capture, measure=measure or file_aware_measure,
            use_model=True, base_dir=self.root / "collab", **kw)
        collab._blind_rng = _FixedCoin()  # 盲比 A/B 指派固定（Mock 响应语义匹配）
        return collab

    def test_evidence_backed_candidate_triggers_revision_and_reverify(self):
        """有据候选 → 采纳 → 修订指令（证据/保留/约束/复检办法）→ 复检 improved → 交付。"""
        from mentis.trace import load_events

        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y]), adapter)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertEqual(len(adapter.prompts), 2)  # 构建 + 专业评审修订
        instruction = adapter.prompts[1]
        for section in ("[Mentis 诊断", "(d1)", "证据：", "[必须保留的活动事实",
                        "[原约束", "¥99", "复检", "保持单文件"):
            self.assertIn(section, instruction)  # 位置/证据/保留/约束/复检办法
        # 确切指令先落盘且哈希=实际发送
        rev_attempt_id = collab.session.attempts[-1].attempt_id
        prompt_file = collab.run_dir / "trace" / "prompts" / f"{rev_attempt_id}.md"
        self.assertTrue(prompt_file.is_file())
        self.assertEqual(hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
                         hashlib.sha256(instruction.encode("utf-8")).hexdigest())
        # 候选审查记录：采纳+理由；产物版本绑定
        review_doc = json.loads((collab.run_dir / "professional-review.json")
                                .read_text(encoding="utf-8"))
        self.assertEqual(review_doc["candidates"][0]["decision"], "adopt")
        self.assertTrue(review_doc["reviewed_artifact_sha256"])
        self.assertIn("同源模型", review_doc["source_note"])  # 独立性未建立如实记录
        # 复检绑定当前产物版本；盲比两序一致 → 判定 improved（V2：不再由非盲复查授予）
        verify_doc = json.loads((collab.run_dir / "professional-verify.json")
                                .read_text(encoding="utf-8"))
        self.assertTrue(verify_doc["artifact_changed"])
        self.assertEqual(verify_doc["blind_review"]["overall"], "after_better")
        self.assertTrue(verify_doc["blind_review"]["consistent"])
        self.assertEqual(verify_doc["verdict"], "improved")
        blind_doc = json.loads((collab.run_dir / "blind-review-professional.json")
                               .read_text(encoding="utf-8"))
        self.assertEqual(blind_doc["reviewed_after_sha256"], verify_doc["reviewed_html_sha256"])
        self.assertTrue(blind_doc["mock"])  # Mock 标注
        self.assertEqual(collab.last_professional_review["verdict"], "improved")
        # 修改依据可追溯：revision 记录 + 旧版产物快照
        revision = collab.session.revisions[-1]
        self.assertEqual(revision.from_attempt_id, collab.session.attempts[0].attempt_id)
        self.assertEqual(revision.to_attempt_id, rev_attempt_id)
        self.assertTrue((collab.run_dir / "versions" /
                         f"index-{collab.session.attempts[0].attempt_id}.html").is_file())
        # trace：观察/诊断/候选审查/复检事件齐备
        kinds = [e["kind"] for e in load_events(collab.run_dir)]
        for kind in ("observation.structure", "observation.render",
                     "observation.diagnosis", "review.recorded"):
            self.assertIn(kind, kinds)
        routed = [e for e in load_events(collab.run_dir) if e["kind"] == "decision.routed"
                  and "候选审查" in e["summary"]]
        self.assertTrue(routed and "adopt" in routed[0]["summary"])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("交付前专业评审：已执行", report)
        self.assertIn("修订复检判定 **improved**", report)

    def test_unfounded_and_keepasis_candidates_do_not_trigger_revision(self):
        """溢出主张与可信测量矛盾 → 驳回；"保持现状/保留当前"→ 不进入修订任务；不委托修订。"""
        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([DIAGNOSIS_REJECT_AND_KEEP]), adapter)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertEqual(len(adapter.prompts), 1)  # 未发生修订委托
        review_doc = json.loads((collab.run_dir / "professional-review.json")
                                .read_text(encoding="utf-8"))
        decisions = {c["no"]: c["decision"] for c in review_doc["candidates"]}
        self.assertEqual(decisions["d1"], "reject")       # 主张溢出但测量 0px
        self.assertIn("0px", review_doc["candidates"][0]["reason"])
        self.assertEqual(decisions["d2"], "keep_as_is")   # 保持现状不进入修订任务
        self.assertIn("不为增加交流轮数强行修改", review_doc["candidates"][1]["reason"])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("自主修订 0", report)
        self.assertIn("合格不改", report)  # 接受依据=审查记录

    def test_keep_current_wording_variants_not_revised(self):
        """"保留当前…待用户确认"类候选（真实运行 d1 的措辞）同样不进入修订任务。

        真实核验（20261001_020003-73ca）曾因 keep 提示词未覆盖"保留当前"措辞，
        把该类候选采纳并触发了一次近维持现状的修订——修正后由本测试防回归。
        """
        diagnosis = json.dumps({
            "diagnoses": [{
                "phenomenon": "配色为低饱和暖色（米白、陶土、墨绿），无促销元素",
                "evidence_refs": ["h:1"], "severity": "minor",
                "interpretation": "方向符合偏好；样例待用户确认",
                "modification_candidates": ["保留当前暖色低饱和配色作为候选样例，待用户确认",
                                            "若用户反馈偏冷，可微调饱和度"]}],
            "next_action": "agent_decide", "decision_requests": [],
        }, ensure_ascii=False)
        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([diagnosis]), adapter)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertEqual(len(adapter.prompts), 1)  # 不委托修订
        review_doc = json.loads((collab.run_dir / "professional-review.json")
                                .read_text(encoding="utf-8"))
        self.assertEqual(review_doc["candidates"][0]["decision"], "keep_as_is")

    def test_qualified_no_change_records_acceptance_basis(self):
        """零候选（诊断认为合格）：不改，留接受依据；不为交流强行修改。"""
        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([DIAGNOSIS_QUALIFIED]), adapter)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertEqual(len(adapter.prompts), 1)
        kinds = [e["kind"] for e in __import__("mentis.trace", fromlist=["load_events"])
                 .load_events(collab.run_dir)]
        summary = [e["summary"] for e in
                   __import__("mentis.trace", fromlist=["load_events"]).load_events(collab.run_dir)
                   if e["kind"] == "review.recorded" and "候选审查" in e["summary"]]
        self.assertTrue(summary and "候选 0 条" in summary[0])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("合格不改", report)

    def test_missing_screenshots_marked_unverified_not_faked(self):
        """截图失败：诊断明确不声称看过画面；报告如实标注视觉维度未验证。"""
        def failing_capture(index, out_dir):
            raise RuntimeError("注入的截图故障")

        gateway = FakeGateway([DIAGNOSIS_QUALIFIED])
        adapter = ReviewAdapter()
        collab = self.make(gateway, adapter, browser_capture=failing_capture)
        self.assertEqual(collab.run(), "DELIVERED")
        # V1 起 calls[0] 是评估计划实例化调用；诊断调用按其独有的无截图声明定位
        diagnosis_calls = [c for c in gateway.calls
                           if "本次诊断未收到任何截图" in c["prompt"]]
        self.assertTrue(diagnosis_calls, "诊断调用应在 prompt 中声明未收到截图")
        self.assertTrue(collab.last_professional_review["unverified_visual"])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("无截图输入，视觉维度未验证", report)

    def test_untrusted_measurement_yields_unverified_not_pass(self):
        """测量不可信：修订后复检判 UNVERIFIED——不自动判通过、不交付。"""
        adapter = ReviewAdapter(revision_page=GOOD_PAGE + V2_MARKER)
        collab = self.make(FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y]), adapter,
                           measure=broken_measure)
        self.assertEqual(collab.run(), "UNVERIFIED")
        self.assertFalse(collab.session.attempts[-1].accepted)  # 不接受
        verify_doc = json.loads((collab.run_dir / "professional-verify.json")
                                .read_text(encoding="utf-8"))
        self.assertTrue(verify_doc["measurement_unknown"])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("UNVERIFIED", report)

    def test_real_abnormal_browser_exit_yields_unverified(self):
        """审计场景真进程复现（2026-10-03）：受限 shell 中浏览器异常退出 →
        修订后复检观察不可信 → UNVERIFIED。这是"观察不可信则不验收"按设计
        生效，不是误判；修复方向是让浏览器能跑（环境），不是放宽判定。"""
        def real_failing_measure(page, out_dir):
            # python.exe 充当必然异常退出的"浏览器"（真实子进程，零 Mock）
            return measure_page(page, out_dir, browser=sys.executable, timeout_s=15)

        adapter = ReviewAdapter(revision_page=GOOD_PAGE + V2_MARKER)
        collab = self.make(
            FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y]), adapter,
            measure=real_failing_measure)
        self.assertEqual(collab.run(), "UNVERIFIED")
        self.assertFalse(collab.session.attempts[-1].accepted)
        verify_doc = json.loads((collab.run_dir / "professional-verify.json")
                                .read_text(encoding="utf-8"))
        self.assertTrue(verify_doc["measurement_unknown"])
        viewport_errors = [e for vp in verify_doc["measurement_v2"].values()
                           for e in vp["errors"]]
        self.assertTrue(any("退出码" in e for e in viewport_errors),
                        "真进程异常退出应逐视口记账错误")

    def test_revision_regression_not_delivered_snapshot_kept(self):
        """修订造成保留项回归：不交付、不接受；修订前快照保留。"""
        adapter = ReviewAdapter(revision_page=BAD_PAGE)  # 缺价格 → 保留项回归
        collab = self.make(FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y]), adapter)
        self.assertEqual(collab.run(), "NOT_DELIVERED")
        self.assertFalse(collab.session.attempts[-1].accepted)
        verify_doc = json.loads((collab.run_dir / "professional-verify.json")
                                .read_text(encoding="utf-8"))
        self.assertTrue(any("价格" in r for r in verify_doc["preserved_regression"]))
        self.assertTrue((collab.run_dir / "versions" /
                         f"index-{collab.session.attempts[0].attempt_id}.html").is_file())

    def test_budget_exhausted_archives_candidates_without_revision(self):
        """预算耗尽：成立问题留档不执行，不冒充已解决；初稿按结构+评审记录处理。"""
        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([DIAGNOSIS_GENERIC]), adapter, max_repairs=0)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertEqual(len(adapter.prompts), 1)  # 未委托修订
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("预算已用尽" if "预算已用尽" in report else "预算耗尽", report)
        self.assertIn("自主修订 1", report)  # 成立问题被识别但未执行

    def test_revision_unknown_stops_without_retry_or_accept(self):
        """修订执行 unknown：不判通过、不自动重发、不接受；需人工核对。"""
        adapter = ReviewAdapter(revision_status=AttemptStatus.UNKNOWN)
        collab = self.make(FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y]), adapter)
        self.assertEqual(collab.run(), "UNKNOWN")
        self.assertEqual(len(adapter.prompts), 2)  # 只委托一次，不自动重发
        self.assertFalse(collab.session.attempts[-1].accepted)

    def test_reserved_decision_request_pauses_revision(self):
        """诊断请求触及保留决定：修订暂停等待用户（沉默≠批准），不越权修订。"""
        adapter = ReviewAdapter()
        collab = self.make(FakeGateway([DIAGNOSIS_NEEDS_USER]), adapter)
        self.assertEqual(collab.run(), "DELIVERED")  # 初稿照常可查看
        self.assertEqual(len(adapter.prompts), 1)    # 修订未发生
        pending = [ix for ix in collab.session.interactions
                   if ix.kind.value == "request_decision" and ix.answer_source is None]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].blocking_action, "review-fix")
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("修订暂停", report)


if __name__ == "__main__":
    unittest.main()
