"""P3.3 回归例：进程中断、恢复与晚到结果（明确 Fake 执行器，全部离线）。

开发计划 P3.3 退出条件中可离线固化的部分：委托发起即落盘（进程中断后在途
尝试对恢复的会话可见）、thread.started 凭据早记入追踪、running 且无执行器
结果的孤儿尝试经核对收养（不自动重发）、旧目标尝试的晚到确认被版本门禁拒绝。
真实进程击杀与真实执行器的同型验证见 runtime/observability/p33/。
"""

import tempfile
import unittest
from pathlib import Path

from mentis.contracts import AttemptStatus
from mentis.executors.codex_cli import CodexTaskResult

try:
    from tests.test_collab import GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from tests.test_guidance import file_aware_measure
except ImportError:  # 直接以文件方式运行时
    from test_collab import GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from test_guidance import file_aware_measure

from mentis.collab import collaboration_from_state, load_state

THREAD_ID = "11111111-2222-3333-4444-555555555555"


class ProcessDeath(BaseException):
    """模拟进程被杀：BaseException 绕过 except Exception 的异常收尾
    （真实 taskkill 不会给 run() 任何收尾机会）。"""


def _emit(adapter, spec, status, artifacts, thread=THREAD_ID, error=""):
    return CodexTaskResult(
        task_id="task-fake", attempt_id=f"attempt-{len(adapter.calls):04d}",
        brief_version=spec.brief_version, workspace=str(spec.workspace),
        status=status, native_thread_id=thread, usage={"input_tokens": 1},
        artifact_paths=artifacts, cli_version="fake", error=error,
    )


