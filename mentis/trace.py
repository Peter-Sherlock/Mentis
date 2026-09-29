"""P2.4-T1 统一追踪事件写入与事后回放（契约：docs/TRACKING_EVENT_CONTRACT.md v1.0-draft）。

纪律：
- 只观察不回灌；写入者只有运行流程，回放器只读。
- 确切委托文本在实际调用执行器前落盘（archive_prompt），失败则不发起该次委托。
- 事件写入失败计入 manifest.integrity，不得称"完整追踪"。
- 历史运行没有 trace/ 时不回填：回放展示现存材料与 unrecorded 缺口。
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from mentis.contracts import AttemptStatus

CONTRACT_VERSION = "mentis-trace/1"
STATUS_MAP = {
    AttemptStatus.DISPATCHED: "pending",
    AttemptStatus.RUNNING: "pending",
    AttemptStatus.COMPLETED: "ok",
    AttemptStatus.FAILED: "failed",
    AttemptStatus.UNKNOWN: "unknown",
}
SUMMARY_LIMIT = 500


class TraceArchiveError(RuntimeError):
    """委托 prompt 落盘失败：调用方必须停止本次委托（契约 §1）。"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bound(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 8] + "…（已截断）"


class TraceWriter:
    """运行目录内 trace/ 的唯一写入者；续接时按已有事件续排 seq。"""

    def __init__(self, run_dir, run_id: str, executor_name: str = "codex-cli",
                 executor_version: str = "", mentis_version: str = ""):
        self.run_dir = Path(run_dir)
        self.trace_dir = self.run_dir / "trace"
        self.prompts_dir = self.trace_dir / "prompts"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.trace_dir / "events.jsonl"
        self.manifest_path = self.trace_dir / "manifest.json"
        self.run_id = run_id
        self.executor = {"name": executor_name, "cli_version": executor_version or "unknown"}
        self.mentis_version = mentis_version
        self.write_failures = 0
        self._seq = self._scan_max_seq()
        self._delegations = self._rebuild_delegations()

    # ---- 事件 ----

    def emit(self, source: str, kind: str, summary: str, *, attempt_id: str | None = None,
             brief_version: int | None = None, native_thread_id: str | None = None,
             status: str = "ok", refs: list | None = None) -> int:
        self._seq += 1
        event = {
            "schema_version": CONTRACT_VERSION,
            "run_id": self.run_id,
            "seq": self._seq,
            "time_utc": _now_utc(),
            "source": source,
            "kind": kind,
            "attempt_id": attempt_id,
            "brief_version": brief_version,
            "native_thread_id": native_thread_id,
            "status": status,
            "summary": _bound(summary),
            "refs": refs or [],
        }
        try:
            with self.events_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            # 契约 §1：写失败不得静默；计入完整性，seq 不复用（可能留下半行，读端容忍）
            self.write_failures += 1
        return self._seq

    def archive_prompt(self, attempt_id: str, prompt_text: str) -> dict:
        """实际调用执行器前落盘确切委托文本；失败抛 TraceArchiveError（调用方停止委托）。

        以字节写入（不经 newline 转换），保证文件哈希与实际发送文本逐字节一致。
        """
        prompt_path = self.prompts_dir / f"{attempt_id}.md"
        try:
            prompt_path.write_bytes(prompt_text.encode("utf-8"))
            sha = _sha256_file(prompt_path)
        except OSError as exc:
            raise TraceArchiveError(
                f"prompt 落盘失败（{type(exc).__name__}）：本次委托不发起") from exc
        return {"path": str(prompt_path), "sha256": sha, "role": "prompt"}

    # ---- 委托台账（manifest 用，从事件重建以兼容续接） ----

    def record_delegation(self, attempt_id: str, brief_version: int,
                          prompt_ref: dict, action: str = ""):
        self._delegations.append({
            "attempt_id": attempt_id, "brief_version": brief_version,
            "prompt_file": prompt_ref["path"], "prompt_sha256": prompt_ref["sha256"],
            "action": action, "native_thread_id": "", "terminal_status": "",
            "usage": {},
        })

    def record_executor_result(self, attempt_id: str, status, native_thread_id: str = "",
                               usage: dict | None = None, error: str = ""):
        status_value = status.value if hasattr(status, "value") else str(status)
        for d in reversed(self._delegations):
            if d["attempt_id"] == attempt_id:
                d["native_thread_id"] = native_thread_id or d["native_thread_id"]
                d["terminal_status"] = status_value
                d["usage"] = usage or d.get("usage", {})
                if error:
                    d["error"] = _bound(error, 200)
                return
        self._delegations.append({
            "attempt_id": attempt_id, "brief_version": None, "prompt_file": "",
            "prompt_sha256": "", "action": "", "native_thread_id": native_thread_id,
            "terminal_status": status_value, "usage": usage or {},
        })

    def finalize(self, outcome: str = "") -> dict:
        """写 manifest（每次收尾覆盖为当前状态；integrity 如实）。"""
        manifest = {
            "contract_version": CONTRACT_VERSION,
            "run_id": self.run_id,
            "updated_at_utc": _now_utc(),
            "mentis_version": self.mentis_version,
            "executor": self.executor,
            "delegations": self._delegations,
            "integrity": {
                "events_written": self._seq,
                "events_complete": self.write_failures == 0,
                "write_failures": self.write_failures,
                "note": ("write_failures>0 或 events_complete=false 时运行不得称完整追踪"
                         if self.write_failures else ""),
            },
        }
        if outcome:
            manifest["outcome"] = _bound(outcome, 80)
        try:
            self.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.write_failures += 1
        return manifest

    # ---- 内部 ----

    def _scan_max_seq(self) -> int:
        if not self.events_path.is_file():
            return 0
        import re

        max_seq = 0
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                max_seq = max(max_seq, int(json.loads(line).get("seq", 0)))
            except json.JSONDecodeError:
                # 半行/损坏行：仍提取已写出的 seq，避免续接复用该号
                match = re.search(r'"seq"\s*:\s*(\d+)', line)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
        return max_seq

    def _rebuild_delegations(self) -> list:
        delegations = []
        if not self.events_path.is_file():
            return delegations
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") != "delegation.prepared":
                continue
            refs = event.get("refs") or []
            prompt_ref = next((r for r in refs if r.get("role") == "prompt"), {})
            delegations.append({
                "attempt_id": event.get("attempt_id"), "brief_version": event.get("brief_version"),
                "prompt_file": prompt_ref.get("path", ""), "prompt_sha256": prompt_ref.get("sha256", ""),
                "action": str(event.get("summary", ""))[:60], "native_thread_id": "",
                "terminal_status": "", "usage": {},
            })
        return delegations


