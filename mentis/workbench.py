"""P3.4 本地工作台：浏览器内发起任务、跟进度、答复待决、反馈/改目标、核对与撤回。

只做进程编排与读取投影：所有任务动作转发给真实 `collab` / `collab-resume` CLI
子进程，前端不复制权限、验收或恢复规则；任务记录全部落运行目录（关页面不丢）。
刷新只读状态，不会重复发起任务（发起仅经显式 POST，且单用户同时至多一个构建）。
"""

import hashlib
import html
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mentis.run_projection import load_run_state

PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
# 作品预览：iframe sandbox（无 allow-same-origin）隔离来源；沙箱内允许内联样式/脚本/表单
PREVIEW_CSP = "sandbox allow-scripts allow-forms; object-src 'none'; base-uri 'none'"
EVIDENCE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; object-src 'none'; base-uri 'none'"
EVIDENCE_FILES = ("report.md", "conversation.md", "state.json", "session.json",
                  "trace/events.jsonl", "trace/model-calls.jsonl", "trace/stages.jsonl")
ACTION_KINDS = ("answer", "feedback", "goal", "confirm", "revoke")


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _sha16(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _pending_decisions(session: dict) -> list:
    rows = []
    for ix in session.get("interactions", []):
        if ix.get("kind") == "request_decision" and not ix.get("answer_source"):
            rows.append({"interaction_id": ix.get("interaction_id"),
                         "blocking_action": ix.get("blocking_action"),
                         "message": ix.get("message", ""),
                         "created_at": ix.get("created_at", ""),
                         "brief_version": ix.get("brief_version")})
    return rows


def _active_authorizations(session: dict) -> int:
    return sum(1 for a in session.get("authorizations", [])
               if a.get("status") == "active")


def _attempts(session: dict) -> list:
    return [{"attempt_id": a.get("attempt_id"), "action": a.get("action_key"),
             "status": a.get("status"), "accepted": a.get("accepted", False),
             "brief_version": a.get("brief_version")}
            for a in session.get("attempts", [])]


def _artifact_info(run_dir: Path) -> dict:
    index = run_dir / "workspace" / "index.html"
    if not index.is_file():
        return {"exists": False}
    stat = index.stat()
    return {"exists": True, "sha256_16": _sha16(index), "bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")}


def _versions(run_dir: Path) -> list:
    out = []
    vdir = run_dir / "versions"
    if vdir.is_dir():
        for p in sorted(vdir.iterdir()):
            if p.is_file() and p.suffix == ".html":
                out.append({"name": p.name, "sha256_16": _sha16(p),
                            "bytes": p.stat().st_size})
    return out


