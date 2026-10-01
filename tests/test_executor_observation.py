"""P3.1 关键边界：事件终态、单写入锁、脱敏环境与双视口捕获。"""

import hashlib
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mentis.browser_observation import capture_local_html
from mentis.contracts import AttemptStatus
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec, ExecutorBusy

THREAD = "01a0e752-6690-7333-a317-7acf38facc8e"
SUCCESS_EVENTS = (
    '{"type":"thread.started","thread_id":"' + THREAD + '"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"完成"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
)


class CodexCliAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        (self.root / "brief.txt").write_text("合成任务", encoding="utf-8")
        self.lock_dir = self.root / "locks"

    def test_real_event_shape_and_secret_not_inherited(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertNotIn("DEEPSEEK_API_KEY", kwargs["env"])
            (self.root / "review-note.md").write_text("问题与下一步", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, SUCCESS_EVENTS, "")

        adapter = CodexCliAdapter(lock_dir=self.lock_dir, runner=fake_run, cli_version="codex test")
        spec = CodexTaskSpec(self.root, "写一条评审", brief_version=2, sandbox="workspace-write")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "never-send-to-executor"}):
            result = adapter.start_task(spec)
        self.assertEqual(result.status, AttemptStatus.COMPLETED)
        self.assertEqual(result.native_thread_id, THREAD)
        self.assertIn("review-note.md", result.artifact_paths)
        self.assertNotIn("review-note.md", result.baseline_paths)
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertEqual(result.cli_version, "codex test")
        self.assertEqual(calls[0][0][:5], ["codex", "exec", "--json", "--sandbox", "workspace-write"])
        # prompt 经 stdin（"-" 约定）传递：npm .cmd shim 会截断含引号/多行的 argv prompt
        self.assertEqual(calls[0][0][-1], "-")
        self.assertEqual(calls[0][1].get("input"), "写一条评审")
        self.assertFalse(list(self.lock_dir.glob("*.lock")))

    def test_timeout_is_unknown_and_keeps_single_writer_lock(self):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        adapter = CodexCliAdapter(lock_dir=self.lock_dir, runner=timeout, cli_version="codex test")
        spec = CodexTaskSpec(self.root, "写一条评审", brief_version=1, sandbox="workspace-write")
        result = adapter.start_task(spec)
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        self.assertEqual(len(list(self.lock_dir.glob("*.lock"))), 1)
        with self.assertRaises(ExecutorBusy):
            adapter.start_task(spec)

    def test_resume_preserves_native_identity(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, SUCCESS_EVENTS, "")

        adapter = CodexCliAdapter(lock_dir=self.lock_dir, runner=fake_run, cli_version="codex test")
        spec = CodexTaskSpec(self.root, "继续评审", brief_version=1)
        result = adapter.continue_task(THREAD, "task-1", spec)
        self.assertEqual(result.status, AttemptStatus.COMPLETED)
        self.assertEqual(result.task_id, "task-1")
        self.assertEqual(commands[0][:3], ["codex", "exec", "resume"])
        self.assertIn(THREAD, commands[0])

    def test_invalid_jsonl_does_not_count_as_success(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "not json\n" + SUCCESS_EVENTS, "")

        adapter = CodexCliAdapter(lock_dir=self.lock_dir, runner=fake_run, cli_version="codex test")
        result = adapter.start_task(CodexTaskSpec(self.root, "只读", brief_version=1))
        self.assertEqual(result.status, AttemptStatus.FAILED)

    def test_refusal_or_failed_turn_is_not_completion(self):
        failed = ('{"type":"thread.started","thread_id":"' + THREAD + '"}\n'
                  '{"type":"turn.started"}\n'
                  '{"type":"error","message":"permission denied"}\n'
                  '{"type":"turn.failed"}\n')

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, failed, "")

        adapter = CodexCliAdapter(lock_dir=self.lock_dir, runner=fake_run, cli_version="codex test")
        result = adapter.start_task(CodexTaskSpec(self.root, "只读", brief_version=1))
        self.assertEqual(result.status, AttemptStatus.FAILED)
        self.assertIn("turn.failed", [event.kind for event in result.events])


