"""P3.1 关键边界：事件终态、单写入锁、脱敏环境与双视口捕获。"""

import os
import struct
import subprocess
import tempfile
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
