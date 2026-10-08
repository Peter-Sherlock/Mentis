"""P2.4-T1 统一追踪事件写入与事后回放（契约：docs/TRACKING_EVENT_CONTRACT.md v1.0-draft）。

纪律：
- 只观察不回灌；写入者只有运行流程，回放器只读。
- 确切委托文本在实际调用执行器前落盘（archive_prompt），失败则不发起该次委托。
- 事件写入失败计入 manifest.integrity，不得称"完整追踪"。
- 历史运行没有 trace/ 时不回填：回放展示现存材料与 unrecorded 缺口。
"""

import hashlib
import json
import os
import re
import threading
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

# O1：公开消息摘要「首尾保留」。全文归档是完整依据；摘要只是索引，
# 中文长消息不得只剩前 160 字（T4 缺口：尾部关键说明被截丢）。
MESSAGE_SUMMARY_BUDGET = 160
MESSAGE_SUMMARY_TAIL = 40
MESSAGE_ELISION = "…（中略）…"
MODEL_CALLS_SCHEMA = "mentis-model-call/1"

# O2：阶段与生产者活动信号。stages 追加式；heartbeat 原子重写（单写者）。
STAGES_SCHEMA = "mentis-run-stage/1"
HEARTBEAT_SCHEMA = "mentis-heartbeat/1"
STAGE_NAMES = ("plan", "build", "observe", "diagnose", "adjudicate",
               "revise", "reverify", "waiting_user", "finalize")
HEARTBEAT_INTERVAL_S = 15.0
HEARTBEAT_TTL_S = 45.0


def bounded_text(text: str, budget: int = MESSAGE_SUMMARY_BUDGET,
                 tail: int = MESSAGE_SUMMARY_TAIL) -> str:
    """首尾保留的有界摘要：超出预算时保留头部与尾部，中间以省略标记连接。
    全文以 trace/messages/ 归档为准；此处只影响摘要可读性。"""
    text = " ".join(str(text).split())
    if len(text) <= budget:
        return text
    head_len = max(1, budget - tail - len(MESSAGE_ELISION))
    return text[:head_len] + MESSAGE_ELISION + text[-tail:]

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