class Workbench:
    """进程编排器：持有本服务启动的子进程与逐运行动作锁；不持有任务状态。"""

    def __init__(self, runs_dir, python_flags=None, spawner=None, start_wait_s=30):
        self.runs_dir = Path(runs_dir).resolve()
        self.py = list(python_flags or [sys.executable, "-X", "utf8", "-B"])
        self.spawner = spawner or self._spawn
        self.repo_root = self.runs_dir.parents[1]  # <root>/runtime/collab → <root>
        self.start_wait_s = start_wait_s
        self._mu = threading.Lock()
        self._build_proc = None          # 至多一个进行中的构建（单用户）
        self._build_log = None
        self._actions = {}               # run_id -> {"kind","proc","log","started"}
        self._last_action = {}           # run_id -> {"kind","exit_code","finished_at"}

    @staticmethod
    def _close(log):
        if log is not None and not log.closed:
            try:
                log.close()
            except OSError:
                pass

    def _spawn(self, args, cwd, env, stdout):
        return subprocess.Popen(args, cwd=cwd, env=env, stdout=stdout,
                                stderr=subprocess.STDOUT)

    def _action_env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("CODEX_HOME", str(self.runs_dir.parent / "codex-home"))
        return env

    def _log_dir(self) -> Path:
        log_dir = self.runs_dir.parent / "observability" / "p34" / "workbench-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    # ---- 任务发起 ----

    def start_task(self, goal: str, timeout_s: int = 600) -> dict:
        goal = (goal or "").strip()
        if not goal:
            return {"error": "目标不能为空", "code": 400}
        timeout_s = max(60, min(int(timeout_s), 600))
        with self._mu:
            if self._build_proc is not None and self._build_proc.poll() is None:
                return {"error": "已有一个进行中的任务（单用户同时至多一个构建）；"
                                 "请等它结束或在已有任务上继续", "code": 409}
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            before = {p.name for p in self.runs_dir.iterdir() if p.is_dir()}
            log_path = self._log_dir() / f"build-{datetime.now().strftime('%H%M%S')}.log"
            self._close(self._build_log)
            log = log_path.open("wb")
            proc = self.spawner(
                [*self.py, "-m", "mentis", "collab", "--goal", goal,
                 "--non-interactive", "--timeout", str(timeout_s), "--max-repairs", "1"],
                cwd=str(self.repo_root), env=self._action_env(), stdout=log)
            self._build_proc = proc
        run_id = None
        deadline = time.monotonic() + self.start_wait_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._close(self._build_log)  # 提前退出（如凭据缺失）；错误在日志与任务列表如实可见
                break
            after = {p.name for p in self.runs_dir.iterdir() if p.is_dir()}
            fresh = sorted(after - before)
            if fresh:
                run_id = fresh[-1]
                break
            time.sleep(0.5)
        return {"run_id": run_id, "starting": run_id is None,
                "pid": proc.pid, "log": str(log_path)}

    def build_running(self) -> bool:
        with self._mu:
            return self._build_proc is not None and self._build_proc.poll() is None

    # ---- 任务动作（逐运行串行；异步执行，进度在任务详情可见） ----

    def run_action(self, run_id: str, kind: str, payload: dict) -> dict:
        if kind not in ACTION_KINDS:
            return {"error": f"未知动作 {kind}", "code": 400}
        run_dir = self.runs_dir / run_id
        state = run_dir / "state.json"
        if not state.is_file():
            return {"error": "运行不存在或还没有可恢复状态", "code": 404}
        args = ["collab-resume", "--state", str(state.resolve()), "--non-interactive"]
        if kind == "answer":
            ix = (payload.get("interaction_id") or "").strip()
            text = (payload.get("text") or "").strip()
            if not ix or not text:
                return {"error": "答复需要请求 ID 与回答文本", "code": 400}
            args += ["--answer", f"{ix}:{text}"]
        elif kind == "feedback":
            text = (payload.get("text") or "").strip()
            if not text:
                return {"error": "反馈不能为空", "code": 400}
            args += ["--feedback", text]
        elif kind == "goal":
            text = (payload.get("text") or "").strip()
            if not text:
                return {"error": "新目标不能为空", "code": 400}
            args += ["--goal-new", text]
        elif kind == "confirm":
            outcome = payload.get("outcome")
            if outcome not in ("completed", "failed"):
                return {"error": "核对结论只能是 completed 或 failed", "code": 400}
            args += ["--confirm-outcome", outcome]
        elif kind == "revoke":
            args += ["--revoke"]
        # 验收修复：在途任务拒绝一切 resume 动作——与进行中的构建/动作进程
        # 并发写同一运行状态是真实竞态；停止走「请求停止」，不走绕行
        run_state = _load_run_state_safe(self.runs_dir / run_id)
        if self._in_flight(run_id, run_state):
            return {"error": "任务仍在进行（活动信号 fresh 或构建/动作子进程存活）；"
                             "进行中的执行器轮次无法中断，如需停止请先「请求停止」，"
                             "结束后再核对或操作", "code": 409}
        with self._mu:
            running = self._actions.get(run_id)
            if running is not None and running["proc"].poll() is None:
                return {"error": f"该任务上一个动作（{running['kind']}）仍在进行，"
                                 "请等它结束后再操作", "code": 409}
            if running is not None:
                self._close(running["log"])
            log_path = self._log_dir() / \
                f"action-{run_id}-{kind}-{datetime.now().strftime('%H%M%S')}.log"
            log = log_path.open("wb")
            proc = self.spawner([*self.py, "-m", "mentis", *args],
                                cwd=str(self.repo_root), env=self._action_env(),
                                stdout=log)
            self._actions[run_id] = {"kind": kind, "proc": proc, "log": log,
                                     "started": datetime.now().isoformat(timespec="seconds")}
        return {"accepted": True, "kind": kind, "pid": proc.pid,
                "note": "动作已提交并在后台执行；进度以任务详情为准"}

    def _action_view(self, run_id: str) -> dict:
        with self._mu:
            running = self._actions.get(run_id)
            view = {}
            if running is not None:
                alive = running["proc"].poll() is None
                view["action_running"] = ({"kind": running["kind"],
                                           "started": running["started"]} if alive else None)
                if not alive:
                    self._close(running["log"])
                    self._last_action[run_id] = {
                        "kind": running["kind"],
                        "exit_code": running["proc"].returncode,
                        "finished_at": datetime.now().isoformat(timespec="seconds")}
            last = self._last_action.get(run_id)
            if last:
                view["last_action"] = last
            return view

    # ---- 在途识别与停止请求（验收修复：P3.4 审计 #3） ----
    #
    # 在途 = 本服务持有的构建子进程仍活着，或活动信号 fresh 而本服务没有
    # 存活的动作子进程。后者覆盖服务重启后的孤儿构建/CLI 构建识别——
    # 注册表随进程消失，心跳不随进程消失。
    # 停止请求 ≠ 已确认停止：请求只落本服务自己的标记目录（不写运行目录，
    # 不违反单写入），进行中的执行器轮次无法中断；已确认停止仍是 collab
    # 自身的 WITHDRAWN 终态（idle 时撤回授权才产生）。

    def _state_dir(self) -> Path:
        d = self.runs_dir.parent / "workbench"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _stop_marker(self, run_id: str) -> Path:
        return self._state_dir() / f"{run_id}.stop-request.json"

    def request_stop(self, run_id: str) -> dict:
        run_dir = self.runs_dir / run_id
        if not (run_dir / "state.json").is_file():
            return {"error": "运行不存在", "code": 404}
        run_state = _load_run_state_safe(run_dir)
        in_flight = self._in_flight(run_id, run_state)
        if not in_flight:
            return {"stop_requested": False,
                    "note": "任务不在进行中：无需停止请求；如要终止后续委托请使用撤回授权（已确认停止）",
                    "code": 409}
        marker = self._stop_marker(run_id)
        marker.write_text(json.dumps(
            {"requested_at": datetime.now().isoformat(timespec="seconds"),
             "note": "停止已请求：进行中的执行器轮次无法中断；进行中动作被拒绝，"
                     "结束后由用户核对或撤回"},
            ensure_ascii=False), encoding="utf-8")
        return {"stop_requested": True, "in_flight": True,
                "note": "停止已请求：进行中的执行器轮次无法中断；结束后请核对产物或撤回授权"}

    def _stop_requested(self, run_id: str) -> bool:
        return self._stop_marker(run_id).is_file()

    def _in_flight(self, run_id: str, run_state: dict | None = None) -> bool:
        with self._mu:
            running = self._actions.get(run_id)
            action_alive = running is not None and running["proc"].poll() is None
            build_here = (self._build_proc is not None
                          and self._build_proc.poll() is None
                          and run_id == self.current_build_run())
        if build_here or action_alive:
            return True
        if run_state is None:
            run_state = _load_run_state_safe(self.runs_dir / run_id)
        activity = (run_state.get("activity") or {}).get("category")
        return activity == "fresh"  # 有本服务不知道的生产者活着（重启前的孤儿/CLI 构建）

    # ---- 投影（只读） ----

    def list_tasks(self) -> list:
        if not self.runs_dir.is_dir():
            return []
        rows = []
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            if not d.is_dir() or not (d / "state.json").is_file():
                continue
            meta = _load_json(d / "state.json", {})
            session = _load_json(d / "session.json", {})
            run_state = _load_run_state_safe(d)
            rows.append({
                "run_id": d.name,
                "goal": (meta.get("goal") or "")[:80],
                "status": meta.get("status", ""),
                "delivered": bool(meta.get("delivered")),
                "created_at": meta.get("created_at") or d.name.split("-")[0],
                "open_waits": len(run_state.get("open_waits", [])),
                "activity_category": (run_state.get("activity") or {}).get("category", ""),
                "artifact": bool(_artifact_info(d).get("exists")),
            })
        return rows

    def task_detail(self, run_id: str) -> dict:
        run_dir = self.runs_dir / run_id
        if not (run_dir / "state.json").is_file():
            return None
        meta = _load_json(run_dir / "state.json", {})
        session = _load_json(run_dir / "session.json", {})
        run_state = _load_run_state_safe(run_dir)
        pending = _pending_decisions(session)
        attempts = _attempts(session)
        detail = {
            "run_id": run_id,
            "goal": meta.get("goal", ""),
            "status": meta.get("status", ""),
            "delivered": bool(meta.get("delivered")),
            "mode": meta.get("mode", ""),
            "timeout_s": meta.get("timeout_s"),
            "max_repairs": meta.get("max_repairs"),
            "repairs_used": meta.get("repairs_used"),
            "thread_bound": bool(meta.get("thread_id")),
            "task_db": run_state.get("task_db"),
            "activity": run_state.get("activity"),
            "current_stage": run_state.get("current_stage"),
            "stages": run_state.get("stages", []),
            "open_waits": run_state.get("open_waits", []),
            "pending": pending,
            "attempts": attempts,
            "authorizations_active": _active_authorizations(session),
            "artifact": _artifact_info(run_dir),
            "versions": _versions(run_dir),
            "screenshots": meta.get("screenshots", []),
            "model_usage": meta.get("model_usage", {}),
            "codex_runs": meta.get("codex_runs", []),
            "evaluation_plan": meta.get("evaluation_plan", {}),
            "notes": run_state.get("notes", []),
            # 服务端计算的可用动作与状态区分（前端只渲染，不复制规则）
            "can_answer": bool(pending),
            "can_confirm": any(a.get("status") in ("unknown", "failed")
                               for a in attempts),
            "can_revoke": (_active_authorizations(session) > 0
                           and not self._in_flight(run_id, run_state)),
            # 三状态区分（P3.4 审计 #3）：授权撤回 / 停止请求 / 已确认停止
            "authorizations_revoked": (_active_authorizations(session) == 0
                                       and any(a.get("status") == "revoked"
                                               for a in session.get("authorizations", []))),
            "stop_requested": self._stop_requested(run_id),
            "confirmed_stop": meta.get("status") == "WITHDRAWN",
            "in_flight": self._in_flight(run_id, run_state),
            "build_running": self.build_running() and run_id == self.current_build_run(),
        }
        detail.update(self._action_view(run_id))
        return detail

    def current_build_run(self):
        with self._mu:
            if self._build_proc is None or self._build_proc.poll() is not None:
                return None
            if not self.runs_dir.is_dir():
                return None
            dirs = [d for d in self.runs_dir.iterdir() if d.is_dir()]
            return max(dirs, key=lambda d: d.name).name if dirs else None


