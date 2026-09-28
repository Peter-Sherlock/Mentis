"""P3.1 最小 Codex CLI Adapter（同步、单次 turn、JSONL 事件）。

只依赖官方 ``codex exec`` / ``codex exec resume``；不操作桌面聊天窗口。
写入任务要求独立 Git 工作区并持有单写入锁。超时后状态为 unknown，
锁保留待人工核对执行器与产物，不自动重发。
"""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from mentis.contracts import AttemptStatus

_SANDBOXES = {"read-only", "workspace-write"}
_SECRET_NAME = re.compile(r"(?:API_KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)
DEFAULT_LOCK_DIR = Path(__file__).resolve().parents[2] / "runtime" / "executor_locks"


class ExecutorBusy(RuntimeError):
    """工作区已有未完成/未知的写入者，禁止重复委托。"""


@dataclass(frozen=True)
class CodexTaskSpec:
    workspace: Path
    prompt: str
    brief_version: int
    sandbox: str = "read-only"
    timeout_s: int = 120
    model: str = ""  # 留空时使用 CLI 当前默认模型

    def validate(self) -> Path:
        root = Path(self.workspace).resolve()
        if not root.is_dir() or not (root / ".git").exists():
            raise ValueError("Codex 工作区必须是存在的 Git 仓库根目录")
        if not self.prompt.strip() or self.brief_version < 1:
            raise ValueError("任务必须有非空目标和正整数 brief 版本")
        if self.sandbox not in _SANDBOXES:
            raise ValueError("仅支持 read-only 或 workspace-write")
        if not 1 <= self.timeout_s <= 600:
            raise ValueError("单次 turn 超时须在 1—600 秒内")
        return root


@dataclass
class ExecutorEvent:
    sequence: int
    kind: str
    item_type: str = ""
    item_status: str = ""


@dataclass
class CodexTaskResult:
    task_id: str
    attempt_id: str
    brief_version: int
    workspace: str
    status: AttemptStatus
    native_thread_id: str = ""
    events: list[ExecutorEvent] = field(default_factory=list)
    final_response: str = ""
    usage: dict = field(default_factory=dict)
    baseline_paths: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    error: str = ""
    cli_version: str = ""
    created_at: str = ""


class CodexCliAdapter:
    def __init__(self, executable: str = "codex", lock_dir: Path = DEFAULT_LOCK_DIR,
                 runner=None, cli_version: str = ""):
        self.executable = executable
        self.lock_dir = Path(lock_dir)
        self.runner = runner or subprocess.run
        if cli_version:
            self.cli_version = cli_version
        else:
            try:
                version = subprocess.run([executable, "--version"], capture_output=True,
                                         text=True, encoding="utf-8", errors="replace", timeout=10)
                self.cli_version = version.stdout.strip() if version.returncode == 0 else "unknown"
            except (OSError, subprocess.TimeoutExpired):
                self.cli_version = "unknown"

    def describe_capabilities(self) -> dict:
        """声明这个适配器实际暴露的能力；本机验证记录见 P3.1。"""
        return {
            "events": "native_jsonl",
            "same_session_continue": "native_resume",
            "artifact_listing": "emulated_git_status",
            "synchronous_status": "native_turn_terminal_event",
            "streaming_to_caller": "unsupported",
            "mid_turn_input": "unsupported",
            "approval_roundtrip": "unsupported",
            "cancel_confirmation": "unknown",
            "timeout_recovery": "unknown_requires_reconciliation",
            "cost_hard_cap": "unsupported",
            "filesystem_permission": "native_codex_sandbox_workspace_scope",
        }

    def start_task(self, spec: CodexTaskSpec) -> CodexTaskResult:
        root = spec.validate()
        task_id = f"task-{uuid4().hex[:16]}"
        command = [self.executable, "exec", "--json", "--sandbox", spec.sandbox,
                   "-C", str(root)]
        if spec.model:
            command += ["--model", spec.model]
        command.append(spec.prompt)
        return self._invoke(command, spec, root, task_id)

    def continue_task(self, native_thread_id: str, task_id: str,
                      spec: CodexTaskSpec) -> CodexTaskResult:
        root = spec.validate()
        try:
            UUID(native_thread_id)
        except ValueError as exc:
            raise ValueError("Codex native_thread_id 不是 UUID") from exc
        command = [self.executable, "exec", "resume", "--json", "-c",
                   f'sandbox_mode="{spec.sandbox}"']
        if spec.model:
            command += ["--model", spec.model]
        command += [native_thread_id, spec.prompt]
        return self._invoke(command, spec, root, task_id, expected_thread_id=native_thread_id)

    def _invoke(self, command: list[str], spec: CodexTaskSpec, root: Path,
                task_id: str, expected_thread_id: str = "") -> CodexTaskResult:
        attempt_id = f"attempt-{uuid4().hex[:16]}"
        result = CodexTaskResult(
            task_id=task_id, attempt_id=attempt_id, brief_version=spec.brief_version,
            workspace=str(root), status=AttemptStatus.UNKNOWN,
            cli_version=self.cli_version, created_at=datetime.now().isoformat(timespec="seconds"),
        )
        result.baseline_paths = self.collect_artifacts(root)
        lock_path = self._acquire_lock(root, task_id, attempt_id) if spec.sandbox == "workspace-write" else None
        timed_out = False
        try:
            # 不能把 Mentis 的模型凭据直接继承给执行器；当前适配器使用 Codex 本机登录。
            child_env = {key: value for key, value in os.environ.items() if not _SECRET_NAME.search(key)}
            try:
                completed = self.runner(
                    command, cwd=root, env=child_env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=spec.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                # 超时：状态 unknown、锁保留；但已捕获的事件要保留用于核对与续接
                timed_out = True
                result.error = "Codex turn 超时；执行器是否仍在写入未知，禁止自动重试"
                partial = exc.stdout or ""
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", errors="replace")
                if partial:
                    self._parse_events(partial, result)
                return result
            except OSError as exc:
                result.status = AttemptStatus.FAILED
                result.error = f"Codex 进程启动失败: {type(exc).__name__}"
                return result
            self._parse_events(completed.stdout or "", result)
            try:
                result.artifact_paths = self.collect_artifacts(root)
            except RuntimeError as exc:
                result.status = AttemptStatus.UNKNOWN
                result.error = str(exc)
                return result
            if expected_thread_id and result.native_thread_id != expected_thread_id:
                result.status = AttemptStatus.UNKNOWN
                result.error = "续接返回的 thread ID 不一致；需核对实际会话"
            elif spec.sandbox == "read-only" and result.artifact_paths != result.baseline_paths:
                result.status = AttemptStatus.UNKNOWN
                result.error = "只读任务期间 Git 文件状态变化；需核对是否有外部写入"
            elif completed.returncode == 0 and result.native_thread_id and any(
                e.kind == "turn.completed" for e in result.events
            ) and not any(
                e.kind in ("turn.failed", "error", "invalid_jsonl") or e.item_status == "failed"
                for e in result.events
            ):
                result.status = AttemptStatus.COMPLETED
            elif (completed.returncode == 0 and result.native_thread_id
                  and result.final_response and not any(
                      e.kind in ("turn.failed", "error", "invalid_jsonl")
                      for e in result.events)):
                # 干净退出、有最终文本，但缺少 turn.completed 终态事件：
                # 结局不确定而非明确失败 → unknown，交由产物核对决定
                result.status = AttemptStatus.UNKNOWN
                result.error = "Codex 退出码 0 且有最终文本，但缺少 turn.completed；需产物核对"
            else:
                result.status = AttemptStatus.FAILED
                result.error = f"Codex 未报告成功终态（进程退出码 {completed.returncode}）"
            return result
        finally:
            # timeout 的子进程/文件状态可能尚未可判定，故意保留锁。
            if lock_path is not None and not timed_out:
                lock_path.unlink(missing_ok=True)

    def _acquire_lock(self, root: Path, task_id: str, attempt_id: str) -> Path:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:20]
        path = self.lock_dir / f"{digest}.lock"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ExecutorBusy(f"工作区已有写入锁：{path}；先核对上次任务状态") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"task_id": task_id, "attempt_id": attempt_id, "workspace": str(root),
                       "created_at": datetime.now().isoformat(timespec="seconds")}, out)
        return path

    @staticmethod
    def _parse_events(raw: str, result: CodexTaskResult) -> None:
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                result.events.append(ExecutorEvent(number, "invalid_jsonl"))
                continue
            kind = event.get("type", "")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            result.events.append(ExecutorEvent(number, str(kind), str(item.get("type", "")),
                                               str(item.get("status", ""))))
            if kind == "thread.started":
                result.native_thread_id = str(event.get("thread_id", ""))
            elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
                result.usage = {key: value for key, value in event["usage"].items()
                                if type(value) is int and value >= 0}
            elif kind == "item.completed" and item.get("type") == "agent_message":
                result.final_response = str(item.get("text", ""))

    @staticmethod
    def collect_artifacts(root: Path) -> list[str]:
        """列出当前 Git 脏文件；调用者需比较委托前后基线，不能仅凭列表归因。"""
        try:
            proc = subprocess.run(
                ["git", "-c", "core.excludesfile=", "status", "--porcelain=v1", "-z",
                 "--untracked-files=all"], cwd=root, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法采集 Git 产物状态：{type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"无法采集 Git 产物状态：退出码 {proc.returncode}")
        chunks = proc.stdout.split(b"\0")
        paths = []
        i = 0
        while i < len(chunks):
            chunk = chunks[i]
            i += 1
            if len(chunk) < 4:
                continue
            status = chunk[:2]
            paths.append(chunk[3:].decode("utf-8", errors="replace"))
            if b"R" in status or b"C" in status:
                i += 1  # porcelain -z 的 rename/copy 有额外旧路径
        return sorted(set(paths))