FAKE_BODY = r'''
import hashlib, json, os, sys, time
mode = os.environ.get("FAKE_MODE", "normal")
if os.environ.get("FAKE_PID_FILE"):
    open(os.environ["FAKE_PID_FILE"], "w").write(str(os.getpid()))
thread = "01a0e752-6690-7333-a317-7acf38facc8e"
if mode == "normal":
    data = sys.stdin.buffer.read()
    text = ("SHA:" + hashlib.sha256(data).hexdigest()
            + ":LEN:" + str(len(data)) + ":ARGV:" + json.dumps(sys.argv[1:], ensure_ascii=False))
    print(json.dumps({"type": "thread.started", "thread_id": thread}, ensure_ascii=False))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": text}}, ensure_ascii=False))
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 7, "output_tokens": 3}}))
elif mode == "noread":
    time.sleep(120)  # 从不读 stdin：发送端阻塞到共享截止时间
elif mode == "hang":
    print(json.dumps({"type": "thread.started", "thread_id": thread}))
    sys.stdout.flush()
    time.sleep(120)  # 发事件后挂起：验证超时树杀
elif mode == "earlyexit":
    sys.stdin.buffer.read(10)  # 只读 10 字节即退出：发送端 BrokenPipe
    print(json.dumps({"type": "thread.started", "thread_id": thread}))
    sys.stdout.flush()
'''


