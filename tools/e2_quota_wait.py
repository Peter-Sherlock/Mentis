# -*- coding: utf-8 -*-
"""E2 配额恢复探测循环（评价侧控制器工具；不参与受测运行）。

每 interval 秒对下一臂执行真实 eval-isolate probe：
- ok=true  → 配额已恢复、窗口已开，退出码 0（调用方立即续跑该臂）；
- 失败    → fail-closed，窗口不保留；退避后重试，直至 max_attempts。
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARM = sys.argv[1] if len(sys.argv) > 1 else "input-002/B-1"
SPEC = "runtime/evaluation/e2-run-spec-r3.json"
INTERVAL_S = 35 * 60
MAX_ATTEMPTS = 10

for attempt in range(1, MAX_ATTEMPTS + 1):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "mentis", "eval-isolate", "probe",
         "--prepared", "runtime/evaluation/e2-prepared", "--arm", ARM,
         "--run-spec", SPEC],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]
                             if proc.stdout.strip() else "{}")
    except (json.JSONDecodeError, IndexError):
        payload = {"ok": False, "problems": [proc.stdout[-200:] + proc.stderr[-200:]]}
    print(f"[{datetime.now().isoformat(timespec='seconds')}] attempt {attempt} "
          f"ok={payload.get('ok')} codex={payload.get('codex_status')} "
          f"problems={str(payload.get('problems'))[:160]}", flush=True)
    if payload.get("ok"):
        sys.exit(0)
    if attempt < MAX_ATTEMPTS:
        time.sleep(INTERVAL_S)
print("配额探测循环用尽重试次数", flush=True)
sys.exit(1)
