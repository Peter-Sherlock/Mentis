# -*- coding: utf-8 -*-
"""E2 运行台账：扫描 e2-runs 全部运行目录，输出逐臂状态与资源消耗汇总。

只读工具（评价侧）；不参与受测运行。用法：
    py -3.14 -X utf8 tools/e2_ledger.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runtime" / "evaluation" / "e2-runs"

FROZEN_ORDER = [
    ("E1-01", "end_to_end", ["input-001/B-0", "input-001/B-1", "input-001/B-2", "input-001/M"]),
    ("E1-02", "feedback", ["input-002/B-1", "input-002/B-2", "input-002/B-3", "input-002/M"]),
    ("E1-03", "feedback", ["input-003/B-1", "input-003/B-2", "input-003/B-3", "input-003/M"]),
    ("E1-04", "decision", ["input-004/B-1", "input-004/B-2", "input-004/B-3", "input-004/M"]),
    ("E1-05", "decision", ["input-005/B-1", "input-005/B-2", "input-005/B-3", "input-005/M"]),
]


def main() -> int:
    rows = []
    totals = {"probes": 0, "delegations": 0, "model_calls": 0, "http": 0}
    for sample, kind, arms in FROZEN_ORDER:
        for arm in arms:
            runs = sorted(RUNS.glob(f"*-{arm.replace('/', '-')}")) if RUNS.is_dir() else []
            entries = []
            for rd in runs:
                mp = rd / "run-manifest.json"
                if not mp.is_file():
                    entries.append({"run_id": rd.name, "note": "manifest 缺失"})
                    continue
                d = json.loads(mp.read_text(encoding="utf-8"))
                s = d.get("summary", {})
                entries.append({
                    "run_id": d["run_id"],
                    "exec": d.get("execution_status"),
                    "runner": d.get("runner_status"),
                    "verdict": s.get("verdict"),
                    "attempts": d.get("budget", {}).get("used", {}).get("attempts"),
                    "model_calls": d.get("budget", {}).get("used", {}).get("model_calls"),
                    "http": d.get("model_requests", {}).get("http_attempts"),
                    "conts": d.get("checked_continuations"),
                    "artifact": d.get("artifact", {}).get("exists"),
                    "sha12": (d.get("artifact", {}).get("sha256") or "")[:12],
                    "binding": (d.get("execution_binding", {}) or {}).get("code_commit", "")[:7],
                })
            rows.append((sample, kind, arm, entries))
    for sample, kind, arm, entries in rows:
        tag = "（无运行）" if not entries else ""
        print(f"{sample} {kind:9s} {arm:16s}{tag}")
        for e in entries:
            if "note" in e:
                print(f"    {e['run_id']}: {e['note']}")
                continue
            print(f"    {e['run_id']}  exec={e['exec']} runner={e['runner']} "
                  f"verdict={e['verdict']} 尝试={e['attempts']} 模型={e['model_calls']}"
                  f"/http={e['http']} 续办={e['conts']} 产物={e['artifact']}:{e['sha12']} "
                  f"commit={e['binding']}")
            totals["delegations"] += int(e["attempts"] or 0)
            totals["model_calls"] += int(e["model_calls"] or 0)
            totals["http"] += int(e["http"] or 0)
    print(f"\n委托合计={totals['delegations']} 模型单位合计={totals['model_calls']} "
          f"HTTP尝试合计={totals['http']}（探测次数按 probe 目录另计："
          f"{len(list((ROOT / 'runtime/evaluation/e2-prepared/isolation').glob('probe-*')))} 个探测目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