# ---- 事后回放 ----

def load_events(run_dir) -> list:
    path = Path(run_dir) / "trace" / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"seq": -1, "kind": "corrupt_line", "summary": "（损坏行，原样保留标记）",
                           "source": "observer", "status": "unknown"})
    return events


def render_timeline(run_dir) -> str:
    """有 trace/ 的新运行：按事件流回放（T1 事后；实时旁观属 T2，不在本命令）。"""
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    if not events:
        return render_historical(run_dir)
    lines = [f"# 追踪回放：{run_dir.name}", ""]
    lines.append("- 模式：**事后回放（T1）**；运行中逐条旁观属 T2，本命令不做。")
    manifest_path = run_dir / "trace" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            integrity = manifest.get("integrity", {})
            lines.append(f"- 契约 {manifest.get('contract_version')}；执行器 "
                         f"{manifest.get('executor', {}).get('name')} "
                         f"{manifest.get('executor', {}).get('cli_version')}；"
                         f"完整性 events_complete=**{integrity.get('events_complete')}**"
                         f"（写失败 {integrity.get('write_failures')}）"
                         + (f"；{integrity['note']}" if integrity.get("note") else ""))
            if manifest.get("outcome"):
                lines.append(f"- 运行结局：{manifest['outcome']}")
        except json.JSONDecodeError:
            lines.append("- manifest 不可解析：完整性未知")
    lines.append("")
    for e in events:
        refs = "；".join(
            f"{r.get('role')}={Path(r['path']).name}:{str(r.get('sha256', ''))[:12]}"
            for r in e.get("refs", []) if r.get("path"))
        lines.append(
            f"[{e.get('seq')}] {e.get('time_utc', '?')} [{e.get('source')}/{e.get('kind')}] "
            f"attempt={e.get('attempt_id') or '-'} brief=v{e.get('brief_version') or '-'} "
            f"status={e.get('status')}｜{e.get('summary', '')}"
            + (f"｜refs: {refs}" if refs else ""))
    lines.append("")
    return "\n".join(lines)


