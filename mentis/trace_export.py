"""P2.4-T3 导出白名单（契约 mentis-trace/1 §6 导出层）。

只导出字段白名单处理后的时间线与文件哈希：
- 排除：用户逐字反馈、工作区全文、prompt 原文（只出哈希）、原生原始行、已知密钥字段值；
- 事件/manifest 仅保留白名单字段（含未知附加字段的防御性剔除）；
- 旧运行（无 trace/）导出为历史材料视图 + unrecorded 缺口清单，不回填、不生成事件。
敏感字段规则见 mentis.trace.redact_text：只对已知字段名确定性隐去，不承诺识别任意秘密。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from mentis.trace import CONTRACT_VERSION, load_events, redact_text, render_historical

EXPORT_FORMAT = "mentis-trace-export/1"

EVENT_FIELDS = ("schema_version", "run_id", "seq", "time_utc", "source", "kind",
                "attempt_id", "brief_version", "native_thread_id", "status",
                "summary", "refs", "executor_status", "usage")
MANIFEST_FIELDS = ("contract_version", "run_id", "created_at_utc", "updated_at_utc",
                   "mentis_version", "executor", "outcome")
DELEGATION_FIELDS = ("attempt_id", "brief_version", "native_thread_id", "action",
                     "prompt_sha256", "terminal_status", "usage", "error")
INTEGRITY_FIELDS = ("events_written", "seq_max", "events_complete", "write_failures", "note")

EXPORT_NOTE = ("导出仅含白名单字段与文件哈希；敏感字段按已知字段名规则隐去（不承诺识别任意秘密）；"
               "不含用户逐字反馈、工作区全文、prompt 原文与执行器原生原始行。")


def _whitelist(obj: dict, fields) -> dict:
    return {key: obj[key] for key in fields if key in obj}


def _export_ref(ref: dict) -> dict:
    """引用只保留角色、文件名与哈希；不含本地完整路径。"""
    out = {}
    if isinstance(ref.get("role"), str):
        out["role"] = ref["role"]
    path = ref.get("path")
    if isinstance(path, str) and path:
        out["file"] = Path(path).name
    if isinstance(ref.get("sha256"), str):
        out["sha256"] = ref["sha256"]
    return out


def _export_event(event: dict) -> dict:
    out = _whitelist(event, EVENT_FIELDS)
    if isinstance(out.get("summary"), str):
        out["summary"] = redact_text(out["summary"])
    if isinstance(out.get("refs"), list):
        out["refs"] = [_export_ref(r) for r in out["refs"] if isinstance(r, dict)]
    if isinstance(out.get("usage"), dict):
        out["usage"] = {k: v for k, v in out["usage"].items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return out


def _export_manifest(manifest: dict) -> dict:
    out = _whitelist(manifest if isinstance(manifest, dict) else {}, MANIFEST_FIELDS)
    delegations = manifest.get("delegations")
    if isinstance(delegations, list):
        cleaned = []
        for d in delegations:
            if not isinstance(d, dict):
                continue
            entry = _whitelist(d, DELEGATION_FIELDS)
            if isinstance(entry.get("error"), str):
                entry["error"] = redact_text(entry["error"])
            cleaned.append(entry)
        out["delegations"] = cleaned
    integrity = manifest.get("integrity")
    if isinstance(integrity, dict):
        clean_integrity = _whitelist(integrity, INTEGRITY_FIELDS)
        if isinstance(clean_integrity.get("note"), str):
            clean_integrity["note"] = redact_text(clean_integrity["note"])
        out["integrity"] = clean_integrity
    return out


def export_timeline(run_dir, out_path=None) -> dict:
    """导出一次运行的可共享时间线。返回 {"path", "mode", "events"}。

    有 trace/ 的新运行导出白名单事件与 manifest；历史运行导出现存材料视图与缺口
    （契约 §9：不回填）。导出文件只读运行目录，不产生任何 trace/ 事件。
    """
    run_dir = Path(run_dir)
    events_path = run_dir / "trace" / "events.jsonl"
    manifest_path = run_dir / "trace" / "manifest.json"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if events_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                manifest = {}
        except (OSError, json.JSONDecodeError):
            manifest = {}
        payload = {
            "format": EXPORT_FORMAT,
            "exported_at_utc": stamp,
            "mode": "trace",
            "contract_version": CONTRACT_VERSION,
            "manifest": _export_manifest(manifest),
            "events": [_export_event(e) for e in load_events(run_dir)],
            "note": EXPORT_NOTE,
        }
        count = len(payload["events"])
    else:
        payload = {
            "format": EXPORT_FORMAT,
            "exported_at_utc": stamp,
            "mode": "historical",
            "compatibility_note": ("旧记录兼容：该运行早于追踪契约，无 trace/ 事件流；"
                                   "仅导出现存材料清单与未记录（unrecorded）缺口，"
                                   "不回填、不生成伪事件。"),
            "historical_view_md": render_historical(run_dir),
        }
        count = 0
    out_path = Path(out_path) if out_path else Path("trace-export.json")
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": out_path, "mode": payload["mode"], "events": count}
