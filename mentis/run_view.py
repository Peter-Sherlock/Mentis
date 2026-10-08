"""本地只读运行页面：复用 O1 投影与 O2 最后可靠状态；不重算业务判定。"""

import difflib
import hashlib
import html
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from mentis.run_projection import load_projection, load_run_state
from mentis.trace import _resolve_ref, load_events, render_conversation

EVENT_NAMES = {
    "goal.recorded": "记录目标", "policy.set": "确定委托范围",
    "evaluation_plan.recorded": "建立评估计划", "evaluation_plan.revised": "更新评估计划",
    "observation.structure": "观察页面结构", "observation.render": "测量页面",
    "observation.diagnosis": "模型提出诊断", "decision.routed": "作出处置",
    "decision.requested": "请求用户决定", "decision.answered": "用户作出决定",
    "delegation.prepared": "归档并准备委托", "executor.started": "执行器开始",
    "executor.item": "执行器公开进展", "executor.completed": "执行器调用结束",
    "executor.confirmed": "核对执行结果", "review.recorded": "记录评审结果",
    "run.finalized": "记录本次收尾", "run.resumed": "恢复任务",
    "professional_config.recorded": "冻结专业配置",
    "professional_config.revised": "改版专业配置",
    "professional_config.drift": "注册表漂移·沿用快照",
    "professional_config.blocked": "配置无法核验·委托不发起",
}
DECISIONS = {
    "plan_blind_spot": "计划未覆盖 · 未自动修改",
    "skip_unverifiable": "当前无法验证 · 未修改",
    "keep_as_is": "保持现状", "user_option": "用户可选 · 不自动修改",
    "adopt": "采纳修订", "reject": "拒收",
}
VERDICTS = {
    "no_change": "保留作品 · 未发生修改", "improved": "记录判定：改善",
    "changed_unverified": "已修改 · 改善未验证", "regression": "记录判定：回归",
    "unverified": "未验证", "unknown": "结果未知", "unrecorded": "结论未采集",
}
ACTORS = {"mentis": "Mentis", "executor": "执行器", "observer": "观察工具", "user": "用户"}
READABLE_SUFFIXES = {".html", ".png", ".jpg", ".jpeg", ".webp", ".md", ".json", ".txt", ".diff"}
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
FILE_CSP = ("sandbox; default-src 'none'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
            "form-action 'none'; base-uri 'none'")