def _load_run_state_safe(run_dir: Path) -> dict:
    try:
        return load_run_state(run_dir)
    except Exception:
        return {}


# ---- 页面 ----

CSS = """
:root { --bg:#161311; --card:#221d19; --line:#3a322b; --ink:#efe9e2; --dim:#a99f93;
        --accent:#d98b40; --ok:#7fb069; --warn:#d9a03f; --bad:#c96a5a; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif; }
.topbar { display:flex; align-items:baseline; gap:12px; padding:14px 22px;
          border-bottom:1px solid var(--line); }
.logo { background:var(--accent); color:#1c1712; font-weight:700; border-radius:6px;
        padding:2px 9px; }
.brand { font-size:18px; font-weight:600; }
.subtitle { color:var(--dim); font-size:13px; }
.wrap { display:grid; grid-template-columns:340px 1fr; gap:18px; padding:18px 22px; }
aside#tasks { border:1px solid var(--line); border-radius:10px; background:var(--card);
              padding:14px; align-self:start; max-height:calc(100vh - 100px);
              overflow:auto; }
.side-head { display:flex; justify-content:space-between; align-items:center;
             font-weight:600; margin-bottom:10px; }
#task-list .task { border:1px solid var(--line); border-radius:8px; padding:8px 10px;
                   margin-bottom:8px; cursor:pointer; }
#task-list .task.sel { border-color:var(--accent); }
#task-list .task .g { font-size:13px; }
#task-list .task .m { color:var(--dim); font-size:12px; display:flex; gap:8px; flex-wrap:wrap; }
.badge { display:inline-block; padding:0 8px; border-radius:10px; font-size:12px;
         border:1px solid var(--line); }
.badge.DELIVERED { color:var(--ok); } .badge.UNKNOWN { color:var(--warn); }
.badge.ERROR, .badge.NOT_DELIVERED, .badge.FAILED { color:var(--bad); }
.badge.PENDING, .badge.WITHDRAWN { color:var(--warn); }
#start-form { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
#start-form h3 { margin:0 0 8px; font-size:14px; }
textarea, input[type=number], input[type=text] { width:100%; background:#14100d;
    color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:7px 9px;
    font:inherit; }
button { background:var(--accent); color:#1c1712; font-weight:600; border:0;
         border-radius:6px; padding:7px 14px; cursor:pointer; margin-top:8px; }
button:disabled { opacity:.45; cursor:not-allowed; }
button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); }
button.danger { background:transparent; color:var(--bad); border:1px solid var(--bad); }
.hint { color:var(--dim); font-size:12px; }
main#detail { border:1px solid var(--line); border-radius:10px; background:var(--card);
              padding:16px 20px; max-height:calc(100vh - 100px); overflow:auto; }
section { margin-bottom:20px; }
section h3 { margin:0 0 8px; font-size:14px; color:var(--accent); }
.kv { display:flex; gap:10px; flex-wrap:wrap; font-size:13px; color:var(--dim); }
.kv b { color:var(--ink); font-weight:600; }
.pending { border:1px solid var(--warn); border-radius:8px; padding:10px 12px;
           margin-bottom:10px; }
.pending .msg { font-size:13px; margin-bottom:6px; }
.pending .meta { color:var(--dim); font-size:12px; }
.stage { font-size:13px; padding:3px 0; border-bottom:1px dashed var(--line); }
.stage .s { display:inline-block; width:76px; color:var(--dim); }
iframe.preview { width:100%; height:420px; border:1px solid var(--line); border-radius:8px;
                 background:#fff; }
.compare { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
select { background:#14100d; color:var(--ink); border:1px solid var(--line);
         border-radius:6px; padding:5px 8px; font:inherit; max-width:100%; }
.links a { color:var(--accent); margin-right:14px; font-size:13px; }
.notice { border:1px solid var(--bad); border-radius:8px; padding:10px 12px;
          font-size:13px; margin-bottom:10px; }
.actionrow { display:flex; gap:8px; align-items:flex-start; margin:6px 0; }
.actionrow textarea { flex:1; }
code.path { color:var(--dim); font-size:12px; word-break:break-all; }
"""

