"""P1.4 回归例：真实闭环的故障路径缺口补齐（明确 Fake 执行器，全部离线）。

对照开发计划 P1.4 点名场景与退出条件的覆盖审计（结果见
runtime/observability/p14/fault-matrix.md），本文件只补既有套件未覆盖的
四条故障路径；其余沿用既有回归并纳入同一张矩阵，不重复建、不另建平行实现。
"""

import json
import tempfile
import unittest
from pathlib import Path

from mentis.contracts import AttemptStatus, UnauthorizedAction
from mentis.executors.codex_cli import CodexTaskResult

try:
    from tests.test_collab import FakeAdapter, make_collab, stub_capture
    from tests.test_guidance import file_aware_measure
except ImportError:  # 直接以文件方式运行时
    from test_collab import FakeAdapter, make_collab, stub_capture
    from test_guidance import file_aware_measure

from mentis.collab import collaboration_from_state, load_state


def _install_phantom(adapter):
    """把假执行器换成"谎报 COMPLETED 但零产物"的行为（保持 thread/calls 记账一致）。"""

    def phantom(spec, status_override=None):
        adapter.calls.append(spec.prompt[:30])
        return CodexTaskResult(
            task_id="t", attempt_id=f"attempt-{len(adapter.calls):04d}",
            brief_version=spec.brief_version,
            workspace=str(spec.workspace), status=AttemptStatus.COMPLETED,
            native_thread_id=adapter.thread, usage={"input_tokens": 1},
            artifact_paths=[], cli_version="fake")

    adapter._run = phantom  # type: ignore[method-assign]


class PhantomArtifactTests(unittest.TestCase):
    """退出条件"假执行结果必须经审核"：执行器自报完成≠完成。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_executor_claims_completed_but_artifact_missing_not_delivered(self):
        adapter = FakeAdapter()
        _install_phantom(adapter)
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure, max_repairs=1)
        status = collab.run()
        # 自报 completed 的两次委托都没有产物：结构审核拦下，修复预算内仍无产物 → 不交付
        self.assertEqual(status, "NOT_DELIVERED")
        self.assertEqual(len(collab.session.attempts), 2)  # build + 1 修复（预算有界）
        self.assertFalse(collab.session.attempts[-1].accepted)
        self.assertEqual(collab.session.artifacts, [])  # 不给谎言记产物
        # 审核依据留痕：结构检查记录"产物缺失"
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("状态：**DELIVERED**", report)
        # 库与导出如实反映：无 accepted 尝试、预算已用 1
        meta, session = load_state(collab.run_dir / "state.json")
        self.assertEqual(meta["repairs_used"], 1)
        self.assertTrue(all(not a.accepted for a in session.attempts))

    def test_executor_final_text_claiming_done_is_not_evidence(self):
        """终态消息声称完成也不能替代产物审核（O3 真实运行同型：自述留给外部核）。"""
        adapter = FakeAdapter()
        _install_phantom(adapter)
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure, max_repairs=0)
        status = collab.run()
        self.assertEqual(status, "NOT_DELIVERED")
        self.assertEqual(len(adapter.calls), 1)  # 无修复预算 → 一次委托即止（有界停止）


class RefusalNotBypassableTests(unittest.TestCase):
    """退出条件"拒绝不绕过"：撤权后连续办路径也不能把委托塞过去。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _pending_run(self):
        adapter = FakeAdapter(behavior={"first": "questions", "second": "good"})
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure)
        self.assertEqual(collab.run(), "PENDING")
        return collab, adapter

    def test_revoked_run_resume_answers_cannot_dispatch(self):
        collab, adapter = self._pending_run()
        pending_ix = next(ix for ix in collab.session.interactions
                          if ix.kind.value == "request_decision" and not ix.answer_source)
        state = collab.run_dir / "state.json"
        # 撤回（授权全部 revoked，落库）
        r0 = collaboration_from_state(state, adapter=adapter,
                                      browser_capture=stub_capture)
        r0.gateway = None
        r0.use_model = False
        self.assertEqual(r0.resume(revoke=True), "WITHDRAWN")
        # 撤权后补答案续办：回答可以入档，但 apply-answers 委托必须被拒
        r1 = collaboration_from_state(state, adapter=adapter,
                                      browser_capture=stub_capture)
        r1.gateway = None
        r1.use_model = False
        calls_before = len(adapter.calls)
        with self.assertRaises(UnauthorizedAction):
            r1.resume(answers={pending_ix.interaction_id: "撤回后的回答"})
        self.assertEqual(len(adapter.calls), calls_before)  # 委托没有发生
        # 异常收尾落盘：状态 ERROR、恢复对象可再取（不静默当成继续执行）
        meta, _ = load_state(state)
        self.assertEqual(meta["status"], "ERROR")
        notes = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("UnauthorizedAction", notes)  # 拒绝原因留痕

    def test_no_answer_resume_keeps_pending_without_delegation(self):
        """沉默不视为许可（流级）：不带回答的续办=保持暂停、零委托。"""
        collab, adapter = self._pending_run()
        state = collab.run_dir / "state.json"
        r = collaboration_from_state(state, adapter=adapter,
                                     browser_capture=stub_capture)
        r.gateway = None
        r.use_model = False
        calls_before = len(adapter.calls)
        status = r.resume()  # 什么都没给：沉默
        self.assertEqual(status, "PENDING")
        self.assertEqual(len(adapter.calls), calls_before)
        still_pending = [ix for ix in r.session.interactions
                        if ix.kind.value == "request_decision" and not ix.answer_source]
        self.assertEqual(len(still_pending), 1)  # 未决保持未决


class RepairBudgetAcrossRestartTests(unittest.TestCase):
    """循环预算（×P1.3）：修复预算已用部分随任务库持久化，重启不重置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_repairs_used_survives_restart_and_bounds_further_looping(self):
        adapter = FakeAdapter(behavior={"first": "bad", "second": "bad"})
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure, max_repairs=1)
        self.assertEqual(collab.run(), "NOT_DELIVERED")
        self.assertEqual(collab.repairs_used, 1)
        state = collab.run_dir / "state.json"
        # 重启（库优先恢复）：预算账本原样回来，不是从 0 重来
        r = collaboration_from_state(state, adapter=adapter,
                                     browser_capture=stub_capture)
        r.gateway = None
        r.use_model = False
        self.assertEqual(r.repairs_used, 1)
        self.assertEqual(r.max_repairs, 1)
        # 台账：build + repair-1 两次尝试都在，无隐形重置
        self.assertEqual([a.action_key for a in r.session.attempts],
                         ["build-page", "repair-1"])
        meta, session = load_state(state)
        self.assertEqual(meta["repairs_used"], 1)
        self.assertEqual(len(session.attempts), 2)


if __name__ == "__main__":
    unittest.main()
