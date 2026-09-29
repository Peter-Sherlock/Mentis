"""P2.4-T2 回归例：Codex 逐行流、延迟事件可见性与实时旁观（全部离线，注入式流工厂）。

验收映射（计划 §4/T2 离线部分）：
- 延迟事件夹具证明事件在 turn 完成前已可被回调/落盘可见；
- 超时 → unknown + 锁保留 + 已到达事件保留；损坏行与 turn.failed 不算成功；
- 旧注入 runner 兼容（结束后回调仍触发）；
- 旁观器只读：停止旁观不影响运行继续写事件。
真实 Codex 流验证不在本文件（见 progress/P2.4-T2.md 的受限真实运行记录）。
"""

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from mentis.contracts import AttemptStatus
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec
from mentis.trace import TraceWriter, follow_timeline, load_events


def _git_root(base: Path) -> Path:
    root = base / "ws"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    (root / "brief.txt").write_text("合成任务", encoding="utf-8")
    return root


class ScriptedStream:
    """脚本化流：逐行 yield，可在任意位置停（timed_out）；finish 返回注入的 rc/stderr。"""

    def __init__(self, lines, returncode=0, stderr="", stop_after=None):
        self._lines = list(lines)
        self._rc = returncode
        self._stderr = stderr
        self._stop_after = stop_after  # 第 N 行 yield 后置 timed_out
        self.timed_out = False
        self.yielded = 0

    def lines(self):
        for i, line in enumerate(self._lines, start=1):
            yield line
            self.yielded = i
            if self._stop_after is not None and i >= self._stop_after:
                self.timed_out = True
                return

    def finish(self, grace_s: int = 15):
        return self._rc, self._stderr


class StreamingAdapterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = _git_root(self.base)
        self.lock_dir = self.base / "locks"

    def _spec(self, **kw):
        defaults = dict(workspace=self.root, prompt="只读核对", brief_version=1,
                        sandbox="read-only", timeout_s=30)
        defaults.update(kw)
        return CodexTaskSpec(**defaults)

    def test_delayed_event_visible_before_turn_completes(self):
        """核心验收：turn.completed 尚未被消费时，首事件已经过回调（可落盘被 --follow 看到）。"""
        tid = str(uuid4())
        events = [
            json.dumps({"type": "thread.started", "thread_id": tid}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "正在处理"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7}}),
        ]
        trace_writer = TraceWriter(self.base / "run", "run-stream")
        received = []

        def on_event(event, meta):
            trace_writer.emit("executor",
                              "executor.started" if meta.get("first") else "executor.item",
                              json.dumps(event, ensure_ascii=False)[:120], status="pending")
            received.append(event)

        stream = ScriptedStream(events)
        state = {"checkpoint_ran": False, "trace_kinds_at_checkpoint": []}

        def after_first():
            # 第一条事件刚 yield：trace 已可见，而 turn.completed 尚未产生
            state["checkpoint_ran"] = True
            state["trace_kinds_at_checkpoint"] = [e["kind"] for e in load_events(
                self.base / "run")]

        stream.after_each = after_first if False else None  # 占位；真正挂接在 yield 后
        # 逐行挂接：包装 lines 生成器，在第 1 行后检查
        original_lines = stream.lines

        def lines_with_checkpoint():
            gen = original_lines()
            yield next(gen)
            state["checkpoint_ran"] = True
            state["trace_kinds_at_checkpoint"] = [e["kind"] for e in load_events(
                self.base / "run")]
            for rest in gen:
                yield rest
        stream.lines = lines_with_checkpoint

        adapter = CodexCliAdapter(executable="codex", lock_dir=self.lock_dir,
                                  cli_version="codex test",
                                  stream_factory=lambda *a: stream)
        result = adapter.start_task(self._spec(on_event=on_event))
        self.assertEqual(result.status, AttemptStatus.COMPLETED)
        self.assertTrue(state["checkpoint_ran"])
        # 关键断言：checkpoint 时 trace 已有首事件，而完整 turn 未结束
        self.assertIn("executor.started", state["trace_kinds_at_checkpoint"])
        self.assertNotIn("turn.completed", state["trace_kinds_at_checkpoint"])
        # 全部事件经回调到达；首事件延迟已记录
        self.assertEqual(len(received), 4)
        self.assertGreaterEqual(result.first_event_latency_s, 0.0)
        self.assertEqual(result.usage.get("input_tokens"), 7)
        timeline = trace_writer.finalize()
        self.assertTrue(timeline["integrity"]["events_complete"])

    def test_timeout_yields_unknown_and_keeps_lock(self):
        """超时：unknown + 已到达事件保留 + 锁保留（人工核对路径）。"""
        tid = str(uuid4())
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             json.dumps({"type": "turn.started"})],
            returncode=-1, stderr="partial stderr", stop_after=2)
        adapter = CodexCliAdapter(executable="codex", lock_dir=self.lock_dir,
                                  cli_version="codex test",
                                  stream_factory=lambda *a: stream)
        result = adapter.start_task(self._spec(sandbox="workspace-write"))
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        self.assertIn("超时", result.error)
        self.assertEqual(result.native_thread_id, tid)  # 已到达事件保留
        self.assertIn("partial stderr", result.stderr_tail)
        digest = __import__("hashlib").sha256(str(self.root.resolve()).casefold().encode()).hexdigest()[:20]
        self.assertTrue((self.lock_dir / f"{digest}.lock").is_file())  # 锁保留
        (self.lock_dir / f"{digest}.lock").unlink()  # 测试自清理

    def test_invalid_jsonl_is_not_success(self):
        tid = str(uuid4())
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             "not json",
             json.dumps({"type": "turn.completed", "usage": {}})],
            returncode=1)
        adapter = CodexCliAdapter(executable="codex", lock_dir=self.lock_dir,
                                  cli_version="codex test",
                                  stream_factory=lambda *a: stream)
        result = adapter.start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.FAILED)

    def test_turn_failed_is_not_success(self):
        tid = str(uuid4())
        stream = ScriptedStream(
            [json.dumps({"type": "thread.started", "thread_id": tid}),
             json.dumps({"type": "turn.started"}),
             json.dumps({"type": "error", "message": "quota"}),
             json.dumps({"type": "turn.failed"})],
            returncode=1)
        adapter = CodexCliAdapter(executable="codex", lock_dir=self.lock_dir,
                                  cli_version="codex test",
                                  stream_factory=lambda *a: stream)
        result = adapter.start_task(self._spec())
        self.assertEqual(result.status, AttemptStatus.FAILED)
        self.assertIn("退出码 1", result.error)

    def test_legacy_runner_path_still_calls_on_event(self):
        """注入 runner 的兼容路径：结束后逐行回调仍触发（不因重构失效）。"""
        tid = str(uuid4())
        success = (f'{{"type":"thread.started","thread_id":"{tid}"}}\n'
                   '{"type":"turn.completed","usage":{"input_tokens":3}}\n')

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, success, "")

        received = []
        adapter = CodexCliAdapter(executable="codex", lock_dir=self.lock_dir,
                                  runner=fake_run, cli_version="codex test")
        result = adapter.start_task(self._spec(on_event=lambda e, m: received.append(e)))
        self.assertEqual(result.status, AttemptStatus.COMPLETED)
        self.assertEqual(len(received), 2)


class FollowTimelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run"

    def test_follow_replays_then_stops_after_manifest(self):
        w = TraceWriter(self.run_dir, "run-f")
        w.emit("mentis", "goal.recorded", "目标")
        out = []
        done = threading.Event()

        def runner():
            follow_timeline(self.run_dir, out=out.append, poll_s=0.02)
            done.set()

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        for _ in range(200):  # 等旁观者重放已有事件
            if any("goal.recorded" in line for line in out):
                break
            threading.Event().wait(0.02)
        w.emit("executor", "executor.item", "一条进展", status="pending")
        w.emit("mentis", "run.finalized", "收尾", status="ok")
        w.finalize(outcome="DELIVERED")
        self.assertTrue(done.wait(timeout=10))  # 运行收尾后旁观自动结束
        text = "\n".join(out)
        self.assertIn("[旁观中]", text)
        self.assertIn("goal.recorded", text)
        self.assertIn("一条进展", text)
        self.assertIn("[运行已收尾]", text)

    def test_reader_stop_does_not_affect_writer(self):
        """旁观器退出不影响执行：停止读取后写入者继续写且完整落盘。"""
        w = TraceWriter(self.run_dir, "run-f2")
        w.emit("mentis", "goal.recorded", "第一")
        out = []
        stop = threading.Event()
        seen_first = threading.Event()

        def stop_check():
            return seen_first.is_set()

        t = threading.Thread(target=follow_timeline,
                             args=(self.run_dir,), kwargs={"out": out.append, "poll_s": 0.02,
                                                           "stop_check": stop_check},
                             daemon=True)
        t.start()
        for _ in range(200):
            if any("第一" in line for line in out):
                break
            threading.Event().wait(0.02)
        seen_first.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        w.emit("mentis", "run.finalized", "第二")
        manifest = w.finalize(outcome="X")
        self.assertTrue(manifest["integrity"]["events_complete"])
        self.assertEqual(len(load_events(self.run_dir)), 2)  # 写入者未受旁观退出影响（第一/第二）

    def test_follow_on_dir_without_trace_shows_materials(self):
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "state.json").write_text("{}", encoding="utf-8")
        out = []
        follow_timeline(self.run_dir, out=out.append)
        self.assertTrue(any("历史" in line or "state.json" in line for line in out))


if __name__ == "__main__":
    unittest.main()