def describe_executor_event(event: dict, meta: dict, archive_message=None) -> tuple:
    """三入口（collab / guide-revise / eval-run）共用的执行器事件采集逻辑。

    返回 (detail, refs)：
    - agent_message（item.completed）：公开回复全文经 archive_message 归档并以
      SHA-256 引用；摘要首尾保留，归档失败在摘要中显式标记完整性缺口（不静默）。
    - command_execution：动作状态明确（completed/failed/in_progress，缺失记
      unknown）；命令输出未采集，摘要尾部保留「输出未采集」缺口标记——
      状态后缀仍居末位，供回放子项小结解析。
    - reasoning：只记类型，内容不入默认层。
    archive_message(text) -> ref|None 由调用方注入（TraceWriter.archive_executor_message）。
    本函数不抛异常：意外错误降级为「采集异常」占位摘要，由调用方继续落盘留痕。
    """
    refs: list = []
    try:
        kind = str(event.get("type", "?"))
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        itype = str(item.get("type", ""))
        raw_status = str(item.get("status", "") or "")
        status = raw_status or "unknown"
        if itype == "command_execution":
            detail = (f"命令 {str(item.get('command', ''))[:140]}；"
                      f"输出未采集（{status}）")
        elif kind == "item.completed" and itype == "agent_message":
            text = str(item.get("text", ""))
            preview = bounded_text(text)
            if archive_message is None:
                detail = f"最终消息（全文未归档）：「{preview}」"
            else:
                message_ref = archive_message(text)
                if message_ref is not None:
                    refs.append(message_ref)
                    detail = f"最终消息（全文已归档）：「{preview}」"
                else:
                    detail = f"最终消息（全文归档失败：追踪完整性缺口）：「{preview}」"
        elif itype == "reasoning":
            detail = "推理条目（内容不入默认层）"
        else:
            detail = f"{kind} {itype}".strip() + (f"（{raw_status}）" if raw_status else "")
        if meta.get("first"):
            detail = f"首事件延迟 {meta.get('first_event_latency_s', 0):.1f}s；" + detail
        return detail, refs
    except Exception as exc:  # 采集自身异常不中断执行器；占位留痕（写入方再失败计完整性）
        return f"事件采集异常（{type(exc).__name__}）；原文未入流", []


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
        self.messages_dir = self.trace_dir / "messages"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.trace_dir / "events.jsonl"
        self.manifest_path = self.trace_dir / "manifest.json"
        self.model_calls_path = self.trace_dir / "model-calls.jsonl"
        self.stages_path = self.trace_dir / "stages.jsonl"
        self.heartbeat_path = self.trace_dir / "heartbeat.json"
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
             executor_status: str | None = None, usage: dict | None = None,
             interaction_id: str | None = None) -> int:
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
        if interaction_id is not None:
            # 审查修正 A：请求/答复的稳定关联标识；未提供则不留字段（历史事件不回填）
            event["interaction_id"] = interaction_id
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

    def archive_executor_message(self, attempt_id: str, message_text: str) -> dict | None:
        """保存 Codex 对外消息全文；失败只降低追踪完整性，不中断执行器。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id):
            self.write_failures += 1
            return None
        payload = message_text.encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        path = self.messages_dir / f"{attempt_id}-{sha}.md"
        try:
            self.messages_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError:
            self.write_failures += 1
            return None
        return {"path": str(path), "sha256": sha, "role": "executor_message"}

    def record_model_call(self, call, stage: str = "", output_refs: list | None = None,
                          config: dict | None = None) -> bool:
        """O1：把已采集的模型调用追加到 trace/model-calls.jsonl（调用方在关联点调用，
        传入该调用的用途阶段与产出引用）。失败计 write_failures，不静默。
        模型输入全文不保存（prompt 只存哈希指纹）；未知字段如实写 unrecorded。
        call 为 CallRecord 形状（request_id 属性）或含 request_id 的 dict；其他形状
        （如测试夹具把收到的 prompt 记进 calls）不是调用记录，跳过且不算失败。
        C1：config={"version","sha256","stage_responsibility"} 时写入配置关联字段；
        未提供（含历史运行）不留这些字段，不回填。"""
        if isinstance(call, dict):
            if not call.get("request_id"):
                return True
            source = {key: call.get(key) for key in
                      ("request_id", "model", "host", "created_at", "latency_ms", "ok",
                       "usage", "error", "mock", "http_attempted", "prompt_sha256",
                       "image_refs")}
        elif getattr(call, "request_id", None):
            source = {
                "request_id": getattr(call, "request_id", ""),
                "model": getattr(call, "model", ""),
                "host": getattr(call, "host", ""),
                "created_at": getattr(call, "created_at", ""),
                "latency_ms": getattr(call, "latency_ms", None),
                "ok": getattr(call, "ok", False),
                "usage": getattr(call, "usage", {}),
                "error": getattr(call, "error", ""),
                "mock": getattr(call, "mock", False),
                "http_attempted": getattr(call, "http_attempted", False),
                "prompt_sha256": getattr(call, "prompt_sha256", ""),
                "image_refs": getattr(call, "image_refs", []),
            }
        else:
            return True
        payload = {
            "schema": MODEL_CALLS_SCHEMA,
            "request_id": source["request_id"] or "unrecorded",
            "model": source["model"] or "unrecorded",
            "host": source["host"] or "",
            "created_at": source["created_at"] or "unrecorded",
            "latency_ms": source["latency_ms"],
            "ok": bool(source["ok"]),
            "usage": dict(source["usage"] or {}),
            "error": _bound(str(source["error"] or ""), 200),
            "mock": bool(source["mock"]),
            "http_attempted": bool(source["http_attempted"]),
            "prompt_sha256": source["prompt_sha256"] or "unrecorded",
            "image_refs": list(source["image_refs"] or []),
            "stage": stage or "unrecorded",
            "output_refs": list(output_refs or []),
            "input_text_archived": False,
        }
        if isinstance(config, dict) and config.get("version"):
            payload["config_version"] = config.get("version")
            payload["config_sha256"] = config.get("sha256", "")
            payload["stage_responsibility"] = config.get("stage_responsibility", "")
        try:
            with self.model_calls_path.open("ab") as out:
                out.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            self.write_failures += 1
            return False
        return True

    def note_capture_failure(self, reason: str):
        """采集/写入辅助记录失败时显式计入完整性（不静默吞掉）。"""
        with threading.Lock():
            self.write_failures += 1
        self._capture_failures = getattr(self, "_capture_failures", [])
        self._capture_failures.append(_bound(reason, 200))

    # ---- O2：阶段记录与生产者活动信号 ----

    def stage_tracker(self) -> "StageTracker":
        """三入口共用的阶段记录器（trace/stages.jsonl，追加式）。"""
        return StageTracker(self)

    def start_heartbeat(self, producer_role: str = "mentis-process",
                        interval_s: float | None = None,
                        ttl_s: float | None = None) -> "Heartbeat":
        """启动独立于旁观器的生产者活动信号（后台线程周期刷新 heartbeat.json）。
        信号过期只表示「无法确认生产者活跃」，不代表失败、不解锁、不自动重发。"""
        heartbeat = Heartbeat(self, producer_role=producer_role,
                              ttl_s=HEARTBEAT_TTL_S if ttl_s is None else ttl_s)
        heartbeat.start(interval_s=HEARTBEAT_INTERVAL_S if interval_s is None else interval_s)
        self._heartbeat = heartbeat
        return heartbeat

    def stop_heartbeat(self):
        heartbeat = getattr(self, "_heartbeat", None)
        if heartbeat is not None:
            heartbeat.stop()
            self._heartbeat = None

    def current_generation(self) -> int:
        return current_generation(self.run_dir)

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
        capture_failures = getattr(self, "_capture_failures", [])
        note_parts = list(audit["problems"][:5]) + capture_failures[:5]
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
                "note": "; ".join(note_parts) or (
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


class StageTracker:
    """三入口共用的阶段记录器：trace/stages.jsonl 追加式（mentis-run-stage/1）。

    阶段只记录实际发生的（计划/执行器制作/观察/诊断/处置/修订/复检/等待用户/收尾）；
    起止关联代次、attempt、brief/计划版本。新 start 时上一阶段若未关闭（含恢复前
    旧进程遗留的未关闭阶段），以 status=interrupted 如实收尾（不虚构正常结束）。
    写失败计追踪写失败。
    """

    def __init__(self, writer: TraceWriter):
        self.writer = writer
        self._open = None  # (stage, instance, start_monotonic, start_iso)
        self._open_interaction_id = ""  # 当前打开等待的关联标识（审查修正 A）
        self._reconciled = False

    def _unmatched(self) -> list:
        """文件中「有 start 无 end」的阶段实例（按出现顺序）。"""
        return [key for key, _ in self._unmatched_rows()]

    def _unmatched_rows(self) -> list:
        """同 _unmatched，但返回 (key, start_row)；供按 interaction_id 匹配。"""
        counts: dict = {}
        unmatched: list = []
        for row in load_stages(self.writer.run_dir):
            if row.get("schema") == "corrupt_line" or row.get("phase") not in ("start", "end"):
                continue
            key = (str(row.get("stage")), row.get("instance"))
            if row.get("phase") == "start":
                counts[key] = counts.get(key, 0) + 1
                unmatched.append((key, row))
            else:
                if key in counts and counts[key] > 0:
                    counts[key] -= 1
                    # 从尾部移除一个匹配的 start
                    for i in range(len(unmatched) - 1, -1, -1):
                        if unmatched[i][0] == key:
                            unmatched.pop(i)
                            break
        return unmatched

    def _reconcile(self):
        """恢复场景：旧进程遗留的未关闭阶段如实补 interrupted 结束（只做一次）。"""
        if self._reconciled:
            return
        self._reconciled = True
        if self._open is not None:
            return  # 同一进程内已有打开阶段，无需对账
        for stage, instance in self._unmatched():
            self._append_end(stage, instance, status="interrupted",
                             detail="恢复/续办后发现未关闭：由新代次如实关闭，不虚构正常结束")

    def _next_instance(self, stage: str) -> int:
        existing = [r for r in load_stages(self.writer.run_dir)
                    if r.get("stage") == stage and r.get("phase") == "start"]
        return len(existing) + 1

    def _append(self, payload: dict) -> bool:
        try:
            with self.writer.stages_path.open("ab") as out:
                out.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            self.writer.note_capture_failure(f"阶段记录写入失败（{payload.get('stage')}）")
            return False
        return True

    def _base(self, stage: str, instance: int) -> dict:
        return {"schema": STAGES_SCHEMA, "run_id": self.writer.run_id,
                "generation": self.writer.current_generation(), "stage": stage,
                "instance": instance}

    def _append_end(self, stage: str, instance: int, status: str, detail: str,
                    duration_s=None, started_iso: str = "") -> bool:
        payload = self._base(stage, instance)
        payload.update({"phase": "end", "time_utc": _now_utc(), "status": status,
                        "duration_s": duration_s,
                        "started_at_utc": started_iso,
                        "detail": _bound(detail, 200)})
        return self._append(payload)

    def start(self, stage: str, detail: str = "", attempt_id: str | None = None,
              brief_version: int | None = None, plan: dict | None = None,
              blocking: bool | None = None, reason: str = "",
              config: dict | None = None,
              interaction_id: str | None = None) -> int | None:
        if stage not in STAGE_NAMES:
            stage = "unrecorded_stage"
        self._reconcile()
        if self._open is not None:
            # 上一个阶段未关闭（异常路径）：如实记 interrupted，不冒充正常结束
            self.finish(status="interrupted", detail="新阶段开始时仍未关闭，如实记中断")
        instance = self._next_instance(stage)
        payload = self._base(stage, instance)
        payload.update({"phase": "start", "time_utc": _now_utc(),
                        "attempt_id": attempt_id, "brief_version": brief_version,
                        "plan": plan or {}, "detail": _bound(detail, 200)})
        if isinstance(config, dict) and config.get("version"):
            # C1：阶段绑定专业配置版本/哈希；未冻结时不留字段（不回填）
            payload["config"] = {"version": config.get("version"),
                                 "sha256": config.get("sha256", "")}
        if interaction_id:
            # 审查修正 A：waiting_user 与决定请求的关联标识；其他阶段不留
            payload["interaction_id"] = interaction_id
        if blocking is not None:
            payload["blocking"] = bool(blocking)
        if reason:
            payload["reason"] = _bound(reason, 300)
        if not self._append(payload):
            return None
        self._open = (stage, instance, time.monotonic(), payload["time_utc"])
        self._open_interaction_id = interaction_id or ""
        return instance

    def finish(self, status: str = "ok", detail: str = "") -> bool:
        if self._open is None:
            return False
        stage, instance, started_mono, started_iso = self._open
        self._open = None
        duration_s = round(time.monotonic() - started_mono, 3)
        if duration_s < 0:  # 时钟异常：不生成负数耗时
            duration_s = None
        return self._append_end(stage, instance, status, detail,
                                duration_s=duration_s, started_iso=started_iso)

    def finish_waiting(self, status: str = "ok", detail: str = "",
                       interaction_id: str | None = None) -> bool:
        """关闭等待用户阶段（同进程打开的，或恢复前旧进程遗留的）。
        不经 _reconcile——避免把仍待回答的等待阶段抢先记成中断。
        审查修正 A：给出 interaction_id 时只关闭对应的那次等待（多决定不误关）；
        无法按标识匹配时退回「最近一个未匹配等待」（历史行没有该字段）。"""
        if self._open is not None and self._open[0] == "waiting_user":
            if not interaction_id or not self._open_interaction_id \
                    or self._open_interaction_id == interaction_id:
                return self.finish(status=status, detail=detail)
        waiting = [(key, row) for key, row in self._unmatched_rows()
                   if key[0] == "waiting_user"]
        if interaction_id:
            matched = [(key, row) for key, row in waiting
                       if str(row.get("interaction_id", "")) == interaction_id]
            if matched:
                stage, instance = matched[-1][0]
                return self._append_end(stage, instance, status, detail)
            return False  # 对应等待不存在/已关闭：不误关其他未决等待
        if waiting:
            stage, instance = waiting[-1][0]
            return self._append_end(stage, instance, status, detail)
        return False

    def finish_open_stages(self):
        """收尾兜底：仍有未关闭阶段时以 interrupted 如实关闭。"""
        if self._open is not None:
            self.finish(status="interrupted", detail="运行收尾时阶段未正常关闭")

    def close_stray(self, status: str = "interrupted", keep_waiting: bool = True):
        """关闭未配对阶段；keep_waiting=True 保留未决的等待用户阶段（等待跨进程持续，
        由后续回答/续办如实关闭）。"""
        open_key = (self._open[0], self._open[1]) if self._open is not None else None
        for stage, instance in self._unmatched():
            if (stage, instance) == open_key:
                continue
            if keep_waiting and stage == "waiting_user":
                continue  # 等待仍未决：不冒充结束
            self._append_end(stage, instance, status=status,
                             detail="运行收尾时未正常关闭，如实记中断")
        if self._open is not None and not (keep_waiting and self._open[0] == "waiting_user"):
            self.finish(status=status, detail="运行收尾时未正常关闭")


class Heartbeat:
    """生产者活动信号：独立于旁观器，由生产者进程内的后台线程周期刷新
    trace/heartbeat.json（原子替换，单写者）。记录生产者身份、运行代次、
    时刻、有效期与来源。信号缺失/过期/损坏只表示「无法确认活跃」，
    不得推断任务失败、不解锁、不自动重发。"""

    def __init__(self, writer: TraceWriter, producer_role: str = "mentis-process",
                 ttl_s: float = HEARTBEAT_TTL_S):
        self.writer = writer
        self.producer_role = producer_role
        self.ttl_s = max(1.0, float(ttl_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self) -> bool:
        generation = self.writer.current_generation()
        payload = {
            "schema": HEARTBEAT_SCHEMA,
            "run_id": self.writer.run_id,
            "generation": generation if generation else None,  # 无事件流时代次不可导出
            "producer": {"pid": os.getpid(), "role": self.producer_role},
            "time_utc": _now_utc(),
            "ttl_s": self.ttl_s,
            "source": "mentis-process（生产者进程内后台线程；独立于旁观器）",
        }
        tmp_path = self.writer.heartbeat_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            os.replace(tmp_path, self.writer.heartbeat_path)
            return True
        except OSError:
            self.writer.note_capture_failure("活动信号写入失败；实时状态不可靠")
            return False

    def touch(self) -> bool:
        return self._write()

    def _loop(self, interval_s: float):
        while not self._stop.wait(interval_s):
            self._write()

    def start(self, interval_s: float = HEARTBEAT_INTERVAL_S):
        self.touch()
        self._thread = threading.Thread(target=self._loop, args=(max(0.5, interval_s),),
                                        daemon=True, name="mentis-heartbeat")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        # 停止时不写「已停止」记录：信号随后自然过期，状态转为不确定（不冒充任何终态）


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


def load_model_calls(run_dir) -> list:
    """读取 trace/model-calls.jsonl（O1 起新运行写入；旧运行无此文件返回空）。
    损坏行原样标记，不静默丢弃。"""
    path = Path(run_dir) / "trace" / "model-calls.jsonl"
    if not path.is_file():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("调用记录不是对象")
            calls.append(parsed)
        except (json.JSONDecodeError, ValueError):
            calls.append({"schema": "corrupt_line", "request_id": "unrecorded",
                          "error": "（损坏行，原样保留标记）", "usage": {}})
    return calls


def current_generation(run_dir) -> int:
    """运行代次 = 1 + run.resumed 事件数（O2；从既有事件确定性推导）。
    恢复/续办产生新代次：旧代次的收尾与活动信号不得覆盖新代次状态。
    无事件流的运行返回 0（代次 unrecorded，由读取方标注）。"""
    events = load_events(run_dir)
    if not events:
        return 0
    return 1 + sum(1 for e in events if e.get("kind") == "run.resumed")


def load_stages(run_dir) -> list:
    """读取 trace/stages.jsonl（O2 起新运行写入；旧运行无此文件返回空）。
    损坏行原样标记为 corrupt_line，不静默丢弃。"""
    path = Path(run_dir) / "trace" / "stages.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("阶段记录不是对象")
            rows.append(parsed)
        except (json.JSONDecodeError, ValueError):
            rows.append({"schema": "corrupt_line", "stage": "unrecorded",
                         "detail": "（损坏行，原样保留标记；实时状态不可据此声称可靠）"})
    return rows


def load_heartbeat(run_dir) -> dict:
    """读取 trace/heartbeat.json（O2 起新运行写入）。返回原始内容并附读取结论：
    missing（未采集）/ ok / corrupt（格式损坏）/ 额外字段由读取方按代次与有效期判定。
    任何异常都不推断生产者已失败。"""
    path = Path(run_dir) / "trace" / "heartbeat.json"
    if not path.is_file():
        return {"read": "missing", "note": "活动信号未采集（O2 之前的运行或未启动生产者信号）"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"read": "corrupt", "note": "活动信号文件损坏；实时状态不可靠，不据此断言失败"}
    if not isinstance(data, dict) or not data.get("time_utc"):
        return {"read": "corrupt", "note": "活动信号格式不完整；实时状态不可靠"}
    data["read"] = "ok"
    return data


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


def _verified_exchange_text(run_dir: Path, ref: dict, role: str) -> tuple[str | None, str]:
    """交流视图只读取本运行 trace/ 内且哈希匹配的文本。"""
    if not isinstance(ref, dict) or ref.get("role") != role:
        return None, "引用缺失"
    raw_path = ref.get("path")
    expected = ref.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(expected, str):
        return None, "引用路径或哈希缺失"
    path = _resolve_ref(run_dir, raw_path).resolve()
    if not path.is_relative_to((run_dir / "trace").resolve()):
        return None, "引用不在本运行 trace/ 内"
    try:
        data = path.read_bytes()
    except OSError:
        return None, "引用文件缺失"
    if hashlib.sha256(data).hexdigest() != expected:
        return None, "引用文件哈希不匹配"
    try:
        return data.decode("utf-8"), ""
    except UnicodeError:
        return None, "引用文件不是 UTF-8 文本"


def _literal_block(value: str) -> str:
    """Markdown 中保留公开消息原文，包括其中可能出现的反引号。"""
    fence = "```"
    while fence in value:
        fence += "`"
    return f"{fence}text\n{value}\n{fence}"


def render_conversation(run_dir) -> str:
    """本地可见交流与专业决策：确切委托、执行器公开消息与动作摘要，以及 Mentis 的
    评估计划、观察、诊断与处置决定（O1 补齐，按事件真实顺序）；不解析原生 rollout。
    零委托运行如实显示零委托——不把没有执行器的流程伪装成 Mentis 与 Codex 对话。"""
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    lines = [f"# Mentis 与 Codex 的可见交流：{run_dir.name}", "",
             "本地只读视图；依现存追踪文件生成，不增补原始事件。"
             "含发送给执行器的指令、执行器对外消息和有界动作摘要，以及 Mentis 的"
             "评估计划、观察、诊断与处置决定（标注主体与来源）；"
             "不含内部推理或原生命令输出。", ""]
    if not events:
        lines += ["该运行没有可回放的事件流；不能据此重建交流。", ""]
        return "\n".join(lines)
    lines.append(f"原始事件 {len(events)} 条；来源 `trace/events.jsonl`。"
                 "凡只留有摘要或引用损坏之处均逐项标明。")
    delegations = sum(1 for e in events if e.get("kind") == "delegation.prepared"
                      and e.get("status") == "ok")
    if delegations:
        lines.append(f"本运行记录到 {delegations} 次委托；同一委托中的多条 Codex 消息是进度或结果，"
                     "不表示 Mentis 在这些消息之间逐条回复。")
    else:
        lines.append("本运行记录到 **0 次委托**：下方没有 Codex 回复属于真实行为"
                     "（零委托），不为展示交流补造执行器消息。")
    verdict_line = _load_state_verdict(run_dir)
    if verdict_line:
        lines.append(verdict_line)
    lines.append("")
    for event in events:
        seq = event.get("seq", "?")
        kind = event.get("kind", "?")
        source = event.get("source", "?")
        attempt = event.get("attempt_id") or "-"
        summary = str(event.get("summary", ""))
        refs = event.get("refs") if isinstance(event.get("refs"), list) else []
        if kind == "delegation.prepared" and event.get("status") == "ok":
            ref = next((r for r in refs if isinstance(r, dict) and r.get("role") == "prompt"), None)
            message, error = _verified_exchange_text(run_dir, ref, "prompt")
            lines.append(f"## [{seq}] Mentis 发给 Codex｜{attempt}｜brief v{event.get('brief_version') or '?'}")
            if message is None:
                lines.append(f"确切指令不可读取：{error}。仅有事件摘要：{summary}")
            else:
                lines.append(f"确切发送指令；SHA-256 `{ref['sha256']}`：\n\n{_literal_block(message)}")
            lines.append("")
        elif source == "executor" and kind in ("executor.started", "executor.item"):
            ref = next((r for r in refs if isinstance(r, dict) and r.get("role") == "executor_message"), None)
            if ref is not None:
                message, error = _verified_exchange_text(run_dir, ref, "executor_message")
                lines.append(f"## [{seq}] Codex 回复 Mentis｜{attempt}")
                if message is None:
                    lines.append(f"公开消息全文不可读取：{error}。事件摘要：{summary}")
                else:
                    lines.append(f"公开消息原文；SHA-256 `{ref['sha256']}`：\n\n{_literal_block(message)}")
                lines.append("")
            elif "最终消息：「" in summary:
                lines += [f"## [{seq}] Codex 回复 Mentis｜{attempt}",
                          "**仅存事件摘要，可能已截断；不能当作完整回复。**",
                          _literal_block(summary), ""]
            else:
                lines.append(f"- [{seq}] Codex 动作：{summary}（事件摘要）")
        elif source == "mentis" and kind in ("evaluation_plan.recorded",
                                             "evaluation_plan.revised"):
            title = "评估计划（委托前落盘）" if kind == "evaluation_plan.recorded" \
                else "评估计划改版"
            lines += ["", f"## [{seq}] Mentis｜{title}", summary]
            ref = next((r for r in refs if isinstance(r, dict) and r.get("role") == "plan"), None)
            if ref is not None:
                lines.append(f"计划文件：`{Path(ref['path']).name}`；"
                             f"SHA-256 `{ref.get('sha256', '')}`")
            lines.append("")
        elif source == "mentis" and kind in ("professional_config.recorded",
                                             "professional_config.revised",
                                             "professional_config.drift",
                                             "professional_config.blocked"):
            # C1：专业配置冻结/改版/漂移/阻断——对话回放按真实顺序可见
            title = {
                "professional_config.recorded": "专业配置（首次使用前冻结）",
                "professional_config.revised": "专业配置改版",
                "professional_config.drift": "专业配置：注册表漂移（继续使用快照）",
                "professional_config.blocked": "专业配置：无法核验（依赖配置的委托不发起）",
            }[kind]
            lines += ["", f"## [{seq}] Mentis｜{title}", summary]
            ref = next((r for r in refs if isinstance(r, dict)
                        and r.get("role") == "professional_config"), None)
            if ref is not None:
                lines.append(f"配置文件：`{Path(ref['path']).name}`；"
                             f"SHA-256 `{ref.get('sha256', '')}`")
            lines.append("")
        elif source == "observer" and kind in ("observation.structure", "observation.render"):
            ref_line = ""
            event_refs = [r for r in refs if isinstance(r, dict) and isinstance(r.get("path"), str)]
            if event_refs:
                r0 = event_refs[0]
                ref_line = (f"（证据 {r0.get('role', '')}=`{Path(r0['path']).name}`，"
                            f"sha256 前 12 位 {str(r0.get('sha256', ''))[:12]}）")
            lines.append(f"- [{seq}] Mentis 观察：{summary}{ref_line}")
        elif source == "mentis" and kind == "observation.diagnosis":
            lines += ["", f"## [{seq}] Mentis｜诊断（模型候选，逐条审查见下）", summary]
            ref = next((r for r in refs if isinstance(r, dict) and r.get("role") == "diagnosis"), None)
            if ref is not None:
                lines.append(f"诊断全文：`{Path(ref['path']).name}`；SHA-256 `{ref.get('sha256', '')}`")
            lines.append("")
        elif source == "mentis" and kind == "decision.routed":
            lines += ["", f"## [{seq}] Mentis｜处置决定", summary, ""]
        elif kind in ("decision.requested", "decision.answered"):
            who = "Mentis 等待用户决定" if kind == "decision.requested" else "用户决定已回答"
            lines += ["", f"## [{seq}] {who}｜{attempt}", summary, ""]
        elif source == "mentis" and kind in ("review.recorded", "executor.confirmed",
                                                "run.resumed", "run.finalized"):
            lines += ["", f"## [{seq}] Mentis｜{kind}", summary, ""]
        elif source == "executor" and kind == "executor.completed":
            lines += ["", f"## [{seq}] Codex 终态", summary, ""]
    return "\n".join(lines).rstrip() + "\n"


def _load_state_verdict(run_dir: Path) -> str:
    """确定性读取同目录状态文件中的运行判定（eval-runner-state / collab-guidance
    state），标注来源与「原判定不改写」；不存在或不可解析时返回空串。"""
    for name in ("runner-state.json", "state.json"):
        path = run_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        verdict = summary.get("verdict")
        if verdict:
            basis = str(summary.get("verdict_basis", ""))[:120]
            return (f"运行判定（来自 {name}，按现存文件确定性读取；原判定不改写）："
                    f"**{verdict}**" + (f"——{basis}" if basis else ""))
    return ""


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


def activity_status(run_dir) -> tuple:
    """活动信号状态（O2）：返回 (类别, 可读说明)。类别用于变化检测（避免刷屏）：
    missing / corrupt / stale（过期）/ old_generation / fresh / unclear。"""
    hb = load_heartbeat(run_dir)
    if hb.get("read") == "missing":
        return "missing", "活动信号未采集：不能从事件静默判断任务是否仍在运行"
    if hb.get("read") == "corrupt":
        return "corrupt", "活动信号损坏：实时状态不可靠（不据此断言失败）"
    producer = hb.get("producer") if isinstance(hb.get("producer"), dict) else {}
    note = f"生产者 {producer.get('role', '?')}(pid {producer.get('pid', '?')})"
    prefix = f"活动信号 {hb.get('time_utc', '?')}（有效期 {hb.get('ttl_s', '?')}s；{note}）"
    generation = current_generation(run_dir)
    hb_generation = hb.get("generation")
    if isinstance(hb_generation, int) and generation and hb_generation != generation:
        return "old_generation", prefix + f"；属于旧代次 {hb_generation}（当前代次 {generation}），不代表当前生产者"
    try:
        delta = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(str(hb.get("time_utc")))).total_seconds()
    except (TypeError, ValueError):
        return "unclear", prefix + "；时刻不可解析，状态不确定"
    if delta < 0:
        return "unclear", prefix + "；时刻异常（晚于读取时钟），状态不确定"
    if delta > float(hb.get("ttl_s", HEARTBEAT_TTL_S)):
        return ("stale", prefix + f"；已过期 {delta:.0f}s——无法确认生产者仍活跃，"
                "状态不确定（不判失败、不解锁、不自动重发）")
    return "fresh", prefix + f"；{delta:.0f}s 前确认活跃"


def describe_activity(run_dir) -> str:
    """一行可读的活动信号摘要（O2）：区分「信号新鲜/过期/损坏/未采集」。
    过期或缺失只表示无法确认生产者活跃，不代表任务失败。"""
    return activity_status(run_dir)[1]


def follow_timeline(run_dir, out=print, poll_s: float = 0.5, stop_check=None):
    """T2 只读旁观：重放已有事件后跟随新增；Ctrl+C 只停止读取，不影响运行。

    终止（O2 代次语义）：当前代次的 run.finalized 且 manifest 已落盘（正常收尾）、
    stop_check 返回 True、或调用者 Ctrl+C。run.resumed 清除当前代次收尾标记——
    旧代次的 run.finalized 与旧 manifest 不再使恢复中的运行被误报完成；恢复后
    事件静默不触发「静默收尾」兜底（该兜底只适用于从未恢复过的运行）。
    半行容错：只消费完整行，末尾半行留待下次。
    """
    run_dir = Path(run_dir)
    path = run_dir / "trace" / "events.jsonl"
    manifest_path = run_dir / "trace" / "manifest.json"
    if not path.is_file():
        out("该目录尚无 trace/ 事件流（可能为历史运行或运行未开始）；以下为现存材料视图：")
        out(render_historical(run_dir))
        return
    out("[旁观中] 只读跟随 trace/events.jsonl；Ctrl+C 仅停止旁观，不影响运行（取消/续接走现有状态契约）。")
    out(f"[当前状态] 代次 {current_generation(run_dir) or '未采集'}；{describe_activity(run_dir)}。")
    offset = 0
    seen_finalized = False   # 当前代次已见收尾（run.resumed 会清除）
    resumed_seen = False     # 曾见恢复：旧 manifest/静默兜底不再适用
    quiet_polls = 0
    last_size = -1
    last_activity_line = ""
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
                        elif event.get("kind") == "run.resumed":
                            # 恢复产生新代次：旧收尾不再代表当前状态（不覆盖新活动）
                            seen_finalized = False
                            resumed_seen = True
                            out(f"[新代次 {current_generation(run_dir)}] 旧代次收尾与活动信号不再作为当前状态依据。")
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
            # manifest 已存在且事件流连续数轮无增长 → 按收尾处理。
            # O2：恢复过的运行不适用本兜底（旧 manifest 不代表当前代次已完成）。
            if manifest_path.is_file() and not resumed_seen:
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
            elif manifest_path.is_file() and resumed_seen and not seen_finalized:
                # 恢复后未收尾：静默只表示暂无新记录；活动信号类别变化时才汇报（避免刷屏）
                if len(data) == last_size:
                    quiet_polls += 1
                else:
                    quiet_polls = 0
                if quiet_polls and quiet_polls % 20 == 0:
                    category, line = activity_status(run_dir)
                    if category != last_activity_line:
                        out(f"[静默 {quiet_polls * poll_s:.0f}s，不代表无活动] {line}")
                        last_activity_line = category
            last_size = len(data)
            if stop_check is not None and stop_check():
                out("[旁观停止] 应调用方要求；运行不受影响。")
                return
            time.sleep(poll_s)
    except KeyboardInterrupt:
        out("\n[旁观已停止] 仅停止读取，不影响运行；取消/续接按现有状态契约处理。")
