"""P2.4-T1 统一追踪事件写入与事后回放（契约：docs/TRACKING_EVENT_CONTRACT.md v1.0-draft）。

纪律：
- 只观察不回灌；写入者只有运行流程，回放器只读。
- 确切委托文本在实际调用执行器前落盘（archive_prompt），失败则不发起该次委托。
- 事件写入失败计入 manifest.integrity，不得称"完整追踪"。
- 历史运行没有 trace/ 时不回填：回放展示现存材料与 unrecorded 缺口。
"""

import hashlib
import json
import re
import time
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

# T3 敏感字段规则：只对**已知字段名**的赋值做确定性隐去；不承诺识别任意秘密
# （契约 §6：不宣称简单正则可完全脱敏）。prompt 归档不经此函数——哈希须等于发送文本。
# T3 敏感字段规则：只对**已知字段名**的赋值做确定性隐去；不承诺识别任意秘密
# （契约 §6：不宣称简单正则可完全脱敏）。prompt 归档不经此函数——哈希须等于发送文本。
_SENSITIVE_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{8,})")
_SENSITIVE_KV = re.compile(
    r"""(?i)(["']?[\w.-]*(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd|credential|authorization|密码|密钥|令牌|凭证)[\w.-]*["']?\s*[=:：]\s*)(["']?)([^\s"',；;，」”]+)(["']?)""")