class FakeCodexExecutorTests(unittest.TestCase):
    """无害本地子进程（py 脚本 + .cmd 包装）验证 stdin 传输与超时树杀行为。

    覆盖审查要求：复杂 UTF-8 原文完整接收、初始与恢复调用、发送阻塞不无限等待、
    BrokenPipe 不判 completed、超时杀整棵进程树（.cmd 包装 + 子进程）。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        body = self.root / "fake_body.py"
        body.write_text(FAKE_BODY, encoding="utf-8")
        self.shim = self.root / "fake-codex.cmd"
        self.shim.write_text('@echo off\r\npy -3.14 -u "%~dp0fake_body.py" %*\r\n',
                             encoding="utf-8")
        self.lock_dir = self.root / "locks"
        self.pid_file = self.root / "body.pid"
        self.complex_prompt = ('行1 带双引号 "与\\反斜杠" 和单引号 \'x\'\n'
                              '日本語テスト、中文与 £ ¥ 🙂 emoji\n'
                              + "tail-marker-" + "x" * 500)

    def _adapter(self):
        return CodexCliAdapter(executable=str(self.shim), lock_dir=self.lock_dir,
                               cli_version="fake-codex")

    def _wait_pid_gone(self, pid: int, timeout_s: float = 8.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            check = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
            if str(pid) not in (check.stdout or ""):
                return True
            time.sleep(0.3)
        return False

    def test_stdin_utf8_integrity_for_start_and_resume(self):
        adapter = self._adapter()
        spec = CodexTaskSpec(self.root, self.complex_prompt, brief_version=2,
                             sandbox="read-only", timeout_s=60)
        result = adapter.start_task(spec)
        self.assertEqual(result.status, AttemptStatus.COMPLETED, result.error)
        self.assertEqual(result.stdin_status, "sent")
        expected = hashlib.sha256(self.complex_prompt.encode("utf-8")).hexdigest()
        self.assertIn(f"SHA:{expected}:LEN:{len(self.complex_prompt.encode('utf-8'))}",
                      result.final_response)
        # 恢复调用：argv 含 thread id 与 "-"，stdin 同样完整送达
        resume = adapter.continue_task(THREAD, "task-resume", spec)
        self.assertEqual(resume.status, AttemptStatus.COMPLETED, resume.error)
        self.assertIn(f"SHA:{expected}:LEN:{len(self.complex_prompt.encode('utf-8'))}",
                      resume.final_response)
        # 恢复调用：argv 以 [.., thread_id, "-"] 结尾，stdin 同样完整送达
        self.assertIn(THREAD, resume.final_response)
        self.assertTrue(resume.final_response.rstrip().endswith('"-"]'),
                        resume.final_response[-80:])

    def test_unread_stdin_hits_shared_deadline_and_kills_tree(self):
        # 200KB prompt 超过管道缓冲：子进程不读 stdin 时发送真实阻塞，由截止时间解除
        big_prompt = "HEAD|" + "P" * 200_000 + "|TAIL"
        spec = CodexTaskSpec(self.root, big_prompt, brief_version=1,
                             sandbox="workspace-write", timeout_s=3)
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_MODE": "noread",
                                     "FAKE_PID_FILE": str(self.pid_file)}):
            result = self._adapter().start_task(spec)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15, "stdin 阻塞必须在共享截止时间内解除，不得无限等待")
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        self.assertEqual(result.stdin_status, "incomplete")
        self.assertIn("未确认完整送达", result.error)
        self.assertEqual(len(list(self.lock_dir.glob("*.lock"))), 1, "unknown 必须保锁")
        (self.lock_dir / list(self.lock_dir.glob("*.lock"))[0].name).unlink()
        body_pid = int(self.pid_file.read_text())
        self.assertTrue(self._wait_pid_gone(body_pid), "超时必须杀掉 .cmd 包装下的子进程树")

    def test_broken_pipe_is_never_completed(self):
        spec = CodexTaskSpec(self.root, "B" * 200_000, brief_version=1,
                             sandbox="read-only", timeout_s=20)
        with patch.dict(os.environ, {"FAKE_MODE": "earlyexit",
                                     "FAKE_PID_FILE": str(self.pid_file)}):
            result = self._adapter().start_task(spec)
        self.assertEqual(result.stdin_status, "broken_pipe")
        self.assertEqual(result.status, AttemptStatus.UNKNOWN,
                         "指令未完整送达时不得判 completed")
        self.assertIn("未确认完整送达", result.error)

    def test_hanging_executor_killed_on_timeout(self):
        spec = CodexTaskSpec(self.root, "发完事件后挂起", brief_version=1,
                             sandbox="read-only", timeout_s=3)
        with patch.dict(os.environ, {"FAKE_MODE": "hang",
                                     "FAKE_PID_FILE": str(self.pid_file)}):
            result = self._adapter().start_task(spec)
        self.assertEqual(result.status, AttemptStatus.UNKNOWN)
        body_pid = int(self.pid_file.read_text())
        self.assertTrue(self._wait_pid_gone(body_pid), "挂起的子进程必须被树杀")


class BrowserCaptureTests(unittest.TestCase):
    def test_records_two_viewports_and_static_limitations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "page.html"
            page.write_text("<html><body><h1>活动</h1></body></html>", encoding="utf-8")
            browser = root / "chrome.exe"
            browser.write_bytes(b"fake")

            def fake_run(command, **kwargs):
                if "--version" in command:
                    return subprocess.CompletedProcess(command, 0, "Chrome test", "")
                shot = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--screenshot=")))
                width, height = map(int, next(arg.split("=", 1)[1] for arg in command
                                              if arg.startswith("--window-size=")).split(","))
                shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
                                 + struct.pack(">II", width, height))
                return subprocess.CompletedProcess(command, 0, "", "")

            record = capture_local_html(page, root / "out", browser=browser, runner=fake_run)
            self.assertEqual([(v.name, v.actual_width) for v in record.views],
                             [("desktop", 1280), ("mobile", 390)])
            self.assertTrue(all(v.screenshot_path and not v.error for v in record.views))
            self.assertTrue(any("未执行点击" in lim for lim in record.limitations))


if __name__ == "__main__":
    unittest.main()