class CrashMidDelegationTests(unittest.TestCase):
    """进程在适配器调用期间被杀：checkpoint 落盘 + 孤儿收养 + 拒绝盲目确认。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _crashed_run(self):
        """运行到委托中途"被杀"：checkpoint 已落库，执行器（孤儿）随后写完产物。"""
        adapter = FakeAdapter()
        observed = {}

        def orphan_completes_then_mentis_dies(spec, status_override=None):
            # 适配器被调用 = checkpoint 应已把 running 尝试落库（P3.3 修复的前提）
            _, session = load_state(collab.run_dir / "state.json")
            observed["attempt"] = session.attempts[-1]
            # 执行器 thread.started 先到（凭据早记入追踪），孤儿随后完成产物写入
            if spec.on_event:
                spec.on_event({"type": "thread.started", "thread_id": THREAD_ID},
                              {"first": True, "first_event_latency_s": 0.1})
            Path(spec.workspace, "index.html").write_text(GOOD_PAGE, encoding="utf-8")
            raise ProcessDeath()  # Mentis 进程在此"死亡"；执行器结果永不返回

        adapter._run = orphan_completes_then_mentis_dies  # type: ignore[method-assign]
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure)
        with self.assertRaises(ProcessDeath):
            collab.run()
        return collab, adapter, observed

    def test_dispatch_checkpoints_running_attempt_before_executor_call(self):
        collab, adapter, observed = self._crashed_run()
        # checkpoint 在适配器调用前生效：尝试以 running 落库（不再只存在于追踪与写锁中）
        self.assertEqual(observed["attempt"].status, AttemptStatus.RUNNING)
        self.assertEqual(observed["attempt"].action_key, "build-page")
        self.assertEqual(collab.codex_runs, [])  # 无执行器结果记录（崩溃特征）
        # checkpoint 不发收尾事件（运行并未收尾）
        events = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("run.finalized", events)
        # 线程 ID 凭据已在追踪里（不等适配器返回），恢复指引据此续接
        self.assertIn(THREAD_ID, events)

    def test_orphan_attempt_adopted_via_reconciliation_without_redispatch(self):
        collab, adapter, _ = self._crashed_run()
        state = collab.run_dir / "state.json"

        def restored(use_model=True):
            r = collaboration_from_state(state, adapter=adapter,
                                         browser_capture=stub_capture)
            r.gateway = None
            r.use_model = False
            r.measure = file_aware_measure
            return r

        # 活跃写入者仍在 → 拒绝确认（不盲目确认，也不重发）
        r1 = restored()
        r1._codex_processes_since = lambda since: [{"ProcessId": 4321}]
        self.assertEqual(r1.resume(confirm_outcome=AttemptStatus.COMPLETED), "UNKNOWN")
        self.assertEqual(len(adapter.calls), 0)
        # 写入者退出：孤儿尝试（running 且无执行器结果）按 unknown 纳入核对 → 确认收养
        r2 = restored()
        r2._codex_processes_since = lambda since: []
        self.assertEqual(r2.resume(confirm_outcome=AttemptStatus.COMPLETED), "DELIVERED")
        self.assertEqual(len(adapter.calls), 0)  # 核对收养 ≠ 重新委托
        meta, session = load_state(state)
        last = session.attempts[-1]
        self.assertEqual(last.status, AttemptStatus.COMPLETED)
        self.assertTrue(last.accepted)
        self.assertEqual(meta["restored_from"], "sqlite")  # 库为规范态来源
        self.assertEqual(meta["task_db"]["state"], "restored")
        events = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("进程中断恢复", events)  # 收养依据留痕
        self.assertIn("executor.confirmed", events)  # 人工核对确认入流


class StaleGoalLateConfirmTests(unittest.TestCase):
    """改目标后，旧目标尝试的晚到确认：可核对入档，但接受被版本门禁拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_late_confirm_of_old_goal_attempt_is_not_accepted(self):
        adapter = FakeAdapter()
        seq = {"n": 0}

        def timeout_then_good(spec, status_override=None):
            seq["n"] += 1
            adapter.calls.append(spec.prompt[:30])
            if seq["n"] == 1:  # v1 构建超时 → unknown（锁保留语义由真实链路验证）
                return _emit(adapter, spec, AttemptStatus.UNKNOWN, [],
                             thread="", error="Codex turn 超时；需核对")
            Path(spec.workspace, "index.html").write_text(GOOD_PAGE, encoding="utf-8")
            return _emit(adapter, spec, AttemptStatus.COMPLETED, ["index.html"])

        adapter._run = timeout_then_good  # type: ignore[method-assign]
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure)
        self.assertEqual(collab.run(), "UNKNOWN")
        # 改目标 → 按新目标重建并交付（v2）
        # 审计 #1 语义：改目标沿用同主体，Fake 执行器写的咖啡页才能通过 T3/T4
        self.assertEqual(collab.resume(
            goal_new="改为「周六手冲入门工作坊」两天进阶营介绍页（新目标）"), "DELIVERED")
        self.assertEqual(len(adapter.calls), 2)
        # 晚到的确认：用户核对旧目标的 unknown 尝试——确认入档，
        # 但接受被 StaleVersion 拒绝（晚到结果不自动验收、不复活旧目标）
        collab._codex_processes_since = lambda since: []
        self.assertEqual(collab.resume(confirm_outcome=AttemptStatus.COMPLETED),
                         "NOT_DELIVERED")
        meta, session = load_state(collab.run_dir / "state.json")
        v1, v2 = session.attempts[0], session.attempts[1]
        self.assertEqual(v1.status, AttemptStatus.COMPLETED)  # 核对确认成立
        self.assertFalse(v1.accepted)  # 但旧目标尝试不接受
        self.assertTrue(v2.accepted)  # 新目标交付不受影响
        self.assertEqual(meta["status"], "NOT_DELIVERED")
        events = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("过期版本", events)  # StaleVersion 拒绝留痕

    def test_normal_delivery_emits_exactly_one_finalized_event(self):
        """checkpoint 不能给正常路径多发收尾事件（恰好一次）。"""
        collab = make_collab(adapter=FakeAdapter(), base_dir=self.base,
                             measure=file_aware_measure)
        self.assertEqual(collab.run(), "DELIVERED")
        events = (collab.run_dir / "trace" / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(events.count('"run.finalized"'), 1)


if __name__ == "__main__":
    unittest.main()