def redact_text(text: str) -> str:
    """已知密钥字段名（api_key/token/secret/password/credential/authorization/密码/密钥 等）
    的 key=value / key: value / "key": "value" 赋值与 Bearer 令牌 → 值隐去，字段名保留。
    用量计数字段（input_tokens 等）不是密钥，明确不隐去；值恰为 Bearer 一词时不动。"""

    def _sub_kv(m: "re.Match") -> str:
        name = m.group(1).strip("\"' :：=").lower()
        if name.endswith("tokens") or name.endswith("_count") or m.group(3).lower() == "bearer":
            return m.group(0)
        return m.group(1) + "⟨已隐去⟩"

    out = _SENSITIVE_BEARER.sub(r"\1 ⟨已隐去⟩", text)
    return _SENSITIVE_KV.sub(_sub_kv, out)


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
        self.write_failures = self._prior_write_failures()
        self._seq = self._scan_max_seq()
        self._delegations = self._rebuild_delegations()

    # ---- 事件 ----

    def emit(self, source: str, kind: str, summary: str, *, attempt_id: str | None = None,
             brief_version: int | None = None, native_thread_id: str | None = None,
             status: str = "ok", refs: list | None = None,
             executor_status: str | None = None, usage: dict | None = None) -> int:
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
            "summary": _bound(redact_text(summary)),
            "refs": refs or [],
        }
        if executor_status is not None:
            event["executor_status"] = executor_status
        if usage is not None:
            event["usage"] = usage
        try:
            with self.events_path.open("a+b") as out:
                out.seek(0, 2)
                if out.tell():
                    out.seek(-1, 2)
                    if out.read(1) != b"\n":
                        out.seek(0, 2)
                        out.write(b"\n")  # 保留半行，隔离下一条完整事件
                out.seek(0, 2)
                out.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
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
        audit = _inspect_event_stream(self.events_path, self.run_id)
        complete = self.write_failures == 0 and not audit["problems"]
        manifest = {
            "contract_version": CONTRACT_VERSION,
            "run_id": self.run_id,
            "updated_at_utc": _now_utc(),
            "mentis_version": self.mentis_version,
            "executor": self.executor,
            "delegations": self._delegations,
            "integrity": {
                "events_written": audit["valid_count"],
                "seq_max": self._seq,
                "events_complete": complete,
                "write_failures": self.write_failures,
                "note": "; ".join(audit["problems"][:5]) or (
                    "历史写入失败；运行不得称完整追踪" if self.write_failures else ""),
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

        max_seq = 0
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError("事件不是对象")
                max_seq = max(max_seq, int(parsed.get("seq", 0)))
            except (json.JSONDecodeError, ValueError, TypeError):
                # 半行/损坏行：仍提取已写出的 seq，避免续接复用该号
                match = re.search(r'"seq"\s*:\s*(\d+)', line)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
        return max_seq

    def _rebuild_delegations(self) -> list:
        delegations = []
        by_attempt = {}
        for event in load_events(self.run_dir):
            attempt_id = event.get("attempt_id")
            if event.get("kind") == "delegation.prepared" and event.get("status") == "ok":
                refs = event.get("refs") or []
                prompt_ref = next((r for r in refs if isinstance(r, dict) and r.get("role") == "prompt"), {})
                if not attempt_id or not prompt_ref.get("sha256"):
                    continue
                entry = {
                    "attempt_id": attempt_id, "brief_version": event.get("brief_version"),
                    "prompt_file": prompt_ref.get("path", ""),
                    "prompt_sha256": prompt_ref["sha256"],
                    "action": str(event.get("summary", ""))[:60], "native_thread_id": "",
                    "terminal_status": "", "usage": {},
                }
                delegations.append(entry)
                by_attempt[attempt_id] = entry
            elif event.get("kind") == "executor.completed" and attempt_id in by_attempt:
                entry = by_attempt[attempt_id]
                entry["native_thread_id"] = event.get("native_thread_id") or entry["native_thread_id"]
                entry["terminal_status"] = event.get("executor_status") or {
                    "ok": "completed", "failed": "failed", "unknown": "unknown",
                }.get(event.get("status"), "")
                if isinstance(event.get("usage"), dict):
                    entry["usage"] = event["usage"]
        # 旧版终态只保存在 manifest；仅在同 run、同 prompt 指纹时继承。
        try:
            prior = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        if prior.get("run_id") == self.run_id and prior.get("contract_version") == CONTRACT_VERSION:
            for old in prior.get("delegations", []):
                entry = by_attempt.get(old.get("attempt_id"))
                if entry and entry["prompt_sha256"] == old.get("prompt_sha256"):
                    for key in ("native_thread_id", "terminal_status", "usage", "error"):
                        if not entry.get(key) and old.get(key):
                            entry[key] = old[key]
        return delegations

    def _prior_write_failures(self) -> int:
        try:
            prior = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(prior, dict):
                return 0
            if prior.get("run_id") == self.run_id:
                return max(0, int(prior.get("integrity", {}).get("write_failures", 0)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return 0


def _inspect_event_stream(path: Path, run_id: str | None = None) -> dict:
    """从磁盘重新计算可读性，不信任写入器计数或旧 manifest。"""
    if not path.is_file():
        return {"valid_count": 0, "problems": ["事件文件缺失"]}
    data = path.read_bytes()
    problems, valid_count, previous = [], 0, 0
    if not data:
        problems.append("事件文件为空")
    if data and not data.endswith(b"\n"):
        problems.append("事件文件末尾半行")
    for number, raw in enumerate(data.splitlines(), 1):
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            problems.append(f"第 {number} 行损坏")
            continue
        seq = event.get("seq") if isinstance(event, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != previous + 1:
            problems.append(f"第 {number} 行 seq 不连续")
        if isinstance(seq, int):
            previous = seq
        if not isinstance(event, dict) or event.get("schema_version") != CONTRACT_VERSION:
            problems.append(f"第 {number} 行契约不匹配")
        if run_id is not None and isinstance(event, dict) and event.get("run_id") != run_id:
            problems.append(f"第 {number} 行 run_id 不匹配")
        valid_count += 1
    return {"valid_count": valid_count, "problems": problems}


# ---- 事后回放 ----

def load_events(run_dir) -> list:
    path = Path(run_dir) / "trace" / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("事件不是对象")
            events.append(parsed)
        except (json.JSONDecodeError, ValueError):
            events.append({"seq": -1, "kind": "corrupt_line", "summary": "（损坏行，原样保留标记）",
                           "source": "observer", "status": "unknown"})
    return events


def _resolve_ref(run_dir: Path, ref_path: str) -> Path:
    path = Path(ref_path)
    if path.is_absolute() or path.is_file():
        return path
    direct = run_dir / path
    if direct.is_file():
        return direct
    # 旧事件可能保存了相对 CWD 的完整运行路径；允许从运行目录名截取后缀。
    parts = path.parts
    if run_dir.name in parts:
        suffix = parts[parts.index(run_dir.name) + 1:]
        return run_dir.joinpath(*suffix)
    return direct


def render_timeline(run_dir) -> str:
    """有 trace/ 的新运行：按事件流回放（T1 事后；实时旁观属 T2，不在本命令）。"""
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    if not events:
        return render_historical(run_dir)
    lines = [f"# 追踪回放：{run_dir.name}", ""]
    lines.append("- 模式：**事后回放（T1）**；运行中逐条旁观属 T2，本命令不做。")
    ref_problems = []
    for event in events:
        refs = event.get("refs", [])
        if not isinstance(refs, list):
            ref_problems.append(f"seq {event.get('seq')}: refs 格式错误")
            continue
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get("path"), str)
                    or not ref.get("path") or not isinstance(ref.get("sha256"), str)
                    or not ref.get("sha256")):
                ref_problems.append(f"seq {event.get('seq')}: 引用缺路径或哈希")
                continue
            path = _resolve_ref(run_dir, ref["path"])
            if not path.is_file():
                ref_problems.append(f"seq {event.get('seq')}: {path.name} 缺失")
            elif _sha256_file(path) != ref["sha256"]:
                ref_problems.append(f"seq {event.get('seq')}: {path.name} 哈希不匹配")
    manifest_path = run_dir / "trace" / "manifest.json"
    manifest_integrity = None
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest 不是对象")
            integrity = manifest.get("integrity", {})
            if not isinstance(integrity, dict):
                raise ValueError("manifest.integrity 不是对象")
            manifest_integrity = integrity
            lines.append(f"- 契约 {manifest.get('contract_version')}；执行器 "
                         f"{manifest.get('executor', {}).get('name')} "
                         f"{manifest.get('executor', {}).get('cli_version')}；"
                         f"完整性 events_complete=**{integrity.get('events_complete')}**"
                         f"（写失败 {integrity.get('write_failures')}）"
                         + (f"；{integrity['note']}" if integrity.get("note") else ""))
            if manifest.get("outcome"):
                lines.append(f"- 运行结局：{manifest['outcome']}")
        except (json.JSONDecodeError, ValueError, AttributeError):
            manifest = {}
            manifest_integrity = None
            lines.append("- manifest 不可解析：完整性未知")
    stream_audit = _inspect_event_stream(run_dir / "trace" / "events.jsonl",
                                         manifest.get("run_id") if isinstance(manifest, dict) else None)
    if manifest_integrity is not None and (
            manifest_integrity.get("events_written") != stream_audit["valid_count"]):
        stream_audit["problems"].append("manifest 事件数与当前事件文件不一致")
    audit_ok = (manifest_integrity is not None
                and manifest.get("contract_version") == CONTRACT_VERSION
                and manifest_integrity.get("events_complete") is True
                and not stream_audit["problems"] and not ref_problems)
    lines.append(f"- 当前核验：**{'通过' if audit_ok else '未通过'}**；可读事件 "
                 f"{stream_audit['valid_count']}；引用异常 {len(ref_problems)}"
                 + ("；" + "；".join((stream_audit["problems"] + ref_problems)[:5])
                    if stream_audit["problems"] or ref_problems else ""))
    # T3：委托台账（超时/恢复/多 attempt 的连续时间线）——来自 manifest，缺失时如实省略
    delegations = manifest.get("delegations") if isinstance(manifest, dict) else None
    session_attempts = _load_session_attempts(run_dir)
    if isinstance(delegations, list) and delegations:
        lines.append("- 委托台账（attempt → 终态；续接共用同一原生 thread，原判定不覆盖）：")
        for d in delegations:
            if not isinstance(d, dict):
                continue
            line = (f"  - {d.get('attempt_id') or '?'}（brief v{d.get('brief_version') or '?'}）"
                    f"→ 终态 {d.get('terminal_status') or '（未取得）'}"
                    f"｜thread {str(d.get('native_thread_id') or '未关联')[:24]}"
                    f"｜prompt {str(d.get('prompt_sha256') or '')[:12]}")
            # T4：终态未入事件流时交叉引用同目录 session.json（只读展示，不回填事件）——
            # 人工核对确认（executor.confirmed 之前的运行）的依据在该文件
            attempt_id = d.get("attempt_id")
            recorded = session_attempts.get(attempt_id) if attempt_id else None
            if recorded is not None and not d.get("terminal_status"):
                line += (f"｜session.json 记该尝试 status={recorded.get('status', '?')}"
                         f"{('、accepted=' + ('是' if recorded.get('accepted') else '否')) if recorded.get('status') == 'completed' else ''}"
                         "（确认通知见该文件 interactions；不回填为事件）")
            lines.append(line)
        tally = _executor_item_tally(events)
        if tally:
            lines.append("- 执行器子项小结（自既有事件摘要后缀统计，非新增事件）：")
            for attempt_id, counts in tally.items():
                lines.append(f"  - {attempt_id or '（无 attempt）'}：完成 {counts['completed']}、"
                             f"失败 {counts['failed']}、仅开始未到终态 {counts['started_only']}"
                             "（开始数含已完成动作的开始事件；子命令成败以终态计）")
    lines.append("")
    prev_time = None
    for e in events:
        resumed_note = ""
        if e.get("kind") == "run.resumed":
            gap_s = _gap_seconds(prev_time, e.get("time_utc"))
            if gap_s is not None:
                resumed_note = f"｜中断区间 {gap_s:.0f}s（期间无记录，不回填）"
        event_refs = e.get("refs") if isinstance(e.get("refs"), list) else []
        refs = "；".join(
            f"{r.get('role')}={Path(r['path']).name}:{str(r.get('sha256', ''))[:12]}"
            for r in event_refs if isinstance(r, dict) and isinstance(r.get("path"), str))
        lines.append(
            f"[{e.get('seq')}] {e.get('time_utc', '?')} [{e.get('source')}/{e.get('kind')}] "
            f"attempt={e.get('attempt_id') or '-'} brief=v{e.get('brief_version') or '-'} "
            f"status={e.get('status')}｜{e.get('summary', '')}"
            + (f"｜refs: {refs}" if refs else "") + resumed_note)
        if isinstance(e.get("time_utc"), str):
            prev_time = e.get("time_utc")
    lines.append("")
    return "\n".join(lines)


def _gap_seconds(start: str | None, end: str | None) -> float | None:
    try:
        if not start or not end:
            return None
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return max(0.0, delta.total_seconds())
    except (TypeError, ValueError):
        return None


def _load_session_attempts(run_dir: Path) -> dict:
    """读取同目录 session.json 的尝试状态（只读；不存在或不可解析时返回空）。

    用途：终态未入事件流的委托（如 executor.confirmed 之前的人工核对确认），
    回放交叉引用会话记录中的尝试状态与确认通知位置，不回填事件。
    """
    path = Path(run_dir) / "session.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        attempts = data.get("attempts", [])
        if not isinstance(attempts, list):
            return {}
        return {a.get("attempt_id"): a for a in attempts
                if isinstance(a, dict) and a.get("attempt_id")}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _executor_item_tally(events: list) -> dict:
    """按 attempt 统计执行器子项成败（自事件 summary 的状态后缀解析，只读）。

    summary 由写入方按「…（completed/failed/in_progress）」格式记录；此处不改写、
    不新增事件，仅聚合展示。"仅开始未到终态" = in_progress 数 − 已到终态数（下限 0），
    对成对流是准确值；异常流如实显示估算基数。
    """
    tally: dict = {}
    for e in events:
        if e.get("source") != "executor" or e.get("kind") != "executor.item":
            continue
        summary = str(e.get("summary", ""))
        if summary.endswith("（completed）"):
            bucket = "completed"
        elif summary.endswith("（failed）"):
            bucket = "failed"
        elif summary.endswith("（in_progress）"):
            bucket = "in_progress"
        else:
            continue
        counts = tally.setdefault(e.get("attempt_id"),
                                  {"completed": 0, "failed": 0, "in_progress": 0})
        counts[bucket] += 1
    for counts in tally.values():
        counts["started_only"] = max(0, counts["in_progress"] - counts["completed"] - counts["failed"])
    return tally


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


# ---- T2 实时旁观 ----

def format_event(e: dict) -> str:
    """单行可读格式（与 render_timeline 的时间线一致）。"""
    event_refs = e.get("refs") if isinstance(e.get("refs"), list) else []
    refs = "；".join(
        f"{r.get('role')}={Path(r['path']).name}:{str(r.get('sha256', ''))[:12]}"
        for r in event_refs if isinstance(r, dict) and isinstance(r.get("path"), str))
    return (f"[{e.get('seq')}] {e.get('time_utc', '?')} [{e.get('source')}/{e.get('kind')}] "
            f"attempt={e.get('attempt_id') or '-'} brief=v{e.get('brief_version') or '-'} "
            f"status={e.get('status')}｜{e.get('summary', '')}"
            + (f"｜refs: {refs}" if refs else ""))


def follow_timeline(run_dir, out=print, poll_s: float = 0.5, stop_check=None):
    """T2 只读旁观：重放已有事件后跟随新增；Ctrl+C 只停止读取，不影响运行。

    终止：看到 run.finalized 且 manifest 已落盘（正常收尾）、stop_check 返回 True、
    或调用者 Ctrl+C。半行容错：只消费完整行，末尾半行留待下次。
    """
    run_dir = Path(run_dir)
    path = run_dir / "trace" / "events.jsonl"
    manifest_path = run_dir / "trace" / "manifest.json"
    if not path.is_file():
        out("该目录尚无 trace/ 事件流（可能为历史运行或运行未开始）；以下为现存材料视图：")
        out(render_historical(run_dir))
        return
    out("[旁观中] 只读跟随 trace/events.jsonl；Ctrl+C 仅停止旁观，不影响运行（取消/续接走现有状态契约）。")
    offset = 0
    seen_finalized = False
    quiet_polls = 0
    last_size = -1
    try:
        while True:
            data = path.read_bytes() if path.is_file() else b""
            if len(data) > offset:
                chunk = data[offset:]
                cut = chunk.rfind(b"\n")
                if cut >= 0:
                    for line in chunk[:cut].decode("utf-8", errors="replace").splitlines():
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            out("（损坏行，跳过）")
                            continue
                        out(format_event(event))
                        if event.get("kind") == "run.finalized":
                            seen_finalized = True
                    offset += cut + 1
            if seen_finalized and manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    integrity = manifest.get("integrity", {})
                    out(f"[运行已收尾] outcome={manifest.get('outcome', '')}；"
                        f"integrity events_complete={integrity.get('events_complete')}"
                        f"（写失败 {integrity.get('write_failures')}）")
                except (json.JSONDecodeError, OSError):
                    out("[运行已收尾] manifest 不可解析")
                return
            # 兜底：运行收尾但无 run.finalized 事件（如探针/旧版写入器直接 finalize）——
            # manifest 已存在且事件流连续数轮无增长 → 按收尾处理
            if manifest_path.is_file():
                if len(data) == last_size:
                    quiet_polls += 1
                    if quiet_polls >= 4:
                        try:
                            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                            outcome = manifest.get("outcome", "")
                        except (json.JSONDecodeError, OSError):
                            outcome = ""
                        out(f"[运行已收尾（按 manifest，事件流静默）] outcome={outcome}")
                        return
                else:
                    quiet_polls = 0
            last_size = len(data)
            if stop_check is not None and stop_check():
                out("[旁观停止] 应调用方要求；运行不受影响。")
                return
            time.sleep(poll_s)
    except KeyboardInterrupt:
        out("\n[旁观已停止] 仅停止读取，不影响运行；取消/续接按现有状态契约处理。")