CSS = """
:root{--ink:#172b3b;--muted:#637688;--blue:#176f85;--line:#dce5e9;--paper:#f4f7f8;--amber:#a45b0d}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}button,select,input{font:inherit}button,select{border:1px solid var(--line);background:white;border-radius:9px;padding:8px 12px;color:var(--ink);cursor:pointer}
.topbar{background:white;border-bottom:1px solid var(--line);padding:16px max(22px,calc((100vw - 1240px)/2));display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.logo{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--blue);color:white;font-weight:700}.brand{font-weight:750;font-size:20px}.subtitle,.muted{color:var(--muted)}.topbar .subtitle{margin-left:auto}
.wrap{max-width:1240px;margin:auto;padding:30px 22px 60px}.hero{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:24px;align-items:start}
.eyebrow{color:var(--blue);font-size:12px;letter-spacing:2px;font-weight:700}h1{font-size:32px;line-height:1.25;margin:9px 0 18px}h2{font-size:21px;margin:0 0 16px}h3{font-size:16px;margin:0 0 10px}p{margin:7px 0}.goal{font-size:16px;max-width:830px}.runid{font-size:12px;word-break:break-all;color:var(--muted);margin-top:14px}
.panel,.stat,.decision,.event{background:white;border:1px solid var(--line);border-radius:13px}.panel{padding:23px;margin-top:22px;scroll-margin-top:18px}.statusbox{padding:19px;background:#eaf3f4;border-color:#cbdfe2}.statusbox h3{font-size:20px}.note{font-size:13px;color:var(--muted)}.warning{background:#fff6e9;border:1px solid #f0d8b3;border-radius:10px;padding:12px 15px;color:#7e4d10}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:24px}.stat{padding:15px 18px}.number{font-size:27px;font-weight:750;line-height:1.3}.stat .muted{font-size:12px}
nav{display:flex;gap:10px;flex-wrap:wrap;margin-top:23px}nav a{background:white;border:1px solid var(--line);padding:7px 14px;border-radius:22px;font-size:13px}
.sectionhead,.controls{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.controls{justify-content:flex-start;margin:14px 0}label{font-size:13px;color:var(--muted)}
.decisiongrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.decision{padding:18px}.decision h3{font-size:16px;line-height:1.5}.badge{display:inline-block;padding:3px 8px;background:#edf3f6;border-radius:6px;font-size:12px;margin:4px 6px 8px 0}.badge.warn{background:#fff0d9;color:#8c540d}.reason{font-size:14px}.meta{font-size:12px;color:var(--muted);margin-top:9px;word-break:break-all}
.timeline{position:relative;display:grid;gap:10px}.event{padding:14px 17px;border-left:3px solid #95bfca}.eventheader{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.seq{color:var(--blue);font-weight:700}.time{font-size:12px;color:var(--muted);margin-left:auto}.event p{font-size:14px;overflow-wrap:anywhere}.event small{color:var(--muted)}
details{margin-top:9px}summary{cursor:pointer;color:var(--blue);font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f9;border-radius:8px;padding:14px;font:12px/1.7 Consolas,"Microsoft YaHei",monospace;max-height:540px;overflow:auto}code{font:12px Consolas,monospace;overflow-wrap:anywhere}
.scrolltable{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:12px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}td{overflow-wrap:anywhere}.files{display:grid;gap:12px}.file{border:1px solid var(--line);border-radius:9px;padding:14px;min-width:0}
.screenshots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.screenshots figure{margin:8px 0}.screenshots img{max-width:100%;max-height:510px;object-fit:contain;object-position:top;border:1px solid var(--line);border-radius:8px;background:white}.screenshots figcaption{font-size:12px;color:var(--muted)}
iframe{width:100%;height:540px;border:1px solid var(--line);border-radius:9px;background:white}.footer{padding-top:25px;color:var(--muted);font-size:12px}.gaplist{padding-left:20px;overflow-wrap:anywhere}.empty{color:var(--muted);padding:8px 0}#connection{font-size:12px;color:var(--muted)}[hidden]{display:none!important}
@media(max-width:760px){.hero{grid-template-columns:1fr}h1{font-size:27px}.wrap{padding:24px 14px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.decisiongrid,.screenshots{grid-template-columns:1fr}.panel{padding:17px}.topbar{padding:12px 16px}.topbar .subtitle{margin-left:0;font-size:12px}.time{margin-left:0}.goal{font-size:14px}iframe{height:450px}}
"""

JS = """
let busy=false, fingerprint=document.getElementById('content').dataset.fingerprint;
const status=document.getElementById('connection');
function filterEvents(){
 const choice=document.getElementById('actor-filter').value;
 document.querySelectorAll('.event').forEach(el=>el.hidden=choice!=='all'&&el.dataset.actor!==choice);
}
function nowClock(){
 return new Date().toLocaleTimeString('zh-CN',{hour12:false});
}
async function refreshView(){
 if(busy)return;busy=true;
 try{
  const response=await fetch('/api/view',{cache:'no-store'});
  if(!response.ok)throw new Error('读取失败');
  const data=await response.json();
  if(data.fingerprint!==fingerprint){
   const opened=new Set([...document.querySelectorAll('details[open]')].map(el=>el.id));
   const selected=document.getElementById('actor-filter').value;
   document.getElementById('content').innerHTML=data.html;
   fingerprint=data.fingerprint;
   document.querySelectorAll('details').forEach(el=>el.open=opened.has(el.id));
   document.getElementById('actor-filter').value=selected;filterEvents();
  }
  status.textContent='旁观器 '+nowClock()+' 已读取记录；这只表示读取成功，不代表任务进程仍在运行';
 }catch(error){status.textContent='旁观器 '+nowClock()+' 暂时无法读取；保留上次视图，任务状态未知';}
 finally{busy=false;}
}
document.addEventListener('change',event=>{if(event.target.id==='actor-filter')filterEvents();});
document.getElementById('refresh').addEventListener('click',refreshView);
setInterval(()=>{if(document.getElementById('follow').checked)refreshView();},4000);
"""