def render_historical(run_dir) -> str:
    """无 trace/ 的历史运行：列现存材料与 unrecorded 缺口；不回填、不生成事件。"""
    run_dir = Path(run_dir)
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return (f"# 追踪回放：{run_dir.name}\n\n- 不是可识别的运行目录（缺 state.json）；"
                "无时间线可回放。\n")
    meta = json.loads(state_path.read_text(encoding="utf-8"))
    is_guidance = meta.get("state_version") == "p3.2"
    lines = [f"# 追踪回放：{run_dir.name}（历史运行）", ""]
    lines.append("- 模式：**历史材料清单（该运行早于追踪契约，无 trace/ 事件流）**；"
                 "以下逐项标注「未记录」，不回填、不生成伪事件。")
    lines.append(f"- 目标：{meta.get('goal', '')[:120]}；状态 {meta.get('status')}；"
                 f"判定 {meta.get('verdict', '（无）')}")
    lines.append("")
    lines.append("## 现存材料（哈希为现在计算，供核对；非发送时记录）")
    seen = set()

    def add_file(path, role):
        p = Path(path)
        if not p.is_absolute() and not p.is_file():
            p = run_dir / p.name  # 历史记录存 CWD 相对路径；回放时回退到运行目录内
        key = str(p)
        if key in seen or not p.is_file():
            return
        seen.add(key)
        lines.append(f"- [{role}] `{p.name}`（sha256 前 12 位 {_sha256_file(p)[:12]}，"
                     f"大小 {p.stat().st_size}B）")

    if is_guidance:
        for r in meta.get("rounds", []):
            if r.get("instruction_path"):
                add_file(r["instruction_path"], f"round{r['round_no']} 确切指令（发送文本本身，无发送时哈希）")
            if r.get("codex_events_path"):
                add_file(r["codex_events_path"], f"round{r['round_no']} 执行器事件（仅类型/状态）")
            if r.get("diagnosis_path"):
                add_file(r["diagnosis_path"], f"round{r['round_no']} 诊断")
            if r.get("review_path"):
                add_file(r["review_path"], f"round{r['round_no']} 复检")
            mr = run_dir / f"model-review-round{r['round_no']}.json"
            if mr.is_file():
                add_file(mr, f"round{r['round_no']} 模型复查（含图像指纹绑定）")
        for name, role in (("v1/index.html", "v1 产物"), ("v2/index.html", "v2 产物"),
                           ("freeze.json", "冻结记录")):
            add_file(run_dir / name, role)
        gaps = ["事件级时间戳（事件文件只有 seq）", "执行器事件内容（命令/消息/推理）",
                "发送时 prompt 哈希", "失败轮的执行器用量（终态未取得，不可回填）",
                "统一时间线（本清单按文件组织，非事件流）"]
    else:
        for r in meta.get("codex_runs", []):
            lines.append(f"- 委托 {r.get('action_key')}（{r.get('attempt_id')}，状态 "
                         f"{r.get('status')}，时长 {r.get('duration_s')}s，用量 "
                         f"{r.get('usage') or '未取得'}）")
        for name, role in (("workspace/index.html", "作品"), ("workspace/brief.md", "brief"),
                           ("report.md", "三入口报告"), ("session.json", "会话记录")):
            add_file(run_dir / name, role)
        gaps = ["确切委托 prompt 文本（未落盘，仅可从 brief 事后重建——本清单不重建）",
                "执行器事件（完全未落盘）", "事件级时间戳", "发送时 prompt 哈希"]
    lines.append("")
    lines.append("## 未记录（unrecorded——契约 §9：不回填）")
    for gap in gaps:
        lines.append(f"- {gap}")
    lines.append("")
    return "\n".join(lines)
