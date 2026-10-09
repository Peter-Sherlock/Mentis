"""P2.4-O1 共用只读运行投影：CLI（mentis replay）与后续页面（O3）共用。

纪律（docs/OBSERVABILITY_PLAN.md §3）：
- 只读：不改写运行目录任何字节；不重算、不改变原业务判定；投影可再生成，不是第二事实来源。
- 来源分「原始记录」「按现存文件确定性读取」「未采集」；缺失标 unrecorded，
  无法核验（哈希不匹配/引用越界/文件缺失）标 unverified。这两个标签不是新的业务判定。
- 历史运行只按现存材料确定性读取；缺明确关联就标缺口，不猜测候选、阶段、时间或版本，
  不生成解释补齐历史。
- 模型调用次数、HTTP 请求数、执行器委托数分别记账；未知用量不写 0，不虚构货币成本。
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from mentis.trace import (_resolve_ref, activity_status, current_generation,
                          load_events, load_model_calls, load_stages,
                          load_heartbeat)

PROJECTION_SCHEMA = "mentis-run-projection/1"
RUN_STATE_SCHEMA = "mentis-run-state/1"

STAGE_LABELS = {
    "plan": "生成评估计划", "build": "执行器制作", "observe": "观察与测量",
    "diagnose": "模型诊断", "adjudicate": "候选处置", "revise": "委托修订",
    "reverify": "复检", "waiting_user": "等待用户决定", "finalize": "收尾",
    "unrecorded_stage": "未登记阶段", "corrupt_line": "记录损坏行",
}

_UNRECORDED = "unrecorded"
_UNVERIFIED = "unverified"

# 审查修正 A：决定关联标识——优先事件 interaction_id 字段；历史事件按程序生成
# 摘要中的 ix-* 确定性解析（大小写不敏感，键统一小写）；都没有则视为无法关联
# （标缺口，不臆造）。
_IX_RE = re.compile(r"\b(IX-[A-Za-z0-9]+)\b", re.IGNORECASE)


def _decision_key(event: dict):
    ix = event.get("interaction_id")
    if ix:
        return str(ix).strip().lower()
    m = _IX_RE.search(str(event.get("summary", "")))
    return m.group(1).lower() if m else None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify_ref(run_dir: Path, ref) -> dict:
    """核验一个 {path, sha256, role} 引用。只读取运行目录内的文件：
    越界引用不读取（标 unverified），哈希不匹配标 unverified，缺失标 unverified。"""
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not ref.get("path"):
        return {"path": "", "sha256": "", "role": (ref or {}).get("role", "") if isinstance(ref, dict) else "",
                "verify": _UNVERIFIED, "verify_detail": "引用缺路径或格式错误（未读取）"}
    run_root = Path(run_dir).resolve()
    try:
        path = _resolve_ref(Path(run_dir), ref["path"]).resolve()
    except (OSError, ValueError):
        return {"path": ref["path"], "sha256": ref.get("sha256", ""), "role": ref.get("role", ""),
                "verify": _UNVERIFIED, "verify_detail": "引用路径无法解析（未读取）"}
    inside = path == run_root or run_root in path.parents
    record = {"path": ref["path"], "name": path.name,
              "sha256": ref.get("sha256", ""), "role": ref.get("role", "")}
    if not inside:
        record["verify"] = _UNVERIFIED
        record["verify_detail"] = "越界引用（不在本运行目录内）：未读取"
        return record
    if not path.is_file():
        record["verify"] = _UNVERIFIED
        record["verify_detail"] = "引用文件缺失"
        return record
    if not isinstance(ref.get("sha256"), str) or not ref.get("sha256"):
        record["verify"] = _UNRECORDED
        record["verify_detail"] = "引用无哈希（现算 sha256 前 12 位 " + _sha256_file(path)[:12] + "）"
        record["name"] = path.name
        return record
    if _sha256_file(path) != ref["sha256"]:
        record["verify"] = _UNVERIFIED
        record["verify_detail"] = "当前文件哈希与记录不匹配（内容可能已变化）"
        return record
    record["verify"] = "verified"
    record["verify_detail"] = ""
    return record


def detect_run_kind(run_dir: Path) -> tuple:
    """按现存文件确定性判断运行类别；不识别时返回 unknown。"""
    run_dir = Path(run_dir)
    state = run_dir / "runner-state.json"
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("schema", "")).startswith("mentis-eval-state"):
                return "eval-run", data
        except (OSError, json.JSONDecodeError):
            return "eval-run", {}
    session = run_dir / "session.json"
    gstate = run_dir / "state.json"
    if session.is_file() and gstate.is_file():
        try:
            meta = json.loads(gstate.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("state_version") == "p3.2":
                return "guidance", meta
        except (OSError, json.JSONDecodeError):
            pass
        return "collab", {}
    if session.is_file():
        return "collab", {}
    if gstate.is_file():
        return "collab", {}
    return "unknown", {}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_decisions(run_dir: Path, kind: str, state: dict) -> tuple:
    """决策记录：模型提案 → 程序/用户处置。全部按现存文件确定性读取。

    返回 (decisions, sources)。decisions 每条含 no/phenomenon（模型提案）/dimension/
    decision/decision_source/reason/mapping/follow_up/recheck/refs。
    """
    run_dir = Path(run_dir)
    sources, decisions = [], []
    if kind == "eval-run":
        summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
        candidates = summary.get("candidates")
        if isinstance(candidates, list) and candidates:
            sources.append("runner-state.json summary.candidates（按现存文件确定性读取）")
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                decisions.append({
                    "no": c.get("no", ""), "phenomenon": c.get("phenomenon", ""),
                    "dimension": c.get("dimension", "") or "（未对应）",
                    "decision": c.get("decision", ""), "decision_source": "program",
                    "reason": c.get("reason", ""),
                    "mapping": {"matched_by": c.get("matched_by", _UNRECORDED),
                                "score": c.get("mapping_score") if c.get("mapping_score") is not None else _UNRECORDED},
                    "follow_up": ("零修订" if c.get("decision") != "adopt" else "转入修订委托"),
                    "recheck": "", "refs": [], "evidence_refs": [],
                })
        else:
            sources.append("runner-state.json 无候选记录：决策明细 unrecorded")
    elif kind == "collab":
        review = _load_json(run_dir / "professional-review.json")
        candidates = review.get("candidates")
        if isinstance(candidates, list) and candidates:
            sources.append("professional-review.json（按现存文件确定性读取）")
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                reason = str(c.get("reason", ""))
                score = _UNRECORDED
                matched = _UNRECORDED
                if "映射 " in reason:
                    # 理由文本中的映射备注是程序留档的一部分，原样引用不重算
                    pass
                decisions.append({
                    "no": c.get("no", ""), "phenomenon": c.get("phenomenon", ""),
                    "dimension": c.get("dimension", "") or "（未对应）",
                    "decision": c.get("decision", ""), "decision_source": "program",
                    "reason": reason,
                    "mapping": {"matched_by": matched, "score": score},
                    "follow_up": ("转入修订委托" if c.get("decision") == "adopt" else "留档不修订"),
                    "recheck": "", "refs": [],
                    "evidence_refs": list(c.get("evidence_refs", []) or []),
                })
            binding = review.get("evaluation_plan")
            if isinstance(binding, dict) and binding.get("sha256"):
                sources.append(f"评审绑定评估计划 v{binding.get('version')}（sha256 前 12 位 "
                               + str(binding.get("sha256", ""))[:12] + "）")
        else:
            sources.append("无 professional-review.json：候选级决策 unrecorded")
    elif kind == "guidance":
        rounds = state.get("rounds")
        if isinstance(rounds, list) and rounds:
            sources.append("state.json rounds（轮级记录，按现存文件确定性读取）")
            for r in rounds:
                if not isinstance(r, dict):
                    continue
                verification = r.get("verification") if isinstance(r.get("verification"), dict) else {}
                decisions.append({
                    "no": f"round{r.get('round_no', '?')}",
                    "phenomenon": (f"诊断 {r.get('diagnosis_path') or 'unrecorded'}"[:120]),
                    "dimension": "", "decision": "revise_round",
                    "decision_source": "program",
                    "reason": "诊断成立问题 → 预算内修订委托（轮级记录；候选级路由未实现，如实记录）",
                    "mapping": {"matched_by": _UNRECORDED, "score": _UNRECORDED},
                    "follow_up": f"修订 round{r.get('round_no', '?')}（指令 {Path(r['instruction_path']).name if r.get('instruction_path') else 'unrecorded'}）",
                    "recheck": str(verification.get("verdict", _UNRECORDED)),
                    "refs": [], "evidence_refs": [],
                })
        else:
            sources.append("state.json 无轮记录：决策明细 unrecorded")
    # 用户决定：事件流中的 decision.requested/answered（有 trace 的新运行才有）
    return decisions, sources


def _load_user_decisions(events: list) -> list:
    rows = []
    for e in events:
        if e.get("kind") == "decision.requested":
            rows.append({"seq": e.get("seq"), "kind": "requested",
                         "summary": e.get("summary", ""), "status": e.get("status", "")})
        elif e.get("kind") == "decision.answered":
            rows.append({"seq": e.get("seq"), "kind": "answered",
                         "summary": e.get("summary", ""), "status": e.get("status", "")})
    return rows


def _load_model_calls_view(run_dir: Path, kind: str, state: dict) -> tuple:
    """模型调用视图：优先 O1 sidecar（新运行，含阶段关联）；历史 eval 运行按
    runner-state.model_requests 确定性读取（缺字段标 unrecorded，阶段只做
    用量与计划实例化记录的精确匹配关联，不做猜测性推断）。"""
    run_dir = Path(run_dir)
    gaps = []
    calls = load_model_calls(run_dir)
    if calls:
        return ([_normalize_sidecar_call(c) for c in calls], gaps)
    if kind == "eval-run":
        recorded = state.get("model_requests")
        if not isinstance(recorded, list):
            recorded = []
        plan_path = run_dir / "evaluation-plan" / "plan-v1.json"
        plan = _load_json(plan_path)
        mi = plan.get("model_instantiation") if isinstance(plan.get("model_instantiation"), dict) else {}
        mi_usage = mi.get("usage") if isinstance(mi.get("usage"), dict) else None
        view = []
        for c in recorded:
            if not isinstance(c, dict):
                continue
            usage = c.get("usage") if isinstance(c.get("usage"), dict) else {}
            stage = ""
            if mi_usage and usage and all(usage.get(k) == v for k, v in mi_usage.items()):
                stage = "plan_instantiation（用量与计划实例化记录精确匹配）"
            view.append({
                "request_id": c.get("request_id", "") or _UNRECORDED,
                "model": _UNRECORDED, "created_at": _UNRECORDED,
                "latency_ms": _UNRECORDED,
                "ok": c.get("ok"), "usage": usage or None,
                "error": c.get("error", ""), "mock": c.get("mock"),
                "http_attempted": c.get("http_attempted"),
                "prompt_sha256": _UNRECORDED, "image_refs": [],
                "stage": stage or "unrecorded（调用与诊断/复检的关联未记录）",
                "output_refs": [], "input_text_archived": False,
            })
        if recorded:
            gaps.append("历史运行缺 trace/model-calls.jsonl：调用的模型名/时刻/耗时/输入哈希/图像指纹与逐调用产出关联 unrecorded；仅 runner-state.model_requests 所记字段可读")
        return view, gaps
    if kind in ("collab", "guidance"):
        gaps.append("该运行未逐调用保存模型调用记录（O1 之前的版本）：每次调用的请求 ID/模型/耗时/用量/输入哈希 unrecorded；仅报告中的用量汇总可读")
        return [], gaps
    return [], gaps


def _normalize_sidecar_call(c: dict) -> dict:
    out = {
        "request_id": c.get("request_id", _UNRECORDED),
        "model": c.get("model", _UNRECORDED),
        "created_at": c.get("created_at", _UNRECORDED),
        "latency_ms": c.get("latency_ms", _UNRECORDED),
        "ok": c.get("ok"), "usage": c.get("usage") or None,
        "error": c.get("error", ""), "mock": c.get("mock"),
        "http_attempted": c.get("http_attempted"),
        "prompt_sha256": c.get("prompt_sha256", _UNRECORDED),
        "image_refs": c.get("image_refs", []),
        "stage": c.get("stage", _UNRECORDED),
        "output_refs": c.get("output_refs", []),
        "input_text_archived": bool(c.get("input_text_archived")),
    }
    if c.get("config_version"):  # C1：配置关联字段存在才透出（历史行不回填）
        out.update({"config_version": c.get("config_version"),
                    "config_sha256": c.get("config_sha256", ""),
                    "stage_responsibility": c.get("stage_responsibility", "")})
    return out


def _load_versions_and_evidence(run_dir: Path, kind: str, state: dict, events: list) -> tuple:
    """版本与证据：brief/计划/产物版本、截图与差异入口。哈希为现在计算（现算），
    与事件 refs 匹配时标 verified，不匹配标 unverified；材料缺失标 unrecorded。"""
    run_dir = Path(run_dir)
    rows = []
    # 评估计划版本
    plan_dir = run_dir / "evaluation-plan"
    if plan_dir.is_dir():
        for p in sorted(plan_dir.glob("plan-v*.json")):
            ref_sha = ""
            for e in events:
                for r in (e.get("refs") or []):
                    if isinstance(r, dict) and r.get("role") == "plan" \
                            and Path(str(r.get("path", ""))).name == p.name:
                        ref_sha = r.get("sha256", "")
            actual = _sha256_file(p)
            status = ("verified" if ref_sha == actual else
                      (_UNVERIFIED if ref_sha else "现算（事件引用未核对）"))
            rows.append({"label": f"评估计划 {p.name}", "path": str(p), "sha256": actual,
                         "recorded_sha256": ref_sha, "verify": status})
    # 产物版本
    if kind == "eval-run":
        manifest = _load_json(run_dir / "run-manifest.json")
        artifact = manifest.get("artifact") if isinstance(manifest.get("artifact"), dict) else {}
        for p in sorted((run_dir / "versions").glob("*.html")) if (run_dir / "versions").is_dir() else []:
            rows.append({"label": f"产物版本 {p.name}", "path": str(p), "sha256": _sha256_file(p),
                         "recorded_sha256": "", "verify": "现算（对照 manifest 产物 sha 前 12 位 "
                         + str(artifact.get("sha256", ""))[:12] + "）"})
    elif kind == "guidance":
        for name, label in (("v1/index.html", "v1 产物"), ("v2/index.html", "v2 产物"),
                            ("freeze.json", "冻结记录")):
            p = run_dir / name
            if p.is_file():
                rows.append({"label": label, "path": str(p), "sha256": _sha256_file(p),
                             "recorded_sha256": "", "verify": "现算"})
    else:
        for p in sorted((run_dir / "versions").glob("*.html")) if (run_dir / "versions").is_dir() else []:
            rows.append({"label": f"产物快照 {p.name}", "path": str(p), "sha256": _sha256_file(p),
                         "recorded_sha256": "", "verify": "现算"})
        index = run_dir / "workspace" / "index.html"
        if index.is_file():
            rows.append({"label": "当前作品 workspace/index.html", "path": str(index),
                         "sha256": _sha256_file(index), "recorded_sha256": "", "verify": "现算"})
    # 截图与测量
    obs_dir = run_dir / "observation"
    if obs_dir.is_dir():
        for p in sorted(obs_dir.rglob("*.png")):
            rows.append({"label": f"截图 {p.relative_to(run_dir)}", "path": str(p),
                         "sha256": _sha256_file(p), "recorded_sha256": "", "verify": "现算"})
        for p in sorted(obs_dir.rglob("measurement.json")):
            rows.append({"label": f"渲染测量 {p.relative_to(run_dir)}", "path": str(p),
                         "sha256": _sha256_file(p), "recorded_sha256": "", "verify": "现算"})
    # 盲比/复检记录
    for p in sorted(run_dir.glob("blind-review*.json")) + sorted(run_dir.glob("review-*.json")):
        if p.name == "run-manifest.json":
            continue
        rows.append({"label": f"复检记录 {p.name}", "path": str(p), "sha256": _sha256_file(p),
                     "recorded_sha256": "", "verify": "现算"})
    return rows


# ---- C1：专业配置只读视图（投影/页面/status 共用；不回填、不改变判定） ----

def load_professional_view(run_dir) -> dict:
    """读取任务专业配置（mentis-professional-config/1）与绑定记录。

    recorded=配置可读且核验通过；unverified=存在但哈希核验不过（展示标签，
    不是业务判定）；unrecorded=未采集（C1 前运行或未走配置链），不回填。
    """
    from mentis.professional import _config_path, list_config_versions, load_bindings

    run_dir = Path(run_dir)
    versions = list_config_versions(run_dir)
    if not versions:
        return {"status": "unrecorded", "versions": [], "current": None,
                "grounds": [], "bindings_count": 0, "plan_bindings": [],
                "problems": [],
                "note": "未采集（历史运行不回填：不能据现存材料补造当时身份）"}
    import hashlib

    problems = []
    bindings = load_bindings(run_dir)
    plan_bindings = [{"config_version": b.get("config_version"),
                      "plan_version": b.get("plan_version"),
                      "plan_sha256": b.get("plan_sha256", "")}
                     for b in bindings if b.get("event") == "bind_plan"]
    for v in versions:
        path = _config_path(run_dir, v)
        try:
            payload = path.read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
        except OSError:
            problems.append(f"配置 v{v} 文件不可读")
            continue
        expected = next((str(b.get("config_sha256", "")) for b in bindings
                         if b.get("event") == "bind" and b.get("config_version") == v), "")
        if not expected:
            problems.append(f"配置 v{v} 缺少绑定记录，无法核验")
        elif expected != actual:
            problems.append(f"配置 v{v} 字节与绑定记录不符（现 {actual[:12]}）")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            problems.append(f"配置 v{v} 不是合法 JSON")
            continue
        for g in data.get("grounds", []):
            snap = run_dir / "professional-config" / str(g.get("snapshot", ""))
            try:
                content = snap.read_bytes()
            except OSError:
                problems.append(f"配置 v{v} 资源 {g.get('resource_id')} 快照缺失")
                continue
            if hashlib.sha256(content).hexdigest() != g.get("content_sha256"):
                problems.append(f"配置 v{v} 资源 {g.get('resource_id')} 快照哈希不符")
    current_version = versions[-1]
    data = _load_json(_config_path(run_dir, current_version))
    if not isinstance(data, dict):
        return {"status": "unverified", "versions": versions, "current": None,
                "grounds": [], "bindings_count": len(bindings),
                "plan_bindings": plan_bindings, "problems": problems,
                "note": "当前版本配置不可读（展示标签 unverified，不是业务判定）"}
    grounds = [{"resource_id": g.get("resource_id", _UNRECORDED),
                "version": g.get("version", _UNRECORDED),
                "sha256_12": str(g.get("content_sha256", ""))[:12],
                "review_status": g.get("review_status", _UNRECORDED),
                "snapshot": g.get("snapshot", "")}
               for g in data.get("grounds", []) if isinstance(g, dict)]
    return {
        "status": "unverified" if problems else "recorded",
        "versions": versions,
        "current": {
            "version": data.get("version"),
            "created_at": data.get("created_at", _UNRECORDED),
            "reason_kind": data.get("reason_kind", _UNRECORDED),
            "reason_detail": data.get("reason_detail", ""),
            "goal": data.get("goal", ""),
            "task_conditions": data.get("task_conditions", {}),
            "responsibilities": data.get("responsibilities", {}),
            "capability_scope": data.get("capability_scope", {}),
        },
        "grounds": grounds,
        "bindings_count": len(bindings),
        "plan_bindings": plan_bindings,
        "problems": problems,
        "note": ("哈希核验未全通过（展示标签 unverified，不是业务判定；详见 problems）"
                 if problems else
                 f"配置 v1—v{current_version} 已核验；bindings.jsonl 关联配置↔brief↔计划"),
    }


def load_projection(run_dir) -> dict:
    """确定性读取一个运行目录，生成共用投影 dict（不写任何文件、不改判定）。"""
    run_dir = Path(run_dir)
    kind, state = detect_run_kind(run_dir)
    events = load_events(run_dir)
    manifest = _load_json(run_dir / "trace" / "manifest.json")
    delegations = manifest.get("delegations") if isinstance(manifest.get("delegations"), dict) or isinstance(manifest.get("delegations"), list) else []
    integrity = manifest.get("integrity", {}) if isinstance(manifest.get("integrity"), dict) else {}

    decisions, decision_sources = _load_decisions(run_dir, kind, state if isinstance(state, dict) else {})
    user_decisions = _load_user_decisions(events)
    calls_view, call_gaps = _load_model_calls_view(run_dir, kind, state if isinstance(state, dict) else {})
    versions = _load_versions_and_evidence(run_dir, kind, state if isinstance(state, dict) else {}, events)
    professional = load_professional_view(run_dir)

    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    verdict = summary.get("verdict")
    verdict_basis = summary.get("verdict_basis", "")
    goal = ""
    if kind == "eval-run":
        meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
        goal = (meta.get("input") or {}).get("user_goal", "") if isinstance(meta.get("input"), dict) else ""
    elif kind == "guidance":
        goal = str(state.get("goal", ""))
    else:
        session = _load_json(run_dir / "session.json")
        goal = str(session.get("goal", ""))
    if not goal:
        for e in events:
            if e.get("kind") == "goal.recorded":
                goal = e.get("summary", "")
                break
            if e.get("kind") == "policy.set":
                goal = goal or ""
        if not goal:
            goal = _UNRECORDED

    # 分别记账：模型调用 / HTTP 实发 / 执行器委托；不虚构货币成本
    model_call_count = len(calls_view)
    http_count = sum(1 for c in calls_view if c.get("http_attempted"))
    executor_dispatch_count = sum(1 for e in events
                                  if e.get("kind") == "delegation.prepared" and e.get("status") == "ok")
    if not executor_dispatch_count and isinstance(delegations, list):
        executor_dispatch_count = len([d for d in delegations
                                       if isinstance(d, dict) and d.get("prompt_sha256")])

    # 事件级证据核验（refs 全量核对；越界不读取）
    ref_results = []
    for e in events:
        for r in (e.get("refs") or []):
            if isinstance(r, dict):
                result = _verify_ref(run_dir, r)
                result["event_seq"] = e.get("seq")
                ref_results.append(result)
    bad_refs = [r for r in ref_results if r["verify"] != "verified"]

    gaps = list(call_gaps)
    if not events:
        gaps.append("无 trace/ 事件流：事件级时间线 unrecorded（不回填）")
    for r in bad_refs:
        gaps.append(f"seq {r.get('event_seq')} 引用 {r.get('name', r.get('path', ''))}：{r['verify_detail']}")
    unverified_usage = any((not c.get("usage")) and c.get("ok") for c in calls_view)

    # P1.3：任务库摘要（只读；库与 JSON 导出摘要不一致时如实显示——库为规范态）
    task_db = {"present": False}
    try:
        from mentis.store import store_status
        task_db = store_status(run_dir)
    except Exception:
        task_db = {"present": False, "error": "读取异常"}

    return {
        "schema": PROJECTION_SCHEMA,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "kind": kind,
        "mode": "trace" if events else "historical",
        "goal": goal,
        "verdict": verdict if verdict is not None else _UNRECORDED,
        "verdict_basis": verdict_basis or _UNRECORDED,
        "contract_version": manifest.get("contract_version", _UNRECORDED) if manifest else _UNRECORDED,
        "integrity": integrity,
        "delegations": delegations if isinstance(delegations, list) else [],
        "counts": {
            "model_calls": model_call_count,
            "http_attempted": http_count,
            "executor_dispatches": executor_dispatch_count,
            "events": len(events),
        },
        "decisions": decisions,
        "decision_sources": decision_sources,
        "user_decisions": user_decisions,
        "model_calls": calls_view,
        "versions": versions,
        "professional": professional,
        "task_db": task_db,
        "ref_checks": ref_results,
        "gaps": gaps,
        "usage_note": ("存在 ok 调用但无用量记录：未知用量不写 0" if unverified_usage
                       else ""),
        "currency_note": "本投影不虚构货币成本；费用需另行按官方定价核算",
    }


# ---- Markdown 渲染 ----

def _fmt_call(c: dict) -> str:
    usage = c.get("usage")
    usage_text = ("；".join(f"{k}={v}" for k, v in sorted(usage.items()))
                  if isinstance(usage, dict) and usage else "未记录（未知用量，不写 0）")
    lines = [f"- `{c.get('request_id', _UNRECORDED)}` 模型 {c.get('model', _UNRECORDED)}"
             f"｜{c.get('created_at', _UNRECORDED)}｜耗时 {c.get('latency_ms', _UNRECORDED)}ms"
             f"｜Mock={'是' if c.get('mock') else '否'}｜HTTP 实发={'是' if c.get('http_attempted') else '否'}"
             f"｜ok={c.get('ok')}",
             f"  - 用量：{usage_text}；输入哈希 `{c.get('prompt_sha256', _UNRECORDED)}`"
             f"（模型输入全文未归档：unrecorded）"]
    if c.get("image_refs"):
        lines.append(f"  - 图像指纹：{'；'.join(c['image_refs'])}")
    if c.get("error"):
        lines.append(f"  - 错误：{c['error']}")
    stage = c.get("stage", _UNRECORDED)
    lines.append(f"  - 阶段关联：{stage}")
    if c.get("config_version"):
        lines.append(f"  - 配置关联：专业配置 v{c['config_version']}"
                     f"（sha256 前 12 位 {str(c.get('config_sha256', ''))[:12]}；"
                     f"职责 {c.get('stage_responsibility', '') or _UNRECORDED}）")
    for ref in c.get("output_refs", []) or []:
        name = Path(ref.get("path", "")).name if isinstance(ref, dict) else str(ref)
        lines.append(f"  - 产出引用：`{name}`（sha256 前 12 位 {str(ref.get('sha256', ''))[:12] if isinstance(ref, dict) else ''}）")
    return "\n".join(lines)


def render_projection_markdown(proj: dict) -> str:
    lines = [f"# 运行投影：{proj['run_id']}", ""]
    lines.append(f"- 类别：**{proj['kind']}**；模式：{proj['mode']}"
                 f"（{'有事件流，按 seq 回放' if proj['mode'] == 'trace' else '无 trace/，按现存材料清单展示'}）；"
                 f"契约 {proj['contract_version']}")
    lines.append(f"- 本文件是**只读投影**（{PROJECTION_SCHEMA}），可随时再生成；"
                 "不重算、不改变原业务判定；缺失标 unrecorded，无法核验标 unverified。")
    integrity = proj.get("integrity", {})
    if integrity:
        lines.append(f"- 追踪完整性 events_complete=**{integrity.get('events_complete')}**"
                     f"（写失败 {integrity.get('write_failures')}）"
                     + (f"；{integrity.get('note')}" if integrity.get("note") else ""))
    lines.append(f"- 记账：模型调用 {proj['counts']['model_calls']} 次｜其中 HTTP 实发 "
                 f"{proj['counts']['http_attempted']} 次｜执行器委托 "
                 f"{proj['counts']['executor_dispatches']} 次（三者分别计数）。"
                 f"{proj['currency_note']}。")
    if proj.get("usage_note"):
        lines.append(f"- {proj['usage_note']}。")
    lines += ["", f"## 目标与判定", "",
              f"- 目标：{proj['goal']}",
              f"- 判定：**{proj['verdict']}**——{proj['verdict_basis']}（原判定，投影不改写）", ""]

    lines += ["## 决策记录（模型提案 → 程序/用户处置）", ""]
    if proj["decision_sources"]:
        for s in proj["decision_sources"]:
            lines.append(f"- 来源：{s}")
    if proj["decisions"]:
        lines.append("")
        for d in proj["decisions"]:
            mapping = d.get("mapping", {})
            score = mapping.get("score", _UNRECORDED)
            lines.append(
                f"### {d.get('no')}（{d.get('decision')}｜处置主体：程序）")
            lines.append(f"- 模型提案（现象）：{d.get('phenomenon')}")
            lines.append(f"- 计划维度：{d.get('dimension') or '（未对应）'}"
                         f"；映射 {mapping.get('matched_by', _UNRECORDED)}"
                         f"（得分 {score}）")
            lines.append(f"- 程序处置与理由：{d.get('reason')}")
            lines.append(f"- 后续动作：{d.get('follow_up')}"
                         + (f"；复检：{d['recheck']}" if d.get('recheck') else ""))
            if d.get("evidence_refs"):
                lines.append(f"- 证据引用：{'; '.join(str(x) for x in d['evidence_refs'])}")
            lines.append("")
    else:
        lines.append("- 无候选级决策记录（unrecorded）。")
    if proj["user_decisions"]:
        lines.append("### 用户决定（区别于程序处置）")
        for u in proj["user_decisions"]:
            who = "请求用户决定" if u["kind"] == "requested" else "用户已回答"
            lines.append(f"- [seq {u['seq']}] {who}：{u['summary']}")
        lines.append("")

    lines += ["## 模型调用", ""]
    if proj["model_calls"]:
        for c in proj["model_calls"]:
            lines.append(_fmt_call(c))
        lines.append("")
    else:
        lines.append("- 无逐调用记录（unrecorded；见缺口清单）。")
        lines.append("")

    lines += ["## 执行器委托", ""]
    if proj["delegations"]:
        for d in proj["delegations"]:
            if not isinstance(d, dict):
                continue
            usage = d.get("usage")
            usage_text = ("；".join(f"{k}={v}" for k, v in sorted(usage.items()))
                          if isinstance(usage, dict) and usage else "未取得（不写 0）")
            lines.append(f"- {d.get('attempt_id', '?')}（brief v{d.get('brief_version', '?')}，"
                         f"{d.get('action', '') or '动作未记录'}）→ 终态 {d.get('terminal_status') or '未取得'}"
                         f"｜prompt {str(d.get('prompt_sha256', ''))[:12]}｜用量：{usage_text}")
    elif proj["counts"]["events"]:
        lines.append("- **零委托**：本运行没有发起任何执行器委托（真实行为，不是缺记录）。")
    else:
        lines.append("- 委托数 unrecorded：该运行无事件流，无法据现存材料确认是否委托（不回填）。")
    lines.append("")

    lines += ["## 版本与证据", ""]
    if proj["versions"]:
        for v in proj["versions"]:
            mark = {"verified": "已核验", _UNVERIFIED: "**unverified**"}.get(
                v["verify"], v["verify"])
            lines.append(f"- {v['label']}：`{Path(v['path']).name}`｜sha256 前 12 位 "
                         f"{v['sha256'][:12]}｜{mark}"
                         + (f"（{v['verify_detail']}）" if v.get("verify_detail") else ""))
    else:
        lines.append("- 无版本/证据文件（unrecorded）。")
    lines.append("")

    lines += ["## 专业配置（C1）", ""]
    prof = proj.get("professional") or {}
    if prof.get("status") == "unrecorded":
        lines.append(f"- {prof.get('note', '未采集')}。")
    else:
        cur = prof.get("current") or {}
        scope = cur.get("capability_scope", {}) if isinstance(cur.get("capability_scope"), dict) else {}
        lines.append(f"- 状态：**{prof['status']}**｜版本历史 v{'、v'.join(str(v) for v in prof['versions'])}"
                     f"｜绑定记录 {prof.get('bindings_count', 0)} 条（配置↔brief↔计划）")
        lines.append(f"- 当前 v{cur.get('version', _UNRECORDED)}（{cur.get('reason_kind', _UNRECORDED)}）："
                     f"{cur.get('reason_detail', '')}")
        if prof.get("plan_bindings"):
            pb = prof["plan_bindings"][-1]
            lines.append(f"- 计划关联：计划 v{pb.get('plan_version', _UNRECORDED)}"
                         f"（sha256 前 12 位 {str(pb.get('plan_sha256', ''))[:12] or _UNRECORDED}）")
        if prof.get("grounds"):
            lines.append("- 资源快照（内容已冻结于运行目录；复核状态按资源标注）：")
            for g in prof["grounds"]:
                lines.append(f"  - {g['resource_id']}@{g['version']}｜sha256 前 12 位 "
                             f"{g['sha256_12']}｜复核状态 {g['review_status']}")
        else:
            lines.append("- 资源快照：无（该任务未命中领域资源，如实记录）")
        lines.append(f"- 能力范围：有资料={scope.get('has_materials', _UNRECORDED)}｜"
                     f"可执行={scope.get('can_execute', _UNRECORDED)}｜"
                     f"可观察={scope.get('can_observe', _UNRECORDED)}｜"
                     f"已验证范围={scope.get('verified_scope', _UNRECORDED)}"
                     "（配置不授予专业资格；专业水平由作品与独立评价另行验证）")
        if prof.get("problems"):
            for p in prof["problems"]:
                lines.append(f"- **核验问题**：{p}")
        else:
            lines.append(f"- {prof.get('note', '')}")
    lines.append("")

    lines += ["## 缺口（unrecorded / unverified，不回填不解释）", ""]
    if proj["gaps"]:
        for g in proj["gaps"]:
            lines.append(f"- {g}")
    else:
        lines.append("- 无已发现缺口。")
    lines.append("")
    lines.append("## 入口")
    lines.append("")
    lines.append("- 交流与决策回放：`py -3.14 -X utf8 -B -m mentis conversation --run <运行目录>`")
    lines.append("- 事件时间线：`py -3.14 -X utf8 -B -m mentis trace --run <运行目录>`")
    lines.append("- 本投影：`py -3.14 -X utf8 -B -m mentis replay --run <运行目录>`")
    lines.append("")
    return "\n".join(lines)


# ---- O2：最后可靠状态（CLI / --follow / 页面共用；只读，不改业务判定） ----

def _parse_iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _load_run_budget(run_dir: Path, kind: str, state: dict) -> dict:
    """预算视图：区分已用/上限/剩余；缺任一项时剩余不计算（不虚构剩余额度）。
    runner-state 的 budget 为扁平形（max_*+used）；run-manifest 为嵌套形
    （limits+used），两者都按现存记录确定性读取。"""
    budget = {"limits": {}, "used": {}, "remaining": {}, "note": ""}
    if kind == "eval-run":
        raw = state.get("budget") if isinstance(state.get("budget"), dict) else {}
        limits = dict(raw.get("limits")) if isinstance(raw.get("limits"), dict) else {}
        used = dict(raw.get("used")) if isinstance(raw.get("used"), dict) else {}
        if not limits:
            limits = {k: v for k, v in raw.items()
                      if k in ("max_attempts", "max_revisions", "max_model_calls")
                      and isinstance(v, int)}
        if not limits or not used:
            manifest = _load_json(run_dir / "run-manifest.json")
            m_budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
            m_limits = m_budget.get("limits") if isinstance(m_budget.get("limits"), dict) else {}
            m_used = m_budget.get("used") if isinstance(m_budget.get("used"), dict) else {}
            limits = limits or {k: v for k, v in m_limits.items()
                                if k in ("attempts", "revisions", "model_calls")
                                and isinstance(v, int)}
            used = used or {k: v for k, v in m_used.items()
                            if k in ("attempts", "revisions", "model_calls")
                            and isinstance(v, int)}
        # 键名归一（max_attempts ↔ attempts），保持与用量台账同键
        limits = {k.replace("max_", ""): v for k, v in limits.items()}
        budget["limits"] = limits
        budget["used"] = used
        for key in ("attempts", "revisions", "model_calls"):
            if isinstance(limits.get(key), int) and isinstance(used.get(key), int):
                budget["remaining"][key] = max(0, limits[key] - used[key])
        if not limits:
            budget["note"] = "预算上限未采集；不虚构剩余额度"
    else:
        budget["note"] = "该运行未结构化记录预算上限/已用（unrecorded）；用量见模型调用与委托台账"
    return budget


def _finalized_info(events: list) -> dict:
    """收尾信息：区分「当前代次已收尾」与「旧代次收尾仍存于流中」。"""
    last_resumed_seq = -1
    last_finalized = None
    for e in events:
        if e.get("kind") == "run.resumed":
            last_resumed_seq = max(last_resumed_seq, int(e.get("seq", -1) or -1))
        if e.get("kind") == "run.finalized":
            last_finalized = e
    if last_finalized is None:
        return {"finalized": False, "in_current_generation": False,
                "outcome": "", "note": "未见收尾事件"}
    seq = int(last_finalized.get("seq", -1) or -1)
    in_current = seq > last_resumed_seq
    return {"finalized": True, "in_current_generation": in_current,
            "outcome": str(last_finalized.get("summary", ""))[:160],
            "time_utc": last_finalized.get("time_utc", ""),
            "note": ("当前代次已收尾" if in_current
                     else "仅旧代次收尾存在于流中；恢复后的新代次不得据此视为完成")}


def _load_task_db_view(run_dir: Path) -> dict:
    """P1.3：任务库只读摘要（status 入口显示；不一致如实标注，以库为规范态）。"""
    try:
        from mentis.store import store_status
        return store_status(run_dir)
    except Exception as exc:
        return {"present": False, "error": f"读取异常（{type(exc).__name__}）"}


def load_run_state(run_dir) -> dict:
    """最后可靠状态投影：阶段、等待、生产者活动、最近业务事件与预算。

    只读、确定性；缺失标 unrecorded，活动信号过期/损坏只说明「无法确认活跃」，
    不推断失败。读取不能写回运行材料；不重算业务判定。
    """
    run_dir = Path(run_dir)
    kind, state = detect_run_kind(run_dir)
    events = load_events(run_dir)
    stages = load_stages(run_dir)
    heartbeat = load_heartbeat(run_dir)
    generation = current_generation(run_dir)

    # 阶段视图：start/end 配对；未关闭阶段如实显示「进行中（未关闭）」
    stage_rows = []
    open_stage = None
    opened_at = {}
    for row in stages:
        if row.get("schema") == "corrupt_line":
            stage_rows.append({"stage": "corrupt_line", "phase": "corrupt",
                               "detail": row.get("detail", ""), "label": "记录损坏行"})
            continue
        name = str(row.get("stage", "unrecorded"))
        label = STAGE_LABELS.get(name, name)
        if row.get("phase") == "start":
            key = (name, row.get("instance"))
            opened_at[key] = row
            stage_rows.append({"stage": name, "label": label, "phase": "start",
                               "instance": row.get("instance"),
                               "time_utc": row.get("time_utc", ""),
                               "attempt_id": row.get("attempt_id") or "",
                               "detail": row.get("detail", ""),
                               "reason": row.get("reason", ""),
                               "interaction_id": str(row.get("interaction_id", "") or ""),
                               "blocking": row.get("blocking"),
                               "generation": row.get("generation")})
            open_stage = stage_rows[-1]
        elif row.get("phase") == "end":
            key = (name, row.get("instance"))
            start_row = opened_at.pop(key, None)
            duration = row.get("duration_s")
            if isinstance(duration, (int, float)) and duration < 0:
                duration = None  # 时钟异常不显示负耗时
            stage_rows.append({"stage": name, "label": label, "phase": "end",
                               "instance": row.get("instance"),
                               "time_utc": row.get("time_utc", ""),
                               "status": row.get("status", "unrecorded"),
                               "duration_s": duration,
                               "detail": row.get("detail", ""),
                               "generation": row.get("generation")})
            if open_stage is not None and open_stage.get("stage") == name \
                    and open_stage.get("instance") == row.get("instance"):
                open_stage = None
    if open_stage is not None:
        open_stage["open"] = True

    # 等待：未回答的 decision.requested（用户决定只阻塞依赖动作）。
    # 审查修正 A：请求与答复按 interaction_id（或历史摘要中可确定性解析的 IX-*）
    # 关联——答复只关闭对应决定；无法关联的回答记录只标缺口（不臆造、不清空）。
    requested: dict = {}
    unmatched_answers = 0
    for e in events:
        ekind = e.get("kind")
        if ekind == "decision.requested":
            key = _decision_key(e) or f"unlinked-seq-{e.get('seq', '?')}"
            if key in requested:
                continue
            identified = _decision_key(e) is not None
            requested[key] = {
                "seq": e.get("seq"), "time_utc": e.get("time_utc", ""),
                "reason": str(e.get("summary", ""))[:200],
                "blocking": True,
                "depends": "依赖该决定的工作暂停；通知类消息不阻塞",
                "answered": False,
                "interaction_id": _decision_key(e) or "",
                "identified": identified,
                "source": "decision.requested 事件（按现存记录确定性读取）",
            }
        elif ekind in ("decision.answered", "decision.received"):
            key = _decision_key(e)
            if key and key in requested:
                requested[key]["answered"] = True
            else:
                unmatched_answers += 1
    open_waits = [row for row in requested.values() if not row["answered"]]
    for row in stage_rows:
        if row.get("stage") == "waiting_user" and row.get("phase") == "start" and row.get("open"):
            # 事件与阶段不重复计数：同一决定的等待已在事件行出现时不再追加；
            # 已回答的决定对应的未关闭历史等待行按回答记录如实闭合（不修改文件）。
            row_text = str(row.get("reason", "")) + " " + str(row.get("detail", ""))
            m = _IX_RE.search(row_text)
            stage_key = (str(row.get("interaction_id", "") or "").strip().lower()
                         or (m.group(1).lower() if m else ""))
            if stage_key and stage_key in requested:
                # 已回答→不再列为待决；未回答→事件行已代表该等待（不重复追加）
                continue
            open_waits.append({
                "seq": "", "time_utc": row.get("time_utc", ""),
                "reason": row.get("reason") or row.get("detail") or "（原因未记录）",
                "blocking": row.get("blocking", True),
                "depends": "依赖该决定的工作暂停；通知类消息不阻塞",
                "answered": False,
                "interaction_id": stage_key,
                "identified": bool(stage_key),
                "source": "阶段记录（waiting_user 未关闭）",
            })
    gap_note = (f"存在 {unmatched_answers} 条回答/接收记录无法关联到具体决定请求"
                "（缺关联标识或对应请求不在流中）：待决事项按未回答保留，不臆造对应"
                if unmatched_answers else "")

    last_event = events[-1] if events else {}
    last_collected = max(
        [str(e.get("time_utc", "")) for e in events if e.get("time_utc")]
        + [str(r.get("time_utc", "")) for r in stages if r.get("time_utc")],
        default="")
    hb_time = str(heartbeat.get("time_utc", "")) if heartbeat.get("read") == "ok" else ""
    if hb_time > last_collected:
        last_collected = hb_time

    activity_category, activity_line = activity_status(run_dir)
    hb_generation = heartbeat.get("generation") if heartbeat.get("read") == "ok" else None

    manifest = _load_json(run_dir / "trace" / "manifest.json")
    integrity = manifest.get("integrity", {}) if isinstance(manifest.get("integrity"), dict) else {}
    finalized = _finalized_info(events)

    sidecar_calls = [c for c in load_model_calls(run_dir) if c.get("schema") != "corrupt_line"]
    if not sidecar_calls and kind == "eval-run":
        # 历史运行无 sidecar：按 runner-state.model_requests 降级计数（字段缺失另标）
        recorded = state.get("model_requests") if isinstance(state.get("model_requests"), list) else []
        sidecar_calls = [c for c in recorded if isinstance(c, dict)]
    counts = {
        "events": len(events),
        "executor_dispatches": sum(1 for e in events
                                   if e.get("kind") == "delegation.prepared"
                                   and e.get("status") == "ok"),
        "model_calls": len(sidecar_calls),
        "http_attempted": len([c for c in sidecar_calls if c.get("http_attempted")]),
    }

    verdict = ""
    if kind == "eval-run" and isinstance(state.get("summary"), dict):
        verdict = str(state.get("summary", {}).get("verdict", "") or "")

    notes = []
    if gap_note:
        notes.append(gap_note)
    corrupt_stages = sum(1 for r in stage_rows if r.get("phase") == "corrupt")
    if corrupt_stages:
        notes.append(f"阶段记录含 {corrupt_stages} 条损坏行：实时状态不可据此声称可靠")
    if heartbeat.get("read") == "corrupt":
        notes.append(heartbeat.get("note", "活动信号损坏"))
    if integrity and integrity.get("write_failures"):
        notes.append(f"追踪写失败 {integrity.get('write_failures')} 次：记录完整性受影响")

    return {
        "schema": RUN_STATE_SCHEMA,
        "run_id": run_dir.name,
        "kind": kind,
        "generation": generation if generation else None,
        "generation_note": "" if generation else "代次未采集（无事件流；历史运行不回填）",
        "stages": stage_rows,
        "current_stage": open_stage,
        "open_waits": open_waits,
        "last_event": ({"seq": last_event.get("seq"), "kind": last_event.get("kind"),
                        "time_utc": last_event.get("time_utc", ""),
                        "summary": str(last_event.get("summary", ""))[:160]} if events else {}),
        "last_collected": last_collected or "unrecorded",
        "activity": {
            "category": activity_category,
            "line": activity_line,
            "time_utc": hb_time or "unrecorded",
            "ttl_s": heartbeat.get("ttl_s") if heartbeat.get("read") == "ok" else None,
            "producer": heartbeat.get("producer") if heartbeat.get("read") == "ok" else None,
            "source": heartbeat.get("source", "unrecorded") if heartbeat.get("read") == "ok" else "unrecorded",
            "generation": hb_generation,
            "generation_matches": (None if hb_generation is None or not generation
                                   else hb_generation == generation),
        },
        "finalized": finalized,
        "verdict": verdict or "unrecorded",
        "budget": _load_run_budget(run_dir, kind, state if isinstance(state, dict) else {}),
        "professional": load_professional_view(run_dir),
        "task_db": _load_task_db_view(run_dir),
        "counts": counts,
        "integrity": integrity,
        "notes": notes,
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_note": "read_at 是读取方时钟（旁观/CLI/页面各自记录），不代表生产者活动",
    }


def render_state_markdown(state: dict) -> str:
    lines = [f"# 运行状态：{state['run_id']}", ""]
    gen = state["generation"]
    lines.append(f"- 代次：**{gen if gen else '未采集'}**；类别 {state['kind']}；"
                 f"判定 {state['verdict']}（原判定，状态视图不改写）")
    finalized = state["finalized"]
    if finalized["finalized"]:
        mark = ("当前代次已收尾" if finalized["in_current_generation"]
                else "**仅旧代次收尾存在于流中（恢复后的新代次不据此视为完成）**")
        lines.append(f"- 收尾：{mark}｜{finalized.get('outcome', '')[:120]}")
    else:
        lines.append(f"- 收尾：{finalized['note']}（运行进行中或收尾未记录）")
    current = state.get("current_stage")
    if current is not None:
        lines.append(f"- 当前阶段：**{current.get('label')}**（实例 {current.get('instance')}，"
                     f"自 {current.get('time_utc', '?')} 起，未关闭）"
                     + (f"；{current.get('detail')}" if current.get("detail") else ""))
    else:
        finished = [r for r in state["stages"] if r.get("phase") == "end"]
        if finished:
            last = finished[-1]
            duration = last.get("duration_s")
            lines.append(f"- 当前阶段：无进行中阶段；最近结束 {last.get('label')}"
                         f"（{last.get('status')}，"
                         f"耗时 {f'{duration:.1f}s' if isinstance(duration, (int, float)) else '未记录'}）")
        else:
            lines.append("- 当前阶段：unrecorded（无阶段记录；O2 之前的运行不回填）")
    for wait in state["open_waits"]:
        ident = "" if wait.get("identified", True) else "（关联标识未采集）"
        lines.append(f"- **等待用户决定**{ident}（自 {wait['time_utc']}）：{wait['reason']}；"
                     f"{wait['depends']}")
    if not state["open_waits"]:
        lines.append("- 等待：无未决用户决定记录（通知类消息不阻塞，不在此列）")
    activity = state["activity"]
    lines.append(f"- 生产者活动：[{activity['category']}] {activity['line']}")
    if activity.get("generation_matches") is False:
        lines.append("  - 注意：活动信号属于旧代次，不代表当前生产者。")
    if state["last_event"]:
        lines.append(f"- 最近事件：[{state['last_event'].get('seq', '未采集')}] "
                     f"{state['last_event'].get('kind', '未采集')}"
                     f" {state['last_event'].get('time_utc', '')}｜{state['last_event'].get('summary', '')}")
    else:
        lines.append("- 最近事件：unrecorded")
    lines.append(f"- 最后采集时刻：{state['last_collected']}；读取方时刻（仅表示读取）：{state['read_at']}")
    counts = state["counts"]
    lines.append(f"- 记账：模型调用 {counts['model_calls']}｜HTTP 实发 {counts['http_attempted']}｜"
                 f"执行器委托 {counts['executor_dispatches']}（分别计数；未知不填 0）")
    budget = state["budget"]
    if budget["limits"]:
        parts = []
        for key, label in (("attempts", "委托"), ("revisions", "修订"), ("model_calls", "模型调用")):
            used = budget["used"].get(key)
            limit = budget["limits"].get(key)
            remaining = budget["remaining"].get(key)
            parts.append(f"{label} {used if used is not None else '未记录'}/"
                         f"{limit if limit is not None else '未记录'}"
                         + (f"（余 {remaining}）" if remaining is not None else ""))
        lines.append(f"- 预算：{'；'.join(parts)}" + (f"；{budget['note']}" if budget["note"] else ""))
    else:
        lines.append(f"- 预算：{budget['note']}")
    prof = state.get("professional") or {}
    if prof.get("status") == "unrecorded":
        lines.append(f"- 专业配置：未采集（历史运行不回填）")
    elif prof.get("current"):
        cur = prof["current"]
        lines.append(f"- 专业配置：v{cur.get('version', '未采集')}"
                     f"（{prof['status']}；绑定 {prof.get('bindings_count', 0)} 条；"
                     f"资源快照 {len(prof.get('grounds', []))} 项）"
                     + (f"；核验问题 {len(prof['problems'])} 项" if prof.get("problems") else ""))
    tdb = state.get("task_db") or {}
    if tdb.get("present"):
        if tdb.get("error"):
            lines.append(f"- 任务库：存在但不可读（{tdb['error']}）——恢复需先核对，不以 JSON 导出静默替代")
        else:
            lines.append(f"- 任务库：{tdb.get('path', 'state.sqlite3')}"
                         f"（mentis-task-store/1 v{tdb.get('db_version', '?')}；"
                         f"摘要 {tdb.get('state_digest', '') or '未记录'}）"
                         + ("；与 JSON 导出摘要不一致（以库为准）" if tdb.get("export_mismatch") else ""))
    else:
        lines.append("- 任务库：未创建（collab 运行本轮起落盘创建；历史 collab 运行下次"
                     "落盘/迁移时创建，恢复暂走 JSON 导出路径；eval/guidance 沿用各自 JSON 状态）")
    for note in state["notes"]:
        lines.append(f"- 注意：{note}")
    lines.append("")
    lines.append("> 本状态视图只读、可再生成；活动信号过期只表示无法确认生产者活跃，"
                 "不代表任务失败、不解锁、不自动重发。")
    lines.append("")
    return "\n".join(lines)