def esc(value):
    return html.escape(str(value if value is not None else "未采集"), quote=True)


def evidence_path(run_dir, value):
    """只提供运行投影已列出的文件；拒绝越界、秘密文件和私有目录。"""
    try:
        root = Path(run_dir).resolve()
        path = _resolve_ref(root, str(value)).resolve()
        relative = path.relative_to(root)
        if any(part in (".git", ".env", ".codex", ".aws", ".agents") for part in relative.parts):
            return None
        if path.suffix.lower() not in READABLE_SUFFIXES or not path.is_file():
            return None
        return path
    except (OSError, ValueError):
        return None


class RunView:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).resolve()
        if not self.run_dir.is_dir():
            raise ValueError("运行目录不存在")

    def snapshot(self):
        projection = load_projection(self.run_dir)
        events = load_events(self.run_dir)
        exchange = render_conversation(self.run_dir)
        state = load_run_state(self.run_dir)
        files = {}
        for row in projection.get("versions", []) + projection.get("ref_checks", []):
            if row.get("verify") == "unverified":
                continue
            path = evidence_path(self.run_dir, row.get("path", ""))
            if path is None:
                continue
            token = hashlib.sha256(str(path.relative_to(self.run_dir)).encode("utf-8")).hexdigest()[:20]
            expected = row.get("recorded_sha256") or row.get("sha256")
            files[token] = {"path": path, "sha256": expected, "label": row.get("label") or row.get("name") or path.name}
        serialized = json.dumps({"projection": projection, "events": events, "exchange": exchange,
                                 "state": state},
                                ensure_ascii=False, sort_keys=True).encode("utf-8")
        fingerprint = hashlib.sha256(serialized).hexdigest()
        fragment = render_content(projection, events, exchange, files, state)
        return {"fingerprint": fingerprint, "html": fragment, "projection": projection}, files

    def evidence(self, token):
        _, files = self.snapshot()
        row = files.get(token)
        if row is None:
            return None
        path = evidence_path(self.run_dir, row["path"])
        if path is None:
            return None
        data = path.read_bytes()
        if row.get("sha256") and hashlib.sha256(data).hexdigest() != row["sha256"]:
            return None
        return path, data