JS = r"""
const $ = (id) => document.getElementById(id);
let selected = null;
let lastFingerprint = '';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, code: res.status, body };
}

async function post(path, payload) {
  return api(path, { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}) });
}

async function refreshTasks(keepSelection) {
  const { body } = await api('/api/tasks');
  const list = $('task-list');
  list.innerHTML = '';
  for (const t of (body.tasks || [])) {
    const div = document.createElement('div');
    div.className = 'task' + (t.run_id === selected ? ' sel' : '');
    div.innerHTML = '<div class="g">' + (t.goal || t.run_id) + '</div>' +
      '<div class="m">' + badge(t.status) +
      (t.open_waits ? '<span>待决 ' + t.open_waits + '</span>' : '') +
      (t.artifact ? '<span>有作品</span>' : '') +
      '<span>' + (t.created_at || '') + '</span></div>';
    div.onclick = () => { selected = t.run_id; lastFingerprint = ''; refreshTasks(true); pollDetail(true); };
    list.appendChild(div);
  }
  if (body.build_running) $('start').disabled = true;
  else $('start').disabled = false;
}

function badge(status) {
  return '<span class="badge ' + (status||'') + '">' + (status||'未知') + '</span>';
}

function disableActionButtons(disabled) {
  for (const id of ['answer-btn', 'feedback-btn', 'goal-btn', 'confirm-btn', 'revoke-btn']) {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  }
}

// 每次轮询默认重绘 #detail；三重保护避免打断正在操作的用户：
// 1) 数据指纹未变不重绘；2) 焦点在面板输入框内不重绘；3) 重绘保留输入值与焦点。
function fingerprint(d) {
  return JSON.stringify([d.status, d.delivered, d.action_running, d.last_action,
    (d.pending || []).map(p => p.interaction_id), d.artifact,
    (d.attempts || []).map(a => [a.action, a.status, a.accepted]),
    (d.versions || []).map(v => v.name), d.can_answer, d.can_confirm, d.can_revoke,
    (d.activity || {}).category, (d.stages || []).length,
    d.stop_requested, d.in_flight, d.confirmed_stop, d.authorizations_revoked,
    d.authorizations_active]);
}

function preserveInputs() {
  const saved = { values: {}, focus: null };
  const detail = $('detail');
  for (const el of detail.querySelectorAll('input, textarea, select')) {
    if (el.id) saved.values[el.id] = el.value;
  }
  const active = document.activeElement;
  if (detail.contains(active) && active.id) saved.focus = active.id;
  return saved;
}

async function pollDetail(force) {
  if (!selected) return;
  const { body } = await api('/api/task/' + selected);
  if (body.error) { $('detail').innerHTML = '<p class="hint">' + body.error + '</p>'; return; }
  const d = body;
  const fp = fingerprint(d);
  const active = document.activeElement;
  const typing = $('detail').contains(active) &&
                 ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
  if (!force && fp === lastFingerprint) return;
  if (!force && typing) return;
  lastFingerprint = fp;
  const saved = preserveInputs();
  renderDetail(d);
  restoreInputs(saved);
}

function renderDetail(d) {
  let h = '';
  h += '<section><h3>任务</h3><div class="kv">' + badge(d.status) +
    (d.delivered ? '<span class="badge DELIVERED">已交付（初稿可看）</span>' : '') +
    '<span>目标：<b>' + esc(d.goal) + '</b></span></div>' +
    '<div class="kv" style="margin-top:6px">' +
    '<span>修复预算 <b>' + (d.repairs_used ?? 0) + '/' + (d.max_repairs ?? '-') + '</b></span>' +
    '<span>任务库 <b>' + esc(d.task_db && d.task_db.present ?
      ('state.sqlite3 v' + d.task_db.db_version + ' · 摘要 ' + d.task_db.state_digest) : '未创建') + '</b></span>' +
    '<span>模型调用 <b>' + ((d.model_usage && d.model_usage.real_calls) || 0) + '</b>（Mock ' +
    ((d.model_usage && d.model_usage.mock_calls) || 0) + '）</span>' +
    '<span>活动信号 <b>' + esc((d.activity && d.activity.category) || '') + '</b></span></div>' +
    '<div class="hint">' + esc((d.activity && d.activity.line) || '') + '</div>' +
    '<div class="kv" style="margin-top:4px">' +
    '<span>授权：<b>' + (d.authorizations_active ? '有效 ' + d.authorizations_active + ' 项' :
      (d.authorizations_revoked ? '已全部撤回' : '无记录')) + '</b></span>' +
    '<span>停止请求：<b>' + (d.stop_requested ? '已请求（进行中的轮次无法中断）' : '无') + '</b></span>' +
    '<span>已确认停止：<b>' + (d.confirmed_stop ? '是（WITHDRAWN，任何新委托被拒）' : '否') + '</b></span>' +
    '<span>任务进行中：<b>' + (d.in_flight ? '是（动作暂不可用）' : '否') + '</b></span></div></section>';

  if (d.in_flight) {
    h += '<div class="notice">任务进行中：进行中的执行器轮次无法中断，' +
      '反馈/改目标/核对/撤回等动作暂不可用（服务重启后依活动信号同样识别）。' +
      (d.stop_requested
        ? '已请求停止：当前轮次结束后请人工核对产物，再决定撤回授权（已确认停止）。'
        : '如需停止请点「请求停止」：') +
      '</div>' +
      (d.stop_requested ? '' :
        '<p><button id="stop-btn" class="ghost">请求停止</button>' +
        '<span class="hint"> 只标记停止意愿，不中断进行中的轮次，可逆。</span></p>');
  }

  if (d.status === 'ERROR' || d.status === 'UNKNOWN') {
    h += '<div class="notice">运行未正常收尾（' + d.status + '）。已保存的作品与最后可靠状态仍在下方；' +
      '恢复步骤见 docs/RECOVERY_GUIDE.md。若执行器结局已人工核对，可用"核对确认"收尾。</div>';
  }
  if (d.action_running) {
    h += '<div class="notice">动作进行中：' + esc(d.action_running.kind) +
      '（' + d.action_running.started + '）——完成后状态自动更新。</div>';
  } else if (d.last_action) {
    h += '<p class="hint">上次动作：' + esc(d.last_action.kind) + '，退出码 ' +
      d.last_action.exit_code + '（' + d.last_action.finished_at + '）</p>';
  }

  h += '<section><h3>待决请求（沉默不是批准；回答只解除依赖动作）</h3>';
  if (!(d.pending || []).length) h += '<p class="hint">当前没有等待你的请求。</p>';
  for (const p of (d.pending || [])) {
    h += '<div class="pending"><div class="msg">' + esc(p.message) + '</div>' +
      '<div class="meta">' + esc(p.interaction_id) + ' · 阻塞动作 ' +
      esc(p.blocking_action) + ' · brief v' + (p.brief_version ?? '?') + '</div>' +
      '<div class="actionrow"><input type="text" id="ans-' + esc(p.interaction_id) +
      '" placeholder="你的回答"><button id="answer-btn">提交答复</button></div></div>';
  }
  h += '</section><section><h3>继续任务</h3>' +
    '<div class="actionrow"><textarea id="feedback" rows="2" ' +
    'placeholder="反馈：哪里要改（进入新 brief 版本并续接修订；保留未提及部分）"></textarea>' +
    '<button id="feedback-btn">按反馈修订</button></div>' +
    '<div class="actionrow"><textarea id="goalnew" rows="2" ' +
    'placeholder="改目标：旧版本结果不再适用，按新目标重新委托"></textarea>' +
    '<button id="goal-btn">改目标</button></div>' +
    '<div class="actionrow"><select id="confirm-outcome">' +
    '<option value="completed">completed（产物已核对可用）</option>' +
    '<option value="failed">failed（核对确认失败）</option></select>' +
    '<button id="confirm-btn">核对确认</button>' +
    '<span class="hint">仅当执行器结局已由你核对；unknown 不会自动重发。</span></div>' +
    '<div class="actionrow"><button id="revoke-btn" class="danger">撤回授权</button>' +
    '<span class="hint">仅任务不在进行中时可用；撤回 = 已确认停止（WITHDRAWN），' +
    '之后任何新委托都会被拒绝且不可逆；已产出文件保留待核对。进行中请先用「请求停止」。</span></div></section>';

  h += '<section><h3>作品（看结果）</h3>';
  if (d.artifact && d.artifact.exists) {
    h += '<div class="kv"><span>index.html <b>' + d.artifact.sha256_16 + '</b> · ' +
      d.artifact.bytes + ' 字节 · ' + d.artifact.mtime + '</span>' +
      '<a href="/preview/' + d.run_id + '/index.html" target="_blank">新窗口打开</a></div>' +
      '<iframe class="preview" sandbox="allow-scripts allow-forms" ' +
      'src="/preview/' + d.run_id + '/index.html"></iframe>';
  } else {
    h += '<p class="hint">还没有可预览的作品（委托可能未完成或被中断）。</p>';
  }
  h += '</section><section><h3>版本对比（看变化）</h3>';
  if ((d.versions || []).length) {
    // A/B 选项统一用相对 workspace 的子路径：当前产物 + 历史快照
    const opts = ['<option value="index.html">index.html（当前 · ' +
      (d.artifact && d.artifact.sha256_16 || '') + '）</option>'].concat(
      d.versions.map(v => '<option value="versions/' + esc(v.name) + '">' +
        esc(v.name) + '（' + v.sha256_16 + '）</option>')).join('');
    h += '<div class="compare">' +
      '<div><select id="ver-a">' + opts + '</select><iframe class="preview" id="frame-a" ' +
      'sandbox="allow-scripts allow-forms"></iframe></div>' +
      '<div><select id="ver-b">' + opts + '</select><iframe class="preview" id="frame-b" ' +
      'sandbox="allow-scripts allow-forms"></iframe></div></div>';
  } else {
    h += '<p class="hint">没有历史版本快照（首次交付前不产生对比）。</p>';
  }
  h += '</section><section><h3>证据（看依据，按需打开）</h3><div class="links">' +
    '<a href="/evidence/' + d.run_id + '/report.md" target="_blank">运行报告</a>' +
    '<a href="/evidence/' + d.run_id + '/conversation.md" target="_blank">委托与执行器公开交流</a>' +
    '<a href="/evidence/' + d.run_id + '/trace/events.jsonl" target="_blank">事件流</a>' +
    '<a href="/evidence/' + d.run_id + '/trace/model-calls.jsonl" target="_blank">模型调用台账</a>' +
    '<a href="/evidence/' + d.run_id + '/state.json" target="_blank">状态导出</a>' +
    '</div><div class="kv" style="margin-top:8px"><span>截图：</span>' +
    (d.screenshots || []).map(s => '<a href="/screenshot/' + d.run_id + '/' +
      esc(s.split(/[\\/]/).pop()) + '" target="_blank">' + esc(s.split(/[\\/]/).pop()) + '</a>').join('') +
    '</div>' +
    '<p class="hint">深度时间线/决策关联：另开 <code>python -m mentis view --run ' +
    esc(d.run_id) + '</code>（只读）。</p></section>';

  h += '<section><h3>阶段</h3>';
  for (const s of (d.stages || []).slice(-12)) {
    h += '<div class="stage"><span class="s">' + esc(s.phase || '') + ' ' +
      esc(s.name || '') + '</span>' + esc(s.status || '') + ' · ' + esc(s.detail || '') + '</div>';
  }
  h += '</section>';

  $('detail').innerHTML = h;
  disableActionButtons(!!d.action_running || !!d.in_flight);
  if ((d.versions || []).length) {
    const bind = (sel, frame) => {
      const el = $(sel), fr = $(frame);
      const srcOf = (v) => '/preview/' + d.run_id + '/' + v;
      if (el && fr) { fr.src = srcOf(el.value);
        el.onchange = () => { fr.src = srcOf(el.value); }; }
    };
    bind('ver-a', 'frame-a'); bind('ver-b', 'frame-b');
    if ($('ver-a')) {  // A 默认最旧快照，B 默认当前；程序设值不触发 onchange，需同步 src
      $('ver-a').value = 'versions/' + d.versions[0].name;
      $('frame-a').src = '/preview/' + d.run_id + '/versions/' + d.versions[0].name;
    }
  }
}

async function sendAnswer(ix) {
  const text = $('ans-' + ix).value.trim();
  if (!text) return;
  const { ok, body } = await post('/api/task/' + selected + '/action',
    { kind: 'answer', interaction_id: ix, text });
  if (!ok) alert(body.error || '提交失败');
  pollDetail(true);
}

async function sendKind(kind) {
  const map = { feedback: 'feedback', goal: 'goalnew' };
  let payload = { kind };
  if (kind === 'feedback' || kind === 'goal') {
    const text = $(map[kind]).value.trim();
    if (!text) return;
    payload.text = text;
  } else if (kind === 'confirm') {
    payload.outcome = $('confirm-outcome').value;
    const btn = $('confirm-btn');
    if (btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.textContent = '确认提交（再次点击）';
      setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = '';
        btn.textContent = '核对确认'; } }, 8000);
      return;
    }
    btn.dataset.armed = '';
    btn.textContent = '核对确认';
  }
  const { ok, body } = await post('/api/task/' + selected + '/action', payload);
  if (!ok) alert(body.error || '提交失败');
  pollDetail(true);
}

async function requestStop() {
  const { ok, body } = await post('/api/task/' + selected + '/stop', {});
  if (!ok && body.error) alert(body.error || '请求失败');
  pollDetail(true);
}

async function revoke() {
  const btn = $('revoke-btn');
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = '确认撤回（再次点击，不可逆）';
    setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = '';
      btn.textContent = '撤回授权（已确认停止）'; } }, 8000);
    return;
  }
  btn.dataset.armed = '';
  btn.textContent = '撤回授权（已确认停止）';
  const { ok, body } = await post('/api/task/' + selected + '/action', { kind: 'revoke' });
  if (!ok) alert(body.error || '提交失败');
  pollDetail(true);
}

$('start-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const goal = $('goal').value.trim();
  if (!goal) return;
  $('start').disabled = true;
  $('start').textContent = '发起中…';
  const { ok, body } = await post('/api/start',
    { goal, timeout_s: parseInt($('timeout').value || '600', 10) });
  $('start').textContent = '发起任务';
  if (!ok) { alert(body.error || '发起失败'); refreshTasks(); return; }
  $('goal').value = '';
  if (body.run_id) { selected = body.run_id; lastFingerprint = ''; }
  refreshTasks(); pollDetail(true);
};

// 动作按钮事件委托：onclick 内联属性会被 CSP（script-src 'self'）拦截，
// 监听器挂在静态的 #detail 容器上，子节点重绘不影响
$('detail').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  if (btn.id === 'stop-btn') return requestStop();
  if (btn.id === 'confirm-btn') return sendKind('confirm');
  if (btn.id === 'feedback-btn') return sendKind('feedback');
  if (btn.id === 'goal-btn') return sendKind('goal');
  if (btn.id === 'revoke-btn') return revoke();
  if (btn.id === 'answer-btn') {
    const input = btn.parentElement.querySelector('input');
    const ix = input ? input.id.slice(4) : '';
    if (ix) return sendAnswer(ix);
  }
});
$('refresh').onclick = () => { refreshTasks(); pollDetail(true); };
refreshTasks();
if (window.location.hash) selected = window.location.hash.slice(1);
pollDetail(true);
setInterval(() => { refreshTasks(true); pollDetail(false); }, 3000);
"""