def render_status_panel(state):
    """O2 最后可靠状态（普通语言；细节可展开）。刷新/读取不代表生产者活跃。"""
    activity = state.get("activity", {})
    category = activity.get("category", "missing")
    activity_note = {
        "fresh": "活动信号有效：生产者近期确认活跃（活跃不等于有新进展）",
        "stale": "活动信号已过期：无法确认生产者仍活跃（不判失败、不解锁）",
        "corrupt": "活动信号损坏：实时状态不可靠",
        "missing": "活动信号未采集：不能从事件静默判断任务是否仍在运行",
        "old_generation": "活动信号属于旧代次：不代表当前生产者",
        "unclear": "活动信号时刻异常：状态不确定",
    }.get(category, "活动状态不确定")
    lines = ['<section class="panel statusbox" id="run-state"><div class="sectionhead">',
             '<h2>当前状态</h2><span class="note">只读投影；读取时刻仅表示读取，不代表任务活动</span></div>']
    gen = state.get("generation")
    lines.append('<p>运行代次：<strong>' + esc(gen if gen else "未采集") + '</strong>（恢复/续办产生新代次；'
                 '旧代次的收尾与活动信号不作为当前状态依据）</p>')
    finalized = state.get("finalized", {})
    if finalized.get("finalized"):
        if finalized.get("in_current_generation"):
            lines.append('<p>收尾：<strong>当前代次已收尾</strong>｜' + esc(finalized.get("outcome", "")) + '</p>')
        else:
            lines.append('<p class="warning">仅旧代次收尾存在于记录中；恢复后的新代次不据此视为完成。</p>')
    else:
        lines.append('<p>收尾：未见收尾记录（运行进行中，或收尾未采集）。</p>')
    current = state.get("current_stage")
    if current:
        lines.append('<p>当前阶段：<strong>' + esc(current.get("label")) + '</strong>（实例 '
                     + esc(current.get("instance")) + '，自 ' + esc(current.get("time_utc", "未采集"))
                     + ' 起，未关闭）' + (esc("；" + current.get("detail", "")) if current.get("detail") else '') + '</p>')
    else:
        finished = [r for r in state.get("stages", []) if r.get("phase") == "end"]
        if finished:
            last = finished[-1]
            duration = last.get("duration_s")
            lines.append('<p>当前阶段：无进行中阶段；最近结束 ' + esc(last.get("label")) + '（'
                         + esc(last.get("status")) + '，耗时 '
                         + (esc(f"{duration:.1f}s") if isinstance(duration, (int, float)) else '未记录')
                         + '）</p>')
        else:
            lines.append('<p>当前阶段：未采集（该运行早于阶段记录，不回填）。</p>')
    for wait in state.get("open_waits", []):
        lines.append('<p class="warning">等待你的决定（自 ' + esc(wait.get("time_utc", "未采集"))
                     + '）：' + esc(wait.get("reason")) + '。只有依赖这个决定的工作暂停；'
                     '其余通知不要求回复。</p>')
    activity_line = activity.get("line", "")
    if activity_note and activity.get("line"):
        lines.append('<p>生产者活动：' + esc(activity_line) + '</p>')
    else:
        lines.append('<p>生产者活动：' + esc(activity_note) + '</p>')
    lines.append('<p class="note">最近事件：[' + esc((state.get("last_event") or {}).get("seq", "未采集"))
                 + '] ' + esc((state.get("last_event") or {}).get("kind", "未采集"))
                 + ' ' + esc((state.get("last_event") or {}).get("time_utc", ""))
                 + '｜最后采集 ' + esc(state.get("last_collected", "未采集"))
                 + '；读取方时刻（仅表示读取）' + esc(state.get("read_at", "未采集")) + '</p>')
    budget = state.get("budget", {})
    if budget.get("limits"):
        parts = []
        for key, label in (("attempts", "委托"), ("revisions", "修订"), ("model_calls", "模型调用")):
            used = budget.get("used", {}).get(key)
            limit = budget.get("limits", {}).get(key)
            remaining = budget.get("remaining", {}).get(key)
            parts.append(label + " " + esc(used if used is not None else "未记录") + "/"
                         + esc(limit if limit is not None else "未记录")
                         + ("（余 " + esc(remaining) + "）" if remaining is not None else ""))
        lines.append('<p>预算：' + "；".join(parts) + '</p>')
    else:
        lines.append('<p class="note">' + esc(budget.get("note", "预算未采集；不虚构剩余额度")) + '</p>')
    for note in state.get("notes", []):
        lines.append('<p class="warning">' + esc(note) + '</p>')
    lines.append('<details id="state-detail"><summary>状态说明与边界</summary><ul>'
                 '<li>页面定时读取的是<strong>已保存的记录</strong>；读取成功不表示任务进程仍在运行。</li>'
                 '<li>「生产者活动」来自任务进程自己写入的活动信号（含有效期）；过期只表示无法确认活跃。</li>'
                 '<li>本页面只读，不启动、续办或取消任务。</li></ul></details></section>')
    return "".join(lines)


def _render_professional_panel(prof):
    """C1：专业配置按需查看（记录ed/未采集/核验不过分别如实显示；只读）。"""
    lines = ['<section id="professional" class="panel"><div class="sectionhead">'
             '<h2>专业配置</h2><span class="note">职责/资源快照/任务条件与绑定；配置不授予专业资格</span></div>']
    status = prof.get("status", "unrecorded")
    if status == "unrecorded":
        lines.append('<p class="note">' + esc(prof.get("note", "专业配置未采集（历史运行不回填）")) + '</p>')
        lines.append('</section>')
        return "".join(lines)
    cur = prof.get("current") or {}
    head = ('<p>状态：<strong>' + esc({"recorded": "已核验", "unverified": "unverified（核验未全过）"}
                                     .get(status, status)) + '</strong>｜版本历史 '
            + esc("、".join("v" + str(v) for v in prof.get("versions", [])) or "未采集")
            + '｜绑定记录 ' + esc(prof.get("bindings_count", 0)) + ' 条（配置↔brief↔计划）</p>')
    lines.append(head)
    lines.append('<p>当前 v' + esc(cur.get("version", "未采集")) + '（' + esc(cur.get("reason_kind", "未采集"))
                 + '）：' + esc(cur.get("reason_detail", "")) + '</p>')
    pb = (prof.get("plan_bindings") or [{}])[-1]
    if pb.get("plan_version"):
        lines.append('<p>计划关联：评估计划 v' + esc(pb.get("plan_version")) + '（sha256 前 12 位 '
                     + esc(str(pb.get("plan_sha256", ""))[:12] or "未采集") + '）</p>')
    grounds = prof.get("grounds", [])
    if grounds:
        lines.append('<details id="professional-grounds"><summary>资源快照（'
                     + str(len(grounds)) + ' 项；内容已冻结于运行目录）</summary><ul>')
        for g in grounds:
            lines.append('<li>' + esc(g.get("resource_id")) + '@' + esc(g.get("version"))
                         + '｜sha256 前 12 位 ' + esc(g.get("sha256_12"))
                         + '｜复核状态 ' + esc(g.get("review_status", "未采集")) + '</li>')
        lines.append('</ul></details>')
    else:
        lines.append('<p class="note">资源快照：无（该任务未命中领域资源，如实记录）。</p>')
    scope = cur.get("capability_scope") or {}
    if scope:
        lines.append('<p>能力范围：有资料=' + esc(scope.get("has_materials", "未采集"))
                     + '｜可执行=' + esc(scope.get("can_execute", "未采集"))
                     + '｜可观察=' + esc(scope.get("can_observe", "未采集"))
                     + '｜已验证范围=' + esc(scope.get("verified_scope", "未采集")) + '</p>')
    problems = prof.get("problems", [])
    for p in problems:
        lines.append('<p class="warning">核验问题：' + esc(p) + '</p>')
    lines.append('<details id="professional-detail"><summary>展开职责与任务条件</summary><pre>'
                 + esc(json.dumps({"responsibilities": cur.get("responsibilities", {}),
                                   "task_conditions": cur.get("task_conditions", {})},
                                  ensure_ascii=False, indent=2))
                 + '</pre></details></section>')
    return "".join(lines)