def page_html() -> str:
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Mentis · 本地工作台</title>'
            '<link rel="stylesheet" href="/workbench.css">'
            '<script src="/workbench.js" defer></script></head><body>'
            '<header class="topbar"><span class="logo">M</span><span class="brand">Mentis 工作台</span>'
            '<span class="subtitle">本机单用户 · 任务记录都在本机运行目录</span></header>'
            '<div class="wrap"><aside id="tasks"><div class="side-head">任务'
            '<button id="refresh" type="button">刷新</button></div>'
            '<div id="task-list"></div>'
            '<form id="start-form"><h3>发起新任务</h3>'
            '<textarea id="goal" rows="4" placeholder="用你自己的话描述网页目标（自然语言，不做问卷）"></textarea>'
            '<label>单次执行器超时秒数 <input id="timeout" type="number" value="600" min="60" max="600"></label>'
            '<button id="start" type="submit">发起任务</button>'
            '<p class="hint">发起后由 Mentis 自主委托与检查；需要你决定的事项会出现在"待决请求"。'
            '刷新页面不会重复发起任务。</p></form></aside>'
            '<main id="detail"><p class="hint">从左侧选择任务，或发起新任务。</p></main></div>'
            '</body></html>')


def make_server(runs_dir, port=8874, bench=None):
    bench = bench or Workbench(runs_dir)
    runs_root = Path(runs_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, body, content_type, csp=PAGE_CSP, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or []):
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json_response(self, payload, status=200):
            self.respond(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

        def _guard(self):
            host = self.headers.get("Host", "")
            port_actual = self.server.server_address[1]
            if host not in (f"127.0.0.1:{port_actual}", f"localhost:{port_actual}"):
                self.respond(403, b"Local host required", "text/plain")
                return False
            return True

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 65536:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            if not self._guard():
                return
            route = urlsplit(self.path).path
            try:
                if route == "/workbench.css":
                    self.respond(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
                elif route == "/workbench.js":
                    self.respond(200, JS.encode("utf-8"), "text/javascript; charset=utf-8")
                elif route == "/":
                    self.respond(200, page_html().encode("utf-8"), "text/html; charset=utf-8")
                elif route == "/api/tasks":
                    self.json_response({"tasks": bench.list_tasks(),
                                        "build_running": bench.build_running()})
                elif route.startswith("/api/task/"):
                    detail = bench.task_detail(route.split("/")[3])
                    if detail is None:
                        self.json_response({"error": "not found"}, 404)
                    else:
                        self.json_response(detail)
                elif route.startswith("/preview/"):
                    self._serve_preview(route)
                elif route.startswith("/evidence/"):
                    self._serve_evidence(route)
                elif route.startswith("/screenshot/"):
                    self._serve_screenshot(route)
                else:
                    self.respond(404, b"Not found", "text/plain")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.json_response({"error": f"读取失败：{type(exc).__name__}"}, 503)

        def _run_segment(self, route, prefix):
            # /<prefix><id>/<subpath> → (run_id, subpath)
            # P3.4 验收修复：允许白名单内的两段子路径（versions/<名>、trace/<名>），
            # 仍拒绝穿越、反斜杠与隐藏文件；隔离与白名单由各 serve 方法把关
            rest = route[len(prefix):]
            parts = rest.split("/", 1)
            if not parts or not parts[0]:
                return None, None
            run_id = parts[0]
            sub = parts[1] if len(parts) > 1 else "index.html"
            if "\\" in sub or ".." in sub or sub.startswith(".") \
                    or sub.endswith("/"):
                return run_id, None
            segments = sub.split("/")
            if len(segments) > 2 or any(not seg for seg in segments):
                return run_id, None
            return run_id, sub

        def _serve_preview(self, route):
            run_id, sub = self._run_segment(route, "/preview/")
            if not run_id:
                self.respond(404, b"Not found", "text/plain")
                return
            run_dir = (runs_root / run_id).resolve()
            if run_dir.parent != runs_root:
                self.respond(404, b"Not found", "text/plain")
                return
            if sub is None:  # 路径被 _run_segment 拒绝（穿越/非法段）
                self.respond(404, "路径不合法".encode("utf-8"), "text/plain; charset=utf-8")
                return
            # P3.4 验收修复（审计 #2）：versions/<名> 指向运行目录的 versions/
            # 快照；其余单段名指向 workspace 根。路径隔离不变：两者都只允许
            # 单层文件名，workdir 下其他目录（如 trace/state）不可达
            segments = sub.split("/")
            if len(segments) == 2:
                if segments[0] != "versions":
                    self.respond(404, b"Not found", "text/plain")
                    return
                target = (run_dir / "versions" / segments[1]).resolve()
                allowed_parent = (run_dir / "versions").resolve()
            else:
                target = (run_dir / "workspace" / sub).resolve()
                allowed_parent = (run_dir / "workspace").resolve()
            if target.parent != allowed_parent or not target.is_file():
                self.respond(404, "作品不存在".encode("utf-8"), "text/plain; charset=utf-8")
                return
            ctype = "text/html; charset=utf-8" if target.suffix == ".html" else \
                (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.respond(200, target.read_bytes(), ctype, PREVIEW_CSP)

        def _serve_evidence(self, route):
            run_id, sub = self._run_segment(route, "/evidence/")
            if not run_id or sub not in EVIDENCE_FILES:
                self.respond(404, b"Not found", "text/plain")
                return
            target = (runs_root / run_id / sub).resolve()
            if target.parent != (runs_root / run_id / Path(sub).parent).resolve() \
                    or not target.is_file():
                self.respond(404, "证据文件不存在".encode("utf-8"), "text/plain; charset=utf-8")
                return
            ctype = "application/json; charset=utf-8" if target.suffix == ".json" \
                and sub != "trace/events.jsonl" and sub != "trace/model-calls.jsonl" \
                and sub != "trace/stages.jsonl" else "text/plain; charset=utf-8"
            self.respond(200, target.read_bytes(), ctype, EVIDENCE_CSP)

        def _serve_screenshot(self, route):
            run_id, sub = self._run_segment(route, "/screenshot/")
            if not run_id or not sub or not sub.endswith(".png"):
                self.respond(404, b"Not found", "text/plain")
                return
            run_dir = (runs_root / run_id).resolve()
            meta = _load_json(run_dir / "state.json", {})
            allowed = {Path(p).name: Path(p) for p in meta.get("screenshots", [])}
            obs = run_dir / "observation"
            if obs.is_dir():
                for p in obs.glob("*.png"):
                    allowed.setdefault(p.name, p)
            source = allowed.get(sub)
            if source is None or not Path(source).is_file():
                self.respond(404, "截图不存在".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self.respond(200, Path(source).read_bytes(), "image/png", EVIDENCE_CSP)

        do_HEAD = do_GET

        def _write_guard(self) -> bool:
            """写接口守卫（P3.4 审计 #4）：仅接受本机同源页面的 JSON 请求。

            - Content-Type 必须是 application/json（允许 charset 后缀）；否则 415
            - 携带 Origin 时必须与本地服务同源（浏览器 fetch 对跨源/合成请求
              会带 Origin）；非本地来源 403。无 Origin 的本机工具调用放行
              （Host 校验已在 _guard 完成）
            """
            port_actual = self.server.server_address[1]
            origin = self.headers.get("Origin", "")
            if origin and origin not in (f"http://127.0.0.1:{port_actual}",
                                         f"http://localhost:{port_actual}"):
                self.json_response({"error": "写请求来源不是本机工作台页面；已拒绝"},
                                   403)
                return False
            ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self.json_response({"error": "写请求必须是 application/json；已拒绝"},
                                   415)
                return False
            return True

        def do_POST(self):
            if not self._guard():
                return
            route = urlsplit(self.path).path
            if not self._write_guard():
                return
            body = self._read_body()
            try:
                if route == "/api/start":
                    result = bench.start_task(body.get("goal", ""),
                                              int(body.get("timeout_s", 600)))
                    self.json_response(result, result.get("code", 200))
                elif route.startswith("/api/task/") and route.endswith("/stop"):
                    run_id = route.split("/")[3]
                    result = bench.request_stop(run_id)
                    self.json_response(result, result.get("code", 200))
                elif route.startswith("/api/task/") and route.endswith("/action"):
                    run_id = route.split("/")[3]
                    kind = body.get("kind", "")
                    result = bench.run_action(run_id, kind, body)
                    self.json_response(result, result.get("code", 200))
                else:
                    self.respond(404, b"Not found", "text/plain")
            except (OSError, ValueError, TypeError) as exc:
                self.json_response({"error": f"动作失败：{type(exc).__name__}"}, 500)

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve_workbench(runs_dir, port=8874):
    server = make_server(runs_dir, port)
    print(f"Mentis 本地工作台：http://127.0.0.1:{server.server_address[1]}/", flush=True)
    print(f"任务目录：{Path(runs_dir).resolve()}；本页只在本机可用；"
          "构建/动作由服务转发给真实 collab CLI（需要 Codex 登录态与模型凭据）。"
          "Ctrl+C 停止服务（进行中的任务记录不丢失，可重启服务后继续操作）。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