def render_content(proj, events, exchange, files, state=None):
    counts = proj.get("counts", {})
    verdict = proj.get("verdict", "unrecorded")
    last = events[-1] if events else {}
    lines = [
        '<section class="hero"><div><div class="eyebrow">MENTIS / 任务运行记录</div>',
        '<h1>这次任务，发生了什么</h1>',
        '<p class="goal">' + esc(proj.get("goal")) + '</p>',
        '<div class="runid">' + esc(proj.get("run_id")) + ' · ' + esc(proj.get("kind")) + '</div></div>',
        '<aside class="panel statusbox"><div class="eyebrow">记录中的结论</div>',
        '<h3>' + esc(VERDICTS.get(verdict, verdict)) + '</h3>',
        '<p class="note">' + esc(proj.get("verdict_basis")) + '</p>',
        '<p class="note">执行器结束、交付与质量改善分别核对。</p></aside></section>',
        '<div class="stats">',
    ]
    for key, label in (("events", "已读事件"), ("executor_dispatches", "已记录委托"),
                       ("model_calls", "已记录模型调用"), ("http_attempted", "已记录 HTTP 请求")):
        value = counts.get(key, "未采集")
        if not events and key in ("events", "executor_dispatches"):
            value = "未采集"
        lines.append('<div class="stat"><div class="number">' + esc(value) + '</div><div class="muted">' + label + '</div></div>')
    lines += ['</div>',
              render_status_panel(state) if state is not None else
              '<section class="panel statusbox"><p class="note">状态视图未生成；'
              '不能从事件静默判断任务是否仍在运行。</p></section>',
              '<nav><a href="#run-state">当前状态</a>'
              '<a href="#decisions">专业决策</a><a href="#timeline">执行时间线</a>',
              '<a href="#exchange">公开交流</a><a href="#calls">模型调用</a>'
              '<a href="#professional">专业配置</a><a href="#evidence">作品与证据</a></nav>',
              '<section id="decisions" class="panel"><div class="sectionhead"><h2>专业决策</h2><span class="note">模型提案与最终处置分别展示</span></div>']
    if not counts.get("executor_dispatches") and events:
        lines.append('<p class="warning">这份记录中没有执行器委托。以下是真实的诊断与处置，不补造 Codex 对话。</p>')
    lines.append('<div class="decisiongrid">')
    for index, decision in enumerate(proj.get("decisions", [])):
        code = str(decision.get("decision", "unrecorded"))
        source = {"program": "程序规则", "user": "用户", "model": "模型"}.get(decision.get("decision_source"), "未记录")
        lines += ['<article class="decision"><h3>' + esc(decision.get("no", index + 1)) + ' · ' + esc(decision.get("phenomenon")) + '</h3>',
                  '<span class="badge warn">' + esc(DECISIONS.get(code, code)) + '</span><span class="badge">处理者：' + esc(source) + '</span>',
                  '<p class="reason">' + esc(decision.get("reason")) + '</p>',
                  '<div class="meta">计划维度：' + esc(decision.get("dimension")) + ' · 后续：' + esc(decision.get("follow_up") or "未记录") + '</div>',
                  '<details id="decision-' + str(index) + '"><summary>查看原始决策字段</summary><pre>' + esc(json.dumps(decision, ensure_ascii=False, indent=2)) + '</pre></details></article>']
    if not proj.get("decisions"):
        lines.append('<p class="empty">没有可读取的候选处置记录；不据此推断没有专业判断。</p>')
    lines += ['</div><p class="meta">来源：' + esc("；".join(proj.get("decision_sources", [])) or "未采集") + '</p></section>',
              '<section id="timeline" class="panel"><div class="sectionhead"><h2>执行时间线</h2><select id="actor-filter" aria-label="按执行主体筛选"><option value="all">全部主体</option><option value="mentis">Mentis</option><option value="executor">执行器</option><option value="observer">观察工具</option><option value="user">用户</option></select></div>',
              '<p class="note">最后记录：' + esc(last.get("time_utc", "未采集")) + '。事件静默只表示暂无新记录；'
              '任务是否仍在运行见上方「当前状态」的生产者活动。</p><div class="timeline">']
    for index, event in enumerate(events):
        actor = str(event.get("source", "unknown"))
        kind = str(event.get("kind", "unknown"))
        lines += ['<article class="event" data-actor="' + esc(actor) + '"><div class="eventheader"><span class="seq">#' + esc(event.get("seq")) + '</span><strong>' + esc(EVENT_NAMES.get(kind, kind)) + '</strong><span class="badge">' + esc(ACTORS.get(actor, actor)) + '</span><span class="time">' + esc(event.get("time_utc")) + '</span></div>',
                  '<p>' + esc(event.get("summary", "")) + '</p><small>记录状态：' + esc(event.get("status")) + ' · 委托：' + esc(event.get("attempt_id") or "无关联记录") + '</small>',
                  '<details id="event-' + str(index) + '"><summary>查看事件与来源</summary><pre>' + esc(json.dumps(event, ensure_ascii=False, indent=2)) + '</pre></details></article>']
    if not events:
        lines.append('<p class="empty">事件未采集，无法重建时间线。</p>')
    lines += ['</div></section><section id="exchange" class="panel"><h2>公开交流与专业评审原文</h2><p class="note">确切指令、公开回复与外部决策；不展示内部推理。</p><details id="full-exchange"><summary>展开完整可见交流</summary><pre>' + esc(exchange) + '</pre></details></section>',
              '<section id="calls" class="panel"><h2>模型调用</h2><p class="note">阶段和用量仅展示已保存字段；缺失显示未采集。</p><div class="scrolltable"><table><thead><tr><th>阶段 / 请求</th><th>模型 / 来源</th><th>耗时</th><th>实际用量</th></tr></thead><tbody>']
    for call in proj.get("model_calls", []):
        usage = call.get("usage") or {}
        stage_cell = esc(call.get("stage"))
        if call.get("config_version"):
            stage_cell += ('<br><small>专业配置 v' + esc(call.get("config_version"))
                           + '（sha ' + esc(str(call.get("config_sha256", ""))[:12]) + '）</small>')
        lines += ['<tr><td>' + stage_cell + '<br><code>' + esc(call.get("request_id")) + '</code></td>',
                  '<td>' + esc(call.get("model")) + '<br>' + ("Mock" if call.get("mock") else "非 Mock 记录") + '</td>',
                  '<td>' + esc(call.get("latency_ms")) + ' ms</td><td>' + esc(json.dumps(usage, ensure_ascii=False) if usage else "用量未采集") + '</td></tr>']
    lines += ['</tbody></table></div></section>', _render_professional_panel(proj.get("professional") or {}),
              '<section id="evidence" class="panel"><h2>作品与证据</h2>',
              '<p class="note">文件来自本次运行的证据索引；哈希失配或越界文件不提供读取。截图按原记录展示，不生成新的作品版本。</p>']
    screenshots = []
    preview = None
    initial = final = None
    lines.append('<div class="files">')
    for token, item in files.items():
        path = item["path"]
        label = str(item["label"])
        url = "/evidence/" + token
        lines.append('<div class="file"><a href="' + url + '" target="_blank" rel="noopener">' + esc(label) + '</a><div class="meta">' + esc(path.relative_to(Path(proj["run_dir"]).resolve())) + '</div><code>SHA-256 ' + esc(item.get("sha256") or "未记录") + '</code></div>')
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            screenshots.append('<figure><a href="' + url + '" target="_blank" rel="noopener"><img loading="lazy" src="' + url + '" alt="' + esc(label) + '"></a><figcaption>' + esc(label) + '</figcaption></figure>')
        if path.suffix.lower() == ".html" and ("当前作品" in label or path.name == "index.html"):
            preview = url
        if path.name == "initial.html" or label == "v1 产物":
            initial = (url, item)
        if (path.name.startswith("final-") and path.suffix.lower() == ".html") or label == "v2 产物":
            final = (url, item)
            preview = url
    lines.append('</div>')
    if initial and final:
        same = initial[1].get("sha256") == final[1].get("sha256")
        lines.append('<details id="version-compare"><summary>展开首稿与结果对照</summary>')
        lines.append('<p class="note">' + ("两份归档的哈希相同：本次未修改作品。" if same else "两份归档内容不同，变化可从文件差异核对。") + '</p>')
        lines.append('<div class="screenshots"><div><h3>首稿归档</h3><iframe sandbox="" title="首稿归档" src="' + initial[0] + '"></iframe></div><div><h3>结果归档</h3><iframe sandbox="" title="结果归档" src="' + final[0] + '"></iframe></div></div>')
        before = initial[1]["path"].read_bytes()
        after = final[1]["path"].read_bytes()
        if hashlib.sha256(before).hexdigest() == initial[1].get("sha256") and hashlib.sha256(after).hexdigest() == final[1].get("sha256"):
            diff = "".join(difflib.unified_diff(before.decode("utf-8", errors="replace").splitlines(keepends=True), after.decode("utf-8", errors="replace").splitlines(keepends=True), fromfile="首稿归档", tofile="结果归档"))
            lines.append('<pre>' + esc(diff or "没有文件差异。") + '</pre>')
        else:
            lines.append('<p class="warning">文件在读取期间变化，对照未绑定，不能据此作结论。</p>')
        lines.append('</details>')
    if preview:
        lines.append('<details id="artifact-preview"><summary>展开当前作品的静态预览</summary><p class="note">只读预览，脚本与表单发送未执行。</p><iframe sandbox="" title="当前作品静态预览" src="' + preview + '"></iframe></details>')
    if screenshots:
        lines.append('<div class="screenshots">' + "".join(screenshots) + '</div>')
    if not files:
        lines.append('<p class="empty">没有可提供的绑定证据文件。</p>')
    prof = proj.get("professional") or {}
    if prof.get("status") == "recorded":
        boundary = ('<p>本任务的专业配置已随任务冻结（版本/资源快照/绑定见「专业配置」）；'
                    '配置只解决一致性，不授予专业资格，专业效果仍按作品对照与独立评审记账。</p>')
    elif prof.get("status") == "unverified":
        boundary = ('<p class="warning">专业配置存在但哈希核验未全通过：展示标签 unverified，'
                    '不能据此声称已绑定。</p>')
    else:
        boundary = ('<p>专家身份未作为统一的任务配置独立归档；已保存的评估计划和判断依据可从上方文件核对。专业效果仍按独立评审记账。</p>')
    lines += ['</section><section class="panel"><h2>本次记录的边界</h2>', boundary,
              '<ul class="gaplist">']
    for gap in proj.get("gaps", []):
        lines.append('<li>' + esc(gap) + '</li>')
    if proj.get("usage_note"):
        lines.append('<li>' + esc(proj["usage_note"]) + '</li>')
    lines += ['<li>本页面只读取记录，不启动、续办或取消任务；刷新状态不代表任务进程存活。</li></ul></section>',
              '<footer class="footer">同一 O1 运行投影 · 原始记录不回填 · 未采集的信息如实显示</footer>']
    return "".join(lines)


def page_html(snapshot):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Mentis · 运行流程</title><link rel="stylesheet" href="/viewer.css">'
            '<script src="/viewer.js" defer></script></head><body>'
            '<header class="topbar"><span class="logo">M</span><span class="brand">Mentis</span>'
            '<span class="subtitle">本地运行观察 · 只读</span></header><div class="wrap">'
            '<div class="controls"><button id="refresh">立即刷新</button>'
            '<label><input type="checkbox" id="follow" checked> 跟随新记录</label>'
            '<span id="connection">读取已保存记录；刷新只表示读取，不代表任务进程仍在运行</span></div>'
            '<main id="content" data-fingerprint="' + snapshot["fingerprint"] + '">' +
            snapshot["html"] + '</main></div></body></html>')


def make_server(run_dir, port=8873):
    view = RunView(run_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, body, content_type, csp=PAGE_CSP):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def serve(self):
            host = self.headers.get("Host", "")
            actual_port = self.server.server_address[1]
            if host not in (f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"):
                self.respond(403, b"Local host required", "text/plain")
                return
            route = urlsplit(self.path).path
            try:
                if route == "/viewer.css":
                    self.respond(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
                elif route == "/viewer.js":
                    self.respond(200, JS.encode("utf-8"), "text/javascript; charset=utf-8")
                elif route in ("/", "/api/view"):
                    snapshot, _ = view.snapshot()
                    if route == "/":
                        self.respond(200, page_html(snapshot).encode("utf-8"), "text/html; charset=utf-8")
                    else:
                        body = json.dumps({"fingerprint": snapshot["fingerprint"], "html": snapshot["html"]}, ensure_ascii=False).encode("utf-8")
                        self.respond(200, body, "application/json; charset=utf-8")
                elif route.startswith("/evidence/") and route.count("/") == 2:
                    result = view.evidence(route.rsplit("/", 1)[1])
                    if result is None:
                        self.respond(404, b"Evidence unavailable or unverified", "text/plain")
                        return
                    path, data = result
                    content_type = mimetypes.guess_type(path.name)[0] if path.suffix.lower() in (".html", ".png", ".jpg", ".jpeg", ".webp") else "text/plain"
                    self.respond(200, data, (content_type or "application/octet-stream") + ("; charset=utf-8" if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") else ""), FILE_CSP)
                else:
                    self.respond(404, b"Not found", "text/plain")
            except (OSError, ValueError, TypeError, KeyError):
                self.respond(503, "记录暂不可读取；不推断任务状态".encode("utf-8"), "text/plain; charset=utf-8")

        do_GET = serve
        do_HEAD = serve

        def do_POST(self):
            self.respond(405, b"Read-only viewer", "text/plain")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve_run(run_dir, port=8873):
    server = make_server(run_dir, port)
    print(f"Mentis 只读流程页：http://127.0.0.1:{server.server_address[1]}/", flush=True)
    print(f"运行：{Path(run_dir).resolve()}；只读取记录，不发起 AI 调用。Ctrl+C 停止查看服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
