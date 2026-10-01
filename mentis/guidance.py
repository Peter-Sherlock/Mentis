"""P3.2 最小真实指导与修订闭环。

回答一个问题：Mentis 能否凭自己可获得的证据，给执行器一条有效的专业修改指令，
使同一作品的可观察质量提高，同时保住原有要求。

流程：冻结首稿 → 真实观察（结构 + 渲染测量 + 真实视口截图）→ 模型诊断（含图像输入）
→ 组装修改指令（证据与保留项来自冻结材料，不含开发者代答）→ 真实 Codex 修订
→ 独立复检（测量/结构/保留项 + 模型复查）→ 判定改善/无改善/回归。

纪律：
- 缺陷认定与修改内容来自模型诊断与机器测量；开发者不在运行时手写"修什么"的答案。
- 自动修改最多 max_rounds 轮；超时先核对（无活跃写入者才继续），不盲目重试，
  产物核对路径明确标注，不冒充执行器自报完成。
- 修复一个问题但破坏保留项 = 回归，不得标为改善。
- 无货币硬上限：仅记录调用次数、时长与可取得的 token 用量。
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mentis.capabilities.routing import Gap, route_gap
from mentis.capabilities.selection import select_for_task
from mentis.contracts import (AttemptStatus, DelegationPolicy, Disposition, Provenance,
                              SourcedItem)
from mentis.diagnosis import (DiagnosisBundle, render_bundle_markdown, run_diagnosis)
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec
from mentis.models.gateway import ModelConfig, ModelGateway
from mentis.observation import ObservationRecord, observe_structure
from mentis.persist import save_session
from mentis.render_measurement import measure_page
from mentis.session import TaskSession
from mentis.trace import TraceArchiveError, TraceWriter

GUIDANCE_DIR = Path("runtime") / "guidance"
STATE_VERSION = "p3.2"
# trace 契约 §7 状态映射：执行器终态 → 事件 status
STATUS_VALUE = {"completed": "ok", "failed": "failed", "unknown": "unknown",
                "dispatched": "pending", "running": "pending"}

GUIDANCE_SYSTEM_ADDENDUM = (
    "你将收到双视口（1280×800 与 390×844）真实截图作为图像输入：视觉判断依据截图，"
    "数值判断依据渲染测量记录；两者冲突时以测量数值为准并明确说明。"
    "渲染测量已提供横向溢出数值、容器内裁剪与表单声明行为，这些方面不得再请求用户决定。"
)

# 决策请求分流（确定性规则，理由落盘）：
# 问句同时含"变更意图动词 + 保留事项名词"，或要求素材外新事实 → 保留决定，暂停依赖动作；
# 其余（呈现顺序、密度等可逆专业取舍）→ Mentis 自主决定，不转问用户（ADR-002 + P2.2 教训：
# 字段完整的请求仍可能不必要）。
_CHANGE_VERBS = ("改", "变更", "换成", "调整为", "取消", "延长", "缩短", "增加", "移除",
                 "纳入", "新增", "延期", "提前", "推迟")
_RESERVED_NOUNS = ("日期", "时间", "价格", "费用", "地点", "退款", "发布", "上线", "部署",
                   "账号", "密钥", "隐私", "报名费")
_NEW_FACT_HINTS = ("是否要", "是否需要", "有没有", "是否存在")
AUTONOMOUS_DECISION_TEXT = (
    "保持当前内容与顺序（最小化改动）；如修复已确认缺陷时自然涉及，"
    "允许以不删除内容、不重排信息的方式微调尺寸与间距。依据：冻结偏好「温暖、简洁」；"
    "该取舍可逆、属常规视觉专业范围（ADR-002 默认自主），用户可随时以反馈覆盖。")


@dataclass
class ObservationPack:
    """结构观察 + 渲染测量的合并视图，供诊断证据引用核对。"""

    structure: ObservationRecord
    measurement: object
    image_names: list = field(default_factory=list)

    @property
    def source_path(self) -> str:
        return self.structure.source_path

    @property
    def sha256(self) -> str:
        return self.structure.sha256

    def item_ids(self) -> set:
        return self.structure.item_ids() | self.measurement.item_ids()

    def render_text(self) -> str:
        parts = [self.structure.render_text(), "", self.measurement.render_text()]
        if self.image_names:
            parts.append("[图像输入] 本次诊断同时收到以下截图（真实视口渲染）："
                         + "、".join(self.image_names))
        else:
            parts.append("[图像输入] 本次诊断未收到任何截图；视觉判断不可用，不得声称看过画面")
        return "\n".join(parts)


@dataclass
class RoundRecord:
    round_no: int
    instruction: str = ""
    instruction_path: str = ""
    diagnosis_path: str = ""
    review_path: str = ""
    codex_status: str = ""
    codex_duration_s: float = 0.0
    codex_usage: dict = field(default_factory=dict)
    codex_thread_id: str = ""
    codex_events_path: str = ""
    codex_error: str = ""
    outcome: str = ""          # completed / reconciled_from_artifacts / failed / needs_human
    verdict: str = ""          # improved / partially_improved / not_improved / regression
    verification: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def codex_processes_since(since_iso: str) -> list:
    """列出晚于 since 启动的 codex 进程（判断是否仍有执行器在写入）。"""
    script = (
        "Get-Process codex -ErrorAction SilentlyContinue | "
        "Where-Object { $_.StartTime -gt [datetime]::Parse('%s') } | "
        "Select-Object Id,StartTime | ConvertTo-Json -Compress" % since_iso.replace("T", " ")
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, timeout=30)
    out = (proc.stdout or "").strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def freeze_baseline(baseline_dir: str | Path, run_dir: str | Path) -> dict:
    """把 P2.3 运行的首稿与其素材复制冻结到本轮 v1；原运行目录不修改。"""
    baseline = Path(baseline_dir)
    source_html = baseline / "workspace" / "index.html"
    if not source_html.is_file():
        raise ValueError(f"基线运行目录缺少 workspace/index.html：{baseline}")
    v1 = Path(run_dir) / "v1"
    (v1 / "source").mkdir(parents=True, exist_ok=True)
    legacy = Path(run_dir) / "legacy-screenshots"
    legacy.mkdir(parents=True, exist_ok=True)
    import hashlib

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    shutil.copy2(source_html, v1 / "index.html")
    copied = {"index.html": sha(source_html)}
    for rel in ("workspace/brief.md", "workspace/input/copy.md", "state.json"):
        src = baseline / rel
        if src.is_file():
            dest = v1 / "source" / rel.replace("/", "_")
            shutil.copy2(src, dest)
            copied[rel] = sha(src)
    for shot in ("desktop.png", "mobile.png"):
        src = baseline / "observation" / shot
        if src.is_file():
            shutil.copy2(src, legacy / shot)
            copied[f"observation/{shot}"] = sha(src)
    freeze = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_dir": str(baseline.resolve()),
        "v1_html_sha256": sha(v1 / "index.html"),
        "copied": copied,
        "note": "legacy-screenshots 为 P2.3 原始截图（受 500px 钳制假象影响，仅留证）；"
                "本轮观察使用修复后的真实视口截图",
    }
    (Path(run_dir) / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8")
    return freeze


def parse_frozen_brief(v1_dir: Path) -> tuple[str, list[str]]:
    """从冻结的 brief.md 取目标与约束行（不重新解读，只搬运）。"""
    text = (v1_dir / "source" / "workspace_brief.md").read_text(encoding="utf-8")
    goal, constraints = "", []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            section = stripped[2:].strip()
            continue
        if section == "目标":
            goal = (goal + " " + stripped).strip()
        elif section == "约束与偏好" and stripped.startswith("- "):
            constraints.append(stripped[2:].strip())
    if not goal:
        raise ValueError("冻结 brief.md 缺少 '# 目标' 段")
    return goal, constraints


def preserved_items(v1_dir: Path) -> list:
    """从冻结素材（copy deck）提取必须保留的活动事实：[(标签, 校验子串)]。

    全部由素材文件内容推导，不含开发者硬编码的活动信息。
    """
    deck = (v1_dir / "source" / "workspace_input_copy.md").read_text(encoding="utf-8")
    items = []
    for line in deck.splitlines():
        stripped = line.strip()
        match = re.match(r"^-\s*([^：]{1,12})：(.+)$", stripped)
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2).strip()
        if label in ("氛围要求", "常见问题") or value.startswith(("1.", "2.", "3.")):
            continue
        if label == "日期时间":
            date = re.search(r"\d{4}-\d{2}-\d{2}", value)
            times = re.findall(r"\d{1,2}:\d{2}", value)
            if date:
                items.append(("活动日期", date.group(0)))
            for t in times[:2]:
                items.append(("活动时间", t))
        elif label == "价格":
            price = re.search(r"¥\s*\d+", value)
            if price:
                items.append(("价格", price.group(0).replace(" ", "")))
        elif label == "地点":
            tokens = [t for t in re.split(r"[·（）、,，\s]+", value) if len(t) >= 3]
            for token in tokens[:2]:
                items.append((f"地点·{token}", token))
        elif label in ("活动名称", "讲师"):
            name = re.sub(r"（[^）]*）", "", value).strip()
            if name:
                items.append((label, name))
    return items


class GuidanceCycle:
    def __init__(self, baseline_dir: str | Path, run_dir: str | Path = "",
                 adapter=None, gateway=None, browser_capture=None, measure=None,
                 max_rounds: int = 2, timeout_s: int = 600, use_model: bool = True,
                 include_images: bool = True, run_id: str = ""):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + os.urandom(2).hex()
        self.baseline_dir = Path(baseline_dir)
        self.run_dir = Path(run_dir) if run_dir else GUIDANCE_DIR / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.max_rounds = max(1, int(max_rounds))
        self.timeout_s = min(600, max(1, int(timeout_s)))
        self.use_model = use_model
        self.include_images = include_images
        self.adapter = adapter or CodexCliAdapter(executable=_resolve_codex())
        self.gateway = gateway if gateway is not None else ModelGateway(config=ModelConfig())
        if browser_capture is not None:
            self.browser_capture = browser_capture
        elif include_images:
            from mentis.browser_observation import capture_local_html

            self.browser_capture = capture_local_html
        else:
            self.browser_capture = None
        self.measure = measure or measure_page
        self.session = TaskSession(goal="")
        self.rounds: list[RoundRecord] = []
        self.decision_routes: list[dict] = []
        self.thread_id = ""
        self.status = "created"
        self.verdict = ""
        self.workspace = self.run_dir / "revision-workspace"
        self._v1_dir = self.run_dir / "v1"
        self._preserved: list = []
        self._dropped_preserved: list = []
        self._v1_structure = None
        self._form_field_requirements: list = []
        self._constraints: list[str] = []
        self._goal = ""
        self._workspace_ready = False
        self._finalized_emitted = False
        self.trace = TraceWriter(self.run_dir, self.run_id,
                                 executor_version=self.adapter.cli_version)

    # ---- 流程 ----

    def run(self) -> str:
        """指导修订循环（意外异常同样收尾落盘，见 _finalize_on_error）。"""
        try:
            return self._run_impl()
        except Exception as exc:
            self._finalize_on_error(exc)
            raise

    def _finalize_on_error(self, exc: Exception) -> None:
        """T3：意外异常也如实收尾——无终态尝试改记 unknown（需核对，不判失败），
        run.finalized 记异常原文，状态落盘为 ERROR；既有执行器终态与事件不覆盖、
        不重复判定。收尾自身失败不掩盖原始异常（随后向调用方重抛）。"""
        try:
            for attempt in self.session.attempts:
                if attempt.status.value in ("dispatched", "running"):
                    self.session.mark_unknown(attempt.attempt_id)
                    self._notify(f"异常收尾：尝试 {attempt.attempt_id} 无执行器终态，"
                                 "改记 unknown（需核对；不判失败、不自动重发）")
            self._notify(f"运行异常终止：{type(exc).__name__}：{str(exc)[:160]}；"
                         "已按 ERROR 收尾落盘，可从 state.json 恢复")
            self.status = "ERROR"
            self._finalized_emitted = True
            self.trace.emit("mentis", "run.finalized",
                            f"运行异常终止：{type(exc).__name__}：{str(exc)[:200]}"
                            "（无终态尝试已记 unknown；已记录的执行器终态与事件不变）",
                            brief_version=self.session.current_brief.version,
                            status="failed")
            self._persist()
        except Exception:
            pass

    def _run_impl(self) -> str:
        self._notify(f"P3.2 指导修订循环开始；基线 {self.baseline_dir}")
        self._freeze_and_setup()
        extra_problems: list[str] = []
        for round_no in range(1, self.max_rounds + 1):
            record = RoundRecord(round_no=round_no)
            self.rounds.append(record)
            tag = f"round{round_no}"
            page = self._v1_dir / "index.html" if round_no == 1 else self._current_html()
            pack, images = self._observe(page, tag)
            bundle = self._diagnose(pack, images, tag, record)
            if bundle is None:
                self.status = "NEEDS_HUMAN_NO_MODEL"
                self._persist()
                return self.status
            if not bundle.valid_diagnoses:
                # 合格不改：无有效诊断就不委托修改（不为改而改）
                record.verdict = "qualified_no_change"
                self.verdict = "qualified_no_change"
                self.status = "QUALIFIED_NO_CHANGE"
                self._notify("诊断未发现需要修改的有效问题：合格不乱改，结束")
                self._persist()
                return self.status
            paused, agent_decisions = self._handle_decision_requests(bundle, record)
            if paused:
                self.status = "NEEDS_DECISION"
                self._notify("存在等待用户的保留决定：修订委托暂停，其余记录照常（沉默≠批准）")
                self._persist()
                return self.status
            defect_ids = [f"d{i}" for i in range(1, len(bundle.valid_diagnoses) + 1)]
            record.instruction = compose_revision_instruction(
                bundle, defect_ids, pack, self._preserved, self._constraints,
                agent_decisions=agent_decisions, extra_problems=extra_problems)
            record.instruction_path = str(self.run_dir / f"instruction-{tag}.md")
            (self.run_dir / f"instruction-{tag}.md").write_text(
                record.instruction, encoding="utf-8")
            outcome = self._delegate_revision(record, tag)
            if outcome == "failed":
                self.status = "FAILED"
                self.verdict = "failed"
                self._persist()
                return self.status
            if outcome == "needs_human":
                self.status = "NEEDS_HUMAN"
                self._persist()
                return self.status
            v2_html = self._current_html()
            if not v2_html.is_file():
                record.verdict = "failed"
                self.status = "FAILED"
                self._notify("修订后工作区没有 index.html：按失败处理，不虚构产物")
                self._persist()
                return self.status
            verdict = self._verify_round(record, tag, v2_html, bundle, defect_ids)
            record.verdict = verdict
            self._notify(f"第 {round_no} 轮判定：{verdict}")
            if verdict == "improved":
                self._save_version(v2_html, "v2")
                self.status = "IMPROVED"
                self.verdict = "improved"
                try:
                    self.session.accept(self.session.attempts[-1].attempt_id)
                except Exception as exc:
                    self._notify(f"接受记录被拒绝：{exc}")
                self._persist()
                return self.status
            if verdict == "unverified":
                self._save_version(v2_html, "v2")
                self.status = "UNVERIFIED"
                self.verdict = "unverified"
                self._notify("复检存在未知（测量不可信/缺记录/缺复查截图）："
                             "不判通过也不判失败；需先核对观察链，不消耗修订预算")
                self._persist()
                return self.status
            if round_no == self.max_rounds:
                self._save_version(v2_html, "v2")
                self.status = verdict.upper()
                self.verdict = verdict
                self._notify("自动修改预算已用完：如实记录最终判定（"
                             + ("保留项被破坏即回归，不标改善" if verdict == "regression"
                                else "未达成改善") + "）")
                self._persist()
                return self.status
            # 预算内再修一轮：复检发现的回归/未解决项进入下一轮指令
            extra_problems = (
                [f"回归：{r}" for r in record.verification.get("preserved_regression", [])]
                + [f"上一轮诊断 {u} 未解决" for u in record.verification.get("model_unresolved", [])]
                + [f"新问题：{n}" for n in record.verification.get("model_new_issues", [])])
        self.status = "EXHAUSTED"
        self._persist()
        return self.status

    # ---- 步骤 ----

    def _freeze_and_setup(self):
        freeze_baseline(self.baseline_dir, self.run_dir)
        goal, constraints = parse_frozen_brief(self._v1_dir)
        self._goal, self._constraints = goal, constraints
        self._ensure_v1_reference(force=True)
        s = self.session
        s.goal = goal
        s.init_brief(goal)
        for text in constraints:
            s.current_brief.constraints.append(SourcedItem(
                text, Provenance.AGENT_SELECTED, f"frozen:{self.baseline_dir.name}"))
        s.set_policy(DelegationPolicy(
            policy_id="policy-1", version=1,
            decided_scope=["常规视觉与布局修改", "文案措辞微调", "结构修正的实现方式"],
            reserved_decisions=["活动日期/地点/价格变更", "对外发布或部署", "超出工作区的授权"],
            execution_scopes=["exec-workspace"],
            budget={"max_auto_revisions": self.max_rounds},
            intervention_triggers=["诊断请求用户独有决定", "修订造成保留项回归"],
            provenance=Provenance.AGENT_SELECTED,
            source_ref="derived:frozen-brief + ADR-002",
        ))
        s.authorize("exec-workspace", granted_by="user-cli-invocation(p3.2)")
        self.trace.emit("mentis", "goal.recorded", f"{goal}｜约束来自冻结基线 {self.baseline_dir.name}",
                        brief_version=self.session.current_brief.version)
        self.trace.emit("mentis", "policy.set",
                        "代决范围：" + "；".join(s.current_policy.decided_scope)
                        + "；保留决定：" + "；".join(s.current_policy.reserved_decisions),
                        brief_version=self.session.current_brief.version)
        self.trace.emit("mentis", "authorization.granted", "exec-workspace（user-cli-invocation）",
                        brief_version=self.session.current_brief.version)
        self._notify(f"已冻结 v1（sha256 前 12 位 {freeze_baseline_sha(self._v1_dir)}）；"
                     f"保留项 {len(self._preserved)} 项：{', '.join(l for l, _ in self._preserved)}")

    def _ensure_v1_reference(self, force: bool = False):
        """从冻结 v1 派生核对基准：保留项（逐段校准）与表单逐字段必填要求。

        校准不降级：整句跨 DOM 节点时按"实际出现在 v1 的每一段"逐项核对，
        而不是只保留第一段；v1 中从未出现的段落不作为回归判据（如实丢弃并记录）。
        """
        if getattr(self, "_v1_structure", None) is not None and not force:
            return
        self._preserved = preserved_items(self._v1_dir)
        self._calibrate_preserved()
        self._form_field_requirements = sorted({
            i.text for i in self._v1_structure.items
            if i.kind in ("input", "textarea") and i.extra.get("required") == "yes"
        })

    def _calibrate_preserved(self):
        """保留项核对串按 v1 实际呈现逐段校准；无法核对者不作为判据（记录在案）。"""
        self._v1_structure = observe_structure(str(self._v1_dir / "index.html"))
        blob = "\n".join(i.text for i in self._v1_structure.items)
        calibrated, dropped = [], []
        for label, needle in self._preserved:
            if needle in blob:
                calibrated.append((label, needle))
                continue
            segments = [p for p in re.split(r"[，。；、,;·\s（）]+", needle) if len(p.strip()) >= 2]
            matched = [s for s in segments if s in blob]
            missing = [s for s in segments if s not in blob]
            if matched:
                multiple = len(matched) > 1
                for idx, seg in enumerate(matched, 1):
                    sub_label = f"{label}·{idx}" if multiple else label
                    calibrated.append((sub_label, seg))
                    self._notify(f"保留项校准：{label}「{needle}」→ 逐段核对「{seg}」"
                                 f"（v1 实际呈现粒度；每段独立判回归）")
                for seg in missing:
                    dropped.append((f"{label}（v1 未呈现）", seg))
                    self._notify(f"保留项 {label} 的段落「{seg}」在 v1 中未出现："
                                 "不作为回归判据（v1 本未呈现的要求不能算修订造成的回归）")
            else:
                dropped.append((label, needle))
                self._notify(f"保留项 {label}（{needle}）在 v1 中不可核对：不作为回归判据（仍写入指令供保留）")
        self._preserved = calibrated
        self._dropped_preserved = dropped

    def _observe(self, page: Path, tag: str) -> tuple[ObservationPack, list]:
        out = self.run_dir / f"observation-{tag}"
        structure = observe_structure(str(page))
        measurement = self.measure(page, out)
        images, image_names = [], []
        if self.include_images and self.browser_capture is not None:
            try:
                browser_obs = self.browser_capture(page, out)
                for view in browser_obs.views:
                    if view.screenshot_path and not view.error:
                        blob = Path(view.screenshot_path).read_bytes()
                        images.append((view.name + ".png", blob))
                        image_names.append(f"{view.name}.png（{view.actual_width}×{view.actual_height}）")
            except Exception as exc:
                self._notify(f"截图采集失败（{type(exc).__name__}）：诊断将不声称看过画面")
        (out / "measurement.json").write_text(
            json.dumps(_measurement_to_dict(measurement), ensure_ascii=False, indent=2),
            encoding="utf-8")
        pack = ObservationPack(structure=structure, measurement=measurement,
                               image_names=image_names)
        self.trace.emit("observer", "observation.structure",
                        f"结构观察 {len(structure.items)} 条目（sha256 前 12 位 {structure.sha256[:12]}）",
                        refs=[{"path": str(page), "sha256": structure.sha256, "role": "observation"}])
        self.trace.emit("observer", "observation.render",
                        f"渲染测量：{'; '.join(f'{v.name} 溢出 {v.overflow_px}px/异常 {len(v.errors)}' for v in measurement.viewports)}",
                        refs=[{"path": str(out / "measurement.json"),
                               "sha256": _sha256_file(out / "measurement.json"),
                               "role": "observation"}])
        return pack, images

    def _diagnose(self, pack: ObservationPack, images: list, tag: str,
                  record: RoundRecord) -> DiagnosisBundle | None:
        if not self.use_model or not self.gateway.has_credentials():
            self._notify("模型不可用：无法生成诊断；不凭空给出修改指令")
            return None
        context, _ = select_for_task(
            _load_registry(), task_label="页面诊断与修改指导",
            task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
            observation_capabilities={"structure_only": True, "screenshots": bool(images),
                                      "render_measurement": True},
            can_execute=True,
        )
        system = _diagnosis_system_prompt()
        bundle = run_diagnosis(self.gateway, self._goal, "", pack, context,
                               images=images, system=system)
        origin = "Mock 固定响应" if bundle.mock else f"真实模型（{bundle.model}）"
        image_note = ""
        if getattr(self.gateway, "calls", None):
            refs = getattr(self.gateway.calls[-1], "image_refs", [])
            if refs:
                image_note = f"\n- 图像输入：{';'.join(refs)}（真实截图，随诊断请求发送）"
        record.diagnosis_path = str(self.run_dir / f"diagnosis-{tag}.md")
        (self.run_dir / f"diagnosis-{tag}.md").write_text(
            render_bundle_markdown(bundle) + image_note, encoding="utf-8")
        self.trace.emit("mentis", "observation.diagnosis",
                        f"诊断（{'Mock' if bundle.mock else '真实模型'}）：有效 "
                        f"{len(bundle.valid_diagnoses)}、拒收 {len(bundle.invalid_diagnoses)}、"
                        f"决策请求 {len(bundle.valid_decision_requests)}",
                        brief_version=self.session.current_brief.version,
                        status="ok" if bundle.valid_diagnoses else "unknown",
                        refs=[{"path": record.diagnosis_path,
                               "sha256": _sha256_file(record.diagnosis_path),
                               "role": "diagnosis"}])
        self._notify(f"诊断（{origin}）：有效 {len(bundle.valid_diagnoses)} 条、"
                     f"拒收 {len(bundle.invalid_diagnoses)} 条、决策请求 "
                     f"{len(bundle.valid_decision_requests)} 条")
        return bundle

    def _handle_decision_requests(self, bundle: DiagnosisBundle, record: RoundRecord) -> tuple[bool, list]:
        """诊断决策请求先由 Mentis 分流：保留决定才暂停；其余自主决定并进入指令。"""
        if not bundle.valid_decision_requests:
            return False, []
        pause, decisions = False, []
        for request in bundle.valid_decision_requests:
            q = request.question
            change_intent = any(v in q for v in _CHANGE_VERBS)
            touches_reserved = any(n in q for n in _RESERVED_NOUNS)
            asks_new_fact = any(h in q for h in _NEW_FACT_HINTS) and not touches_reserved
            if (change_intent and touches_reserved) or asks_new_fact:
                ix = self.session.request_decision(
                    q, blocking_action=f"revise-round{record.round_no}")
                self.decision_routes.append({
                    "question": q, "route": "request_user", "interaction_id": ix.interaction_id,
                    "rationale": "触及保留决定或需要用户独有事实；依赖动作暂停（沉默≠批准）",
                })
                self.trace.emit("mentis", "decision.requested",
                                f"请求 {ix.interaction_id}：{q}；依赖修订暂停（沉默≠批准）",
                                brief_version=self.session.current_brief.version, status="pending")
                self.trace.emit("mentis", "decision.routed",
                                f"{ix.interaction_id} → request_user；触及保留决定或需要用户独有事实",
                                brief_version=self.session.current_brief.version, status="pending")
                self._notify(f"决策请求转用户（{ix.interaction_id}）：{q}")
                pause = True
            else:
                route = route_gap(Gap(q, "model_knowledge", "plan_changing", "reversible"))
                self.decision_routes.append({
                    "question": q, "route": "agent_decide", "interaction_id": "",
                    "rationale": f"可逆的专业取舍，未触及保留决定；{route.rationale}",
                    "decision": AUTONOMOUS_DECISION_TEXT,
                })
                self.trace.emit("mentis", "decision.routed",
                                f"agent_decide：{q}；可逆专业取舍；{route.rationale}；决定 {AUTONOMOUS_DECISION_TEXT}",
                                brief_version=self.session.current_brief.version)
                decisions.append(f"问：{q}\n  Mentis 自主决定：{AUTONOMOUS_DECISION_TEXT}")
                self._notify(f"决策请求分流为自主决定（不转问用户）：{q[:50]}…")
        return pause, decisions

    def _delegate_revision(self, record: RoundRecord, tag: str) -> str:
        """建立修订工作区（v1 已提交为基线）并真实委托 Codex。"""
        if not self._workspace_ready:
            self._prepare_workspace()
        self._commit_workspace_baseline()
        trace = self.session.notify_progress(
            f"Mentis 依据诊断自主发起修订 round{record.round_no}（notify 不阻塞）")
        attempt = self.session.dispatch(f"revise-{tag}", "exec-workspace")
        revision = self.session.record_revision(
            feedback_interaction_id=trace.interaction_id,
            from_attempt_id=attempt.attempt_id,
            instruction=f"[Mentis 指导指令 {tag}] 见 {record.instruction_path}",
            round_no=record.round_no,
        )
        revision.to_attempt_id = attempt.attempt_id
        brief_version = self.session.current_brief.version
        # 契约 §1：确切指令在实际调用前落盘；失败则不发起该次委托
        try:
            prompt_ref = self.trace.archive_prompt(attempt.attempt_id, record.instruction)
        except TraceArchiveError as exc:
            self.session.fail_attempt(attempt.attempt_id)
            self.trace.emit("mentis", "delegation.prepared",
                            f"prompt 落盘失败，本次委托不发起：{exc}",
                            attempt_id=attempt.attempt_id, brief_version=brief_version,
                            status="failed")
            self.notify(f"{tag} 未委托：{exc}")
            return "failed"
        self.trace.emit("mentis", "delegation.prepared",
                        f"已归档确切指令并委托修订 {tag}（文本见 refs；与实际发送同一对象）",
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        refs=[prompt_ref])
        self.trace.record_delegation(attempt.attempt_id, brief_version, prompt_ref,
                                     action=f"revise-{tag}")

        def on_executor_event(event: dict, meta: dict):
            # T2：逐行回调；异常不得中断执行（写失败由 manifest 完整性记录）
            try:
                kind = str(event.get("type", "?"))
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                itype = str(item.get("type", ""))
                refs = []
                if itype == "command_execution":
                    detail = f"命令 {str(item.get('command', ''))[:140]}（{item.get('status', '')}）"
                elif kind == "item.completed" and itype == "agent_message":
                    detail = f"最终消息：「{str(item.get('text', ''))[:160]}」"
                    message_ref = self.trace.archive_executor_message(
                        attempt.attempt_id, str(item.get("text", "")))
                    if message_ref is not None:
                        refs.append(message_ref)
                elif itype == "reasoning":
                    detail = "推理条目（内容不入默认层）"
                else:
                    detail = f"{kind} {itype}".strip() + (f"（{item.get('status', '')}）" if item.get("status") else "")
                if meta.get("first"):
                    detail = f"首事件延迟 {meta.get('first_event_latency_s', 0):.1f}s；" + detail
                self.trace.emit("executor", "executor.started" if meta.get("first") else "executor.item",
                                detail, attempt_id=attempt.attempt_id, brief_version=brief_version,
                                native_thread_id=self.thread_id or None, status="pending", refs=refs)
            except Exception:
                pass

        self.session.mark_running(attempt.attempt_id)
        spec = CodexTaskSpec(
            workspace=self.workspace, prompt=record.instruction,
            brief_version=brief_version,
            sandbox="workspace-write", timeout_s=self.timeout_s,
            extra_config=('approval_policy="never"',),  # 升级请求按失败处理，不等待审批
            on_event=on_executor_event,
        )
        import time

        started = time.monotonic()
        if self.thread_id:
            result = self.adapter.continue_task(self.thread_id, attempt.attempt_id, spec)
        else:
            result = self.adapter.start_task(spec)
        record.codex_duration_s = round(time.monotonic() - started, 1)
        self.thread_id = result.native_thread_id or self.thread_id
        record.codex_status = result.status.value
        record.codex_usage = dict(result.usage)
        record.codex_thread_id = result.native_thread_id
        record.codex_error = result.error
        self.trace.emit("executor", "executor.completed",
                        f"修订 {tag} 终态 {result.status.value}；时长 {record.codex_duration_s}s；"
                        f"用量 {record.codex_usage or '未取得'}"
                        + (f"；错误 {result.error[:120]}" if result.error else ""),
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        native_thread_id=result.native_thread_id or None,
                        status=STATUS_VALUE[result.status.value],
                        executor_status=result.status.value, usage=dict(result.usage))
        self.trace.record_executor_result(attempt.attempt_id, result.status,
                                          native_thread_id=result.native_thread_id,
                                          usage=dict(result.usage), error=result.error)
        events_path = self.run_dir / f"codex-{tag}-events.json"
        events_path.write_text(json.dumps(
            [{"seq": e.sequence, "kind": e.kind, "item_type": e.item_type,
              "item_status": e.item_status} for e in result.events],
            ensure_ascii=False, indent=2), encoding="utf-8")
        record.codex_events_path = str(events_path)
        self._notify(f"执行器 {tag}：状态 {result.status.value}，"
                     f"时长 {record.codex_duration_s}s，用量 {record.codex_usage or '（未取得）'}"
                     + (f"，错误 {result.error[:120]}" if result.error else ""))
        if result.status is AttemptStatus.COMPLETED:
            record.outcome = "completed"
            self.session.complete_attempt(
                attempt.attempt_id, artifact_path=str(self._current_html()),
                artifact_producer="codex-cli")
            return "completed"
        if result.status is AttemptStatus.UNKNOWN:
            return self._reconcile_unknown(record, attempt)
        self.session.fail_attempt(attempt.attempt_id)
        record.outcome = "failed"
        return "failed"

    def _reconcile_unknown(self, record: RoundRecord, attempt) -> str:
        """超时/无终态：先核对再继续，不盲目重试；核对依据全部落盘。"""
        active = codex_processes_since(self.session.created_at)
        if active:
            self._notify(f"仍有 {len(active)} 个 codex 进程晚于本轮启动：结局未知，"
                         "不确认、不重发；需人工核对执行器")
            record.outcome = "needs_human"
            self.session.mark_unknown(attempt.attempt_id)
            return "needs_human"
        self.session.mark_unknown(attempt.attempt_id)
        digest = _workspace_lock_digest(self.workspace)
        lock = Path("runtime") / "executor_locks" / f"{digest}.lock"
        if lock.is_file():
            lock.unlink()
            self._notify(f"写锁已释放（核对通过：无活跃写入者）：{lock}")
        if self._current_html().is_file():
            record.outcome = "reconciled_from_artifacts"
            self._notify("执行器无终态事件，但工作区产物存在且无活跃写入者："
                         "按产物核对继续（复检独立判定，不冒充执行器自报完成）")
            artifact = self.session.add_artifact(
                attempt.attempt_id, str(self._current_html()), "codex-cli(产物核对补记)")
            attempt.artifact_id = artifact.artifact_id
            return "reconciled"
        record.outcome = "failed"
        self._notify("无终态事件且工作区无产物：按失败处理")
        return "failed"

    def _verify_round(self, record: RoundRecord, tag: str, v2_html: Path,
                      bundle: DiagnosisBundle, defect_ids: list) -> str:
        """独立复检：确定性保留项/溢出检查 + 模型复查（可看 v2 截图）。

        测量不可信、缺记录或缺复查截图 → unknown：不判通过，记为 unverified。
        """
        pack2, images2 = self._observe(v2_html, f"{tag}-verify")
        findings, regression, unknown = self._deterministic_checks(pack2)
        if self.include_images and self.browser_capture is not None and not images2:
            unknown.append("复查截图缺失：视觉类解决声明无法核对（不作为通过依据）")
        resolved, unresolved, new_issues = self._model_review(
            v2_html, bundle, defect_ids, pack2, images2, tag, record)
        verification = {
            "deterministic_findings": findings,
            "preserved_regression": regression,
            "measurement_unknown": unknown,
            "model_resolved": resolved,
            "model_unresolved": unresolved,
            "model_new_issues": new_issues,
            "measurement_v2": {v.name: {"overflow_px": v.overflow_px,
                                        "overflowing": len(v.overflowing),
                                        "clipped": len(v.clipped_text),
                                        "inner_width": v.inner_width,
                                        "errors": list(v.errors)}
                               for v in pack2.measurement.viewports},
        }
        record.verification = verification
        review_passed = not regression and not unresolved and not new_issues and not unknown
        self.session.record_review(
            self.session.attempts[-1].attempt_id, findings=findings + [
                f"模型复查：已解决 {resolved or '无'}；未解决 {unresolved or '无'}；"
                f"新问题 {new_issues or '无'}"]
                + [f"测量未知：{u}" for u in unknown],
            passed=review_passed,
            disposition=Disposition.ACCEPT if review_passed else Disposition.REVISE)
        (self.run_dir / f"review-{tag}.md").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
        self.trace.emit("mentis", "review.recorded",
                        f"复检：保留项回归 {regression or '无'}；测量未知 {unknown or '无'}；"
                        f"模型复查已解决 {resolved or '无'}/未解决 {unresolved or '无'}/"
                        f"新问题 {new_issues or '无'}；结论 {'通过' if review_passed else '不通过'}",
                        attempt_id=self.session.attempts[-1].attempt_id,
                        brief_version=self.session.current_brief.version,
                        status="ok" if review_passed else "failed",
                        refs=[{"path": str(self.run_dir / f"review-{tag}.md"),
                               "sha256": _sha256_file(self.run_dir / f"review-{tag}.md"),
                               "role": "review"}])
        record.review_path = str(self.run_dir / f"review-{tag}.md")
        if regression:
            return "regression"
        if unknown:
            return "unverified"
        if not unresolved and not new_issues and resolved:
            return "improved"
        if not unresolved and not new_issues and not resolved:
            # 无明确缺陷被确认解决：检查保留项完好 → 视为未改善（不标改善）
            return "not_improved"
        return "partially_improved"

    def _deterministic_checks(self, pack: ObservationPack) -> tuple[list, list, list]:
        return deterministic_checks(pack, self._preserved, self._form_field_requirements)

    def _model_review(self, v2_html: Path, bundle: DiagnosisBundle, defect_ids: list,
                      pack2: ObservationPack, images2: list, tag: str,
                      record: RoundRecord) -> tuple[list, list, list]:
        out_path = self.run_dir / f"model-review-{tag}.json"
        resolved, unresolved, new_issues = run_model_review(
            self.gateway, self._goal, v2_html, bundle, defect_ids, pack2, images2,
            out_path, use_model=self.use_model, notify=self._notify)
        record.review_path = str(out_path)
        return resolved, unresolved, new_issues

    # ---- 工作区与版本 ----

    def _prepare_workspace(self):
        ws = self.workspace
        if ws.exists():
            shutil.rmtree(ws)
        (ws / "input").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True, capture_output=True)
        shutil.copy2(self._v1_dir / "index.html", ws / "index.html")
        shutil.copy2(self._v1_dir / "source" / "workspace_input_copy.md", ws / "input" / "copy.md")
        (ws / "brief.md").write_text(
            f"# 目标\n{self._goal}\n\n# 约束（冻结自基线，不得违反）\n"
            + "\n".join(f"- {c}" for c in self._constraints) + "\n"
            + "# 必须保留的活动事实\n"
            + "\n".join(f"- {label}：{needle}" for label, needle in self._preserved) + "\n",
            encoding="utf-8")
        names = {p.name for p in ws.iterdir()}
        if not {"index.html", "brief.md", "input", ".git"} <= names:
            raise RuntimeError(f"修订工作区初始化不完整：{names}")
        self._workspace_ready = True
        self._notify(f"修订工作区已建立（v1 已提交为基线，无任何秘密文件）：{ws}")

    def _commit_workspace_baseline(self):
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                              cwd=self.workspace, capture_output=True)
        if head.returncode == 0:
            return  # 基线已在（首轮建立）；当前未提交改动就是执行器的工作
        subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Mentis", "-c",
                        "user.email=mentis@local", "commit", "-qm", "baseline v1 (frozen)"],
                       cwd=self.workspace, check=True, capture_output=True)

    def _current_html(self) -> Path:
        return self.workspace / "index.html"

    def _save_version(self, html_path: Path, name: str):
        dest = self.run_dir / name
        dest.mkdir(exist_ok=True)
        shutil.copy2(html_path, dest / "index.html")
        obs_dir = self.run_dir / f"observation-{self.rounds[-1].round_no}-verify"
        if self.browser_capture is not None:
            try:
                browser_obs = self.browser_capture(dest / "index.html", dest)
                self._notify("版本截图：" + "；".join(
                    f"{v.name} {v.actual_width}×{v.actual_height}"
                    for v in browser_obs.views if v.screenshot_path))
            except Exception as exc:
                self._notify(f"版本截图失败：{type(exc).__name__}")

    # ---- 记录 ----

    def _notify(self, message: str):
        self.session.notify_progress(message)
        print(f"[指导] {message}")

    def _retry_failed_round(self) -> str:
        """失败轮的核对后续办（计入修订预算）：确认无活跃写入者后按当前工作区续办。

        不是盲目重试：前提是上一轮执行器已退出（非超时未知）、工作区产物可核对；
        续办指令明确告知执行器上次中断与已有改动，要求先核对再完成自查。
        """
        last = self.rounds[-1] if self.rounds else None
        if last is None or last.outcome != "failed":
            self.status = "NO_WORK"
            self._notify("没有可续办的失败轮（仅 failed 且预算内可续办）")
            self._persist()
            return self.status
        if not (self.workspace / "index.html").is_file():
            self.status = "NO_WORK"
            self._notify("工作区没有可续办的产物")
            self._persist()
            return self.status
        active = codex_processes_since(self.session.created_at)
        if active:
            self.status = "NEEDS_HUMAN"
            self._notify(f"仍有 {len(active)} 个 codex 进程活跃：不续办，先人工核对")
            self._persist()
            return self.status
        if len(self.rounds) >= self.max_rounds:
            self.status = "EXHAUSTED"
            self._notify("修订预算已用完：不续办")
            self._persist()
            return self.status
        record = RoundRecord(round_no=last.round_no + 1)
        self.rounds.append(record)
        tag = f"round{record.round_no}"
        record.instruction = (
            last.instruction
            + f"\n\n[续办说明] 上一 turn 因执行器原因中断（状态 {last.codex_status}"
            + (f"：{last.codex_error[:160]}" if last.codex_error else "")
            + "）。工作区中可能已有未完成的部分修改：先用 git diff 核对已改动内容，"
              "判断其是否完整、正确并与上述要求一致，再补完；不要整体重建，"
              "不要重复已完成的部分。完成后确保 index.html 可直接打开。")
        record.instruction_path = str(self.run_dir / f"instruction-{tag}.md")
        record.diagnosis_path = last.diagnosis_path
        (self.run_dir / f"instruction-{tag}.md").write_text(
            record.instruction, encoding="utf-8")
        self._notify(f"核对通过（无活跃写入者、产物在）：按预算续办第 {record.round_no} 轮")
        outcome = self._delegate_revision(record, tag)
        if outcome in ("failed", "needs_human"):
            self.status = "FAILED" if outcome == "failed" else "NEEDS_HUMAN"
            self._persist()
            return self.status
        v2_html = self._current_html()
        if not v2_html.is_file():
            self.status = "FAILED"
            self._persist()
            return self.status
        bundle = _bundle_from_markdown(
            Path(last.diagnosis_path) if last.diagnosis_path else "")
        defect_ids = [f"d{i}" for i in range(1, len(bundle.valid_diagnoses) + 1)]
        if not bundle.valid_diagnoses:
            defect_ids = []
        verdict = self._verify_round(record, tag, v2_html, bundle, defect_ids)
        record.verdict = verdict
        if verdict == "improved":
            self._save_version(v2_html, "v2")
            self.status = "IMPROVED"
            self.verdict = "improved"
            try:
                self.session.accept(self.session.attempts[-1].attempt_id)
            except Exception as exc:
                self._notify(f"接受记录被拒绝：{exc}")
        else:
            self._save_version(v2_html, "v2")
            self.status = verdict.upper()
            self.verdict = verdict
        self._persist()
        return self.status

    def resume(self, answers: dict | None = None, retry: bool = False) -> str:
        """恢复循环（异常同样收尾落盘，见 _finalize_on_error）。"""
        try:
            return self._resume_impl(answers=answers, retry=retry)
        except Exception as exc:
            self._finalize_on_error(exc)
            raise

    def _resume_impl(self, answers: dict | None = None, retry: bool = False) -> str:
        """恢复循环：回答诊断的用户决定（answers），或在核对后续办失败轮（retry=True）。"""
        if retry:
            return self._retry_failed_round()
        answers = answers or {}
        for ix_id, text in answers.items():
            self.session.answer_interaction(ix_id, text, source="user")
            self.trace.emit("user", "decision.answered",
                            f"用户回答 {ix_id}；原文保存在会话记录",
                            brief_version=self.session.current_brief.version)
        pending = [ix for ix in self.session.interactions
                   if ix.kind.value == "request_decision" and ix.answer_source is None]
        if pending:
            self.status = "NEEDS_DECISION"
            self._notify("仍有未回答的请求：依赖动作保持暂停")
            self._persist()
            return self.status
        last_round = self.rounds[-1] if self.rounds else None
        if last_round is None:
            self.status = "NO_WORK"
            self._notify("没有可继续的修订轮")
            self._persist()
            return self.status
        answer_text = "；".join(f"{k}:{v}" for k, v in answers.items())
        self.session.update_brief(add_preferences=[SourcedItem(
            f"用户决定（诊断请求）：{answer_text}", Provenance.USER_SELECTED, "user-decision")])
        self.trace.emit("mentis", "brief.updated",
                        f"诊断请求的用户决定已写入 brief v{self.session.current_brief.version}",
                        brief_version=self.session.current_brief.version)
        if not last_round.instruction:
            # NEEDS_DECISION 暂停发生在指令组装前：依据落盘诊断与重新观察组装
            page = self._v1_dir / "index.html" if last_round.round_no == 1 else self._current_html()
            pack, images = self._observe(page, f"round{last_round.round_no}b")
            bundle = _bundle_from_markdown(
                self.run_dir / f"diagnosis-round{last_round.round_no}.md")
            if not bundle.valid_diagnoses:
                self.status = "NO_WORK"
                self._notify("落盘诊断无法恢复缺陷清单：不猜测、不委托")
                self._persist()
                return self.status
            defect_ids = [f"d{i}" for i in range(1, len(bundle.valid_diagnoses) + 1)]
            last_round.instruction = compose_revision_instruction(
                bundle, defect_ids, pack, self._preserved, self._constraints,
                user_answers=answer_text)
            last_round.instruction_path = str(
                self.run_dir / f"instruction-round{last_round.round_no}b.md")
            Path(last_round.instruction_path).write_text(
                last_round.instruction, encoding="utf-8")
            self._last_bundle = (bundle, defect_ids)
        self._notify("决定已回答：继续被暂停的修订委托")
        outcome = self._delegate_revision(last_round, f"round{last_round.round_no}b")
        if outcome in ("failed", "needs_human"):
            self.status = "FAILED" if outcome == "failed" else "NEEDS_HUMAN"
            self._persist()
            return self.status
        v2_html = self._current_html()
        if not v2_html.is_file():
            self.status = "FAILED"
            self._persist()
            return self.status
        if hasattr(self, "_last_bundle"):
            bundle, defect_ids = self._last_bundle
        else:
            bundle = _bundle_from_markdown(
                self.run_dir / f"diagnosis-round{last_round.round_no}.md")
            defect_ids = [f"d{i}" for i in range(1, len(bundle.valid_diagnoses) + 1)]
        verdict = self._verify_round(last_round, f"round{last_round.round_no}b", v2_html,
                                     bundle, defect_ids)
        last_round.verdict = verdict
        if verdict == "improved":
            self._save_version(v2_html, "v2")
            self.status = "IMPROVED"
            self.verdict = "improved"
            try:
                self.session.accept(self.session.attempts[-1].attempt_id)
            except Exception as exc:
                self._notify(f"接受记录被拒绝：{exc}")
        else:
            self.status = verdict.upper()
            self.verdict = verdict
        self._persist()
        return self.status

    def reverify(self) -> str:
        """对已有 v2 重新复检（透明更正判定；异常同样收尾落盘）。"""
        try:
            return self._reverify_impl()
        except Exception as exc:
            self._finalize_on_error(exc)
            raise

    def _reverify_impl(self) -> str:
        """对已有 v2 用修正后的确定性工具重新复检（透明更正判定）。

        模型复查结论必须先通过绑定检查（对应当前 v2 产物版本）才可复用；
        本方法重跑确定性检查并重测 v2，不新增模型调用、不重新委托。
        历史复核文件按序号保留，不覆盖（误判与更正记录可审计）。
        """
        last = self.rounds[-1] if self.rounds else None
        v2_html = self.run_dir / "v2" / "index.html"
        if last is None or not v2_html.is_file():
            self.status = "NO_WORK"
            self._notify("没有可复核的 v2 产物")
            self._persist()
            return self.status
        self._ensure_v1_reference(force=True)
        suffix = ""
        k = 1
        while (self.run_dir / f"review-round{last.round_no}-recheck{suffix}.md").is_file():
            k += 1
            suffix = str(k)
        pack2, _ = self._observe(v2_html, f"round{last.round_no}-recheck{suffix}")
        findings, regression, unknown = self._deterministic_checks(pack2)
        resolved, unresolved, new_issues = [], [], []
        review_file = self.run_dir / f"model-review-round{last.round_no}.json"
        bound, bind_note = self._review_binding_status(last, v2_html, review_file)
        if bound:
            try:
                prior = json.loads(review_file.read_text(encoding="utf-8"))
                data = json.loads(prior.get("response", "{}"))
                resolved = [r for r in data.get("resolved", []) if str(r).strip()]
                unresolved = [r for r in data.get("unresolved", []) if str(r).strip()]
                new_issues = [str(x) for x in data.get("new_issues", []) if str(x).strip()]
            except (json.JSONDecodeError, ValueError) as exc:
                unresolved = ["review-unreadable"]
                bind_note = f"{bind_note}；但复查响应不可解析（{exc}）：按未确认处理"
        elif review_file.is_file():
            unresolved = ["review-unbound"]
        else:
            unresolved = ["review-missing"] if not last.verdict else ["review-unbound"]
        verification = {
            "deterministic_findings": findings,
            "preserved_regression": regression,
            "measurement_unknown": unknown,
            "model_resolved": resolved,
            "model_unresolved": unresolved,
            "model_new_issues": new_issues,
            "model_review_binding": bind_note,
            "measurement_v2": {v.name: {"overflow_px": v.overflow_px,
                                        "overflowing": len(v.overflowing),
                                        "clipped": len(v.clipped_text),
                                        "inner_width": v.inner_width,
                                        "errors": list(v.errors)}
                               for v in pack2.measurement.viewports},
        }
        last.verification = verification
        unconfirmed = all(u in ("review-unbound", "review-missing", "review-unreadable")
                          for u in unresolved) and bool(unresolved)
        if regression:
            verdict = "regression"
        elif unknown or unconfirmed:
            verdict = "unverified"
        elif verdict_is_improvement(regression, resolved, unresolved, new_issues):
            verdict = "improved"
        elif resolved:
            verdict = "partially_improved"
        else:
            verdict = "not_improved"
        prior_verdict = last.verdict or "（未复检）"
        correction = "复核重检（结论维持）" if verdict == last.verdict else "复核更正"
        last.notes.append(
            f"{correction}：{prior_verdict} → {verdict}；"
            f"依据：逐段保留项/逐字段必填/测量可信度检查 + 复查绑定确认；{bind_note}")
        last.verdict = verdict
        self.verdict = verdict
        review_passed = not regression and not unknown and not unresolved and not new_issues
        self.session.record_review(
            self.session.attempts[-1].attempt_id,
            findings=findings + [f"测量未知：{u}" for u in unknown] + [bind_note],
            passed=review_passed,
            disposition=Disposition.ACCEPT if review_passed else Disposition.REVISE)
        review_path = self.run_dir / f"review-round{last.round_no}-recheck{suffix}.md"
        review_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        last.review_path = str(review_path)
        self.trace.emit("mentis", "review.recorded",
                        f"复核重检：保留项回归 {regression or '无'}；测量未知 {unknown or '无'}；"
                        f"绑定：{bind_note[:100]}；结论 {'通过' if review_passed else '不通过'}",
                        attempt_id=self.session.attempts[-1].attempt_id,
                        brief_version=self.session.current_brief.version,
                        status="ok" if review_passed else "failed",
                        refs=[{"path": str(review_path),
                               "sha256": _sha256_file(review_path), "role": "review"}])
        if verdict == "improved":
            self.status = "IMPROVED"
            try:
                self.session.accept(self.session.attempts[-1].attempt_id)
                self._notify("复核后满足改善与保留项条件：已接受（completed ≠ 用户满意，后者单独记录）")
            except Exception as exc:
                self._notify(f"接受被拒绝：{exc}")
                self.status = "NOT_ACCEPTED"
        else:
            self.status = verdict.upper()
        self._notify(f"复核判定：{verdict}（历史判定与备注全部保留，可审计）")
        self._persist()
        return self.status

    def _review_binding_status(self, last: RoundRecord, v2_html: Path,
                               review_file: Path) -> tuple[bool, str]:
        """确认历史模型复查对应当前 v2 产物版本；不能确认就不复用其结论。

        新版记录直接比对 reviewed_html_sha256；旧版记录无该字段时，
        用"复查图像指纹 + 当轮测量记录的 source_sha256"间接绑定。
        """
        if not review_file.is_file():
            return False, "无历史模型复查记录：按未确认处理"
        try:
            prior = json.loads(review_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            return False, f"历史复查记录不可解析（{exc}）：按未确认处理"
        if not prior.get("ok"):
            return False, "历史复查调用未成功：按未确认处理"
        current_sha = _sha256_file(v2_html)
        recorded_sha = prior.get("reviewed_html_sha256")
        if recorded_sha:
            if recorded_sha == current_sha:
                return True, f"复查绑定当前产物（reviewed_html_sha256 一致：{current_sha[:12]}）"
            return False, (f"复查针对旧产物（记录 {str(recorded_sha)[:12]} ≠ "
                           f"当前 {current_sha[:12]}）：不复用其结论")
        refs = [str(r) for r in (prior.get("image_refs") or [])]
        if not refs:
            return False, "历史复查未记录图像输入且无产物哈希：无法确认对应版本，按未确认处理"
        verify_dir = self.run_dir / f"observation-round{last.round_no}-verify"
        for ref in refs:
            name, _, sha12 = ref.partition(";")
            path = verify_dir / name
            if not sha12 or not path.is_file():
                return False, f"复查图像 {name} 缺指纹或文件缺失：无法确认绑定，按未确认处理"
            actual = _sha256_file(path)[:12]
            if actual != sha12:
                return False, (f"复查图像 {name} 指纹不符（记录 {sha12} ≠ 现存 {actual}）："
                               "复查针对旧版本，不复用其结论")
        measurement_file = verify_dir / "measurement.json"
        if not measurement_file.is_file():
            return False, "缺少当轮测量记录：无法确认截图对应产物版本，按未确认处理"
        try:
            measured_sha = json.loads(
                measurement_file.read_text(encoding="utf-8")).get("source_sha256")
        except json.JSONDecodeError:
            return False, "当轮测量记录不可解析：无法确认绑定，按未确认处理"
        if measured_sha != current_sha:
            return False, (f"当轮测量对应产物（{str(measured_sha)[:12]}）与当前 v2"
                           f"（{current_sha[:12]}）不一致：不复用复查结论")
        return True, ("复查绑定当前产物（legacy 间接绑定：复查图像指纹与当轮测量 sha256 "
                      "均与现存 v2 一致）")

    def _merged_model_usage(self) -> dict:
        """恢复后的网关只计恢复后的调用；与落盘的先前用量合并，避免漏计。"""
        usage = dict(getattr(self, "_restored_model_usage", {}) or {})
        if self.gateway:
            for key, value in self.gateway.usage_summary().items():
                if key in ("prompt_tokens", "completion_tokens"):
                    usage[key] = usage.get(key, 0) + value
                elif key not in usage or key == "model":
                    usage[key] = value
                elif key in ("real_calls", "mock_calls", "failed_calls"):
                    usage[key] = usage.get(key, 0) + value
        return usage

    def _persist(self):
        if not self._finalized_emitted:
            self.trace.emit("mentis", "run.finalized",
                            f"运行收尾：状态 {self.status}；判定 {self.verdict or '（未定）'}",
                            brief_version=self.session.current_brief.version,
                            status="ok" if self.status == "IMPROVED" else "unknown")
            self._finalized_emitted = True
        self.trace.finalize(outcome=f"{self.status}（verdict={self.verdict or '未定'}）")
        save_session(self.session, str(self.run_dir / "session.json"))
        meta = {
            "state_version": STATE_VERSION,
            "run_id": self.run_id, "status": self.status, "verdict": self.verdict,
            "baseline_dir": str(self.baseline_dir.resolve()),
            "goal": self._goal,
            "max_rounds": self.max_rounds, "timeout_s": self.timeout_s,
            "workspace": str(self.workspace),
            "thread_id": self.thread_id,
            "rounds": [{k: v for k, v in r.__dict__.items()} for r in self.rounds],
            "decision_routes": self.decision_routes,
            "model_usage": self._merged_model_usage(),
            "preserved_items": [[label, needle] for label, needle in self._preserved],
            "preserved_dropped": [[label, needle] for label, needle in getattr(self, "_dropped_preserved", [])],
            "currency_cap": "无货币硬上限——仅记录可取得的用量",
        }
        (self.run_dir / "state.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        from mentis.trace import render_conversation
        self._conversation_ready = False
        try:
            (self.run_dir / "conversation.md").write_text(
                render_conversation(self.run_dir), encoding="utf-8")
            self._conversation_ready = True
        except OSError:
            pass  # 派生视图失败不改写执行结局；原始 trace 仍可回放
        (self.run_dir / "report.md").write_text(self.render_report(meta), encoding="utf-8")

    def render_report(self, meta: dict) -> str:
        lines = [f"# Mentis 指导修订报告 {self.run_id}", ""]
        lines.append(f"- 状态：**{self.status}**；判定：**{self.verdict or '（未定）'}**")
        lines.append(f"- 目标：{self._goal}")
        lines.append(f"- 费用口径：{meta['currency_cap']}")
        lines.append("")
        lines.append("## 看结果")
        for name in ("v1", "v2"):
            html = self.run_dir / name / "index.html"
            if html.is_file():
                lines.append(f"- [{name}] `{html.resolve()}`（可直接用浏览器打开）")
                for shot in ("desktop.png", "mobile.png"):
                    shot_path = self.run_dir / name / shot
                    if shot_path.is_file():
                        lines.append(f"  - 截图：`{shot_path.resolve()}`")
        lines.append("")
        lines.append("## 看变化")
        for r in self.rounds:
            lines.append(f"- 第 {r.round_no} 轮：执行器 {r.codex_status}"
                         f"（{r.codex_duration_s}s）；结局 {r.outcome or '（未委托）'}；"
                         f"判定 {r.verdict or '（未复检）'}")
            if r.verification:
                v = r.verification
                lines.append(f"  - 保留项回归：{v['preserved_regression'] or '无'}；"
                             f"模型复查已解决 {v['model_resolved'] or '无'}、"
                             f"未解决 {v['model_unresolved'] or '无'}、新问题 {v['model_new_issues'] or '无'}")
                if v.get("measurement_unknown"):
                    lines.append(f"  - **测量未知（不作为通过依据）**：{'；'.join(v['measurement_unknown'])}")
                if v.get("model_review_binding"):
                    lines.append(f"  - 复查绑定：{v['model_review_binding']}")
                for vp, m in v["measurement_v2"].items():
                    lines.append(f"  - {vp}：溢出 {m['overflow_px']}px"
                                 f"（溢出元素 {m['overflowing']}、裁剪 {m['clipped']}）")
        for label, needle in self._preserved:
            lines.append(f"- 保留项 {label}：{needle}（复检见 review-*.md）")
        lines.append("")
        lines.append("## 看依据")
        if getattr(self, "_conversation_ready", False):
            lines.append(f"- Mentis 与 Codex 的可见交流：`{(self.run_dir / 'conversation.md').resolve()}`"
                         "（确切委托、公开回复与动作摘要；本地私有，不含内部推理）")
        else:
            lines.append("- 交流视图未生成；可从 trace/events.jsonl 与 trace/prompts/ 核对。")
        lines.append(f"- 冻结记录：{self.run_dir / 'freeze.json'}"
                     f"（v1 sha256 前 12 位 {freeze_baseline_sha(self._v1_dir)}；原运行目录未修改）")
        lines.append(f"- 模型用量：{meta['model_usage']}")
        for r in self.rounds:
            if r.diagnosis_path:
                lines.append(f"- 诊断：`{Path(r.diagnosis_path).resolve()}`")
            if r.instruction_path:
                lines.append(f"- **给执行器的完整指令**：`{Path(r.instruction_path).resolve()}`"
                             f"（含证据引用与保留项，无开发者代答）")
            if r.codex_events_path:
                lines.append(f"- 执行器事件：`{Path(r.codex_events_path).resolve()}`"
                             f"（thread {r.codex_thread_id or '未取得'}，用量 {r.codex_usage or '未取得'}）")
            if r.codex_error:
                lines.append(f"- 执行器错误：{r.codex_error[:200]}")
            if r.review_path:
                lines.append(f"- 复检：`{Path(r.review_path).resolve()}`")
            model_review = self.run_dir / f"model-review-round{r.round_no}.json"
            if model_review.is_file():
                lines.append(f"- 模型复查（含图像输入）：`{model_review.resolve()}`")
            for note in r.notes:
                lines.append(f"- 备注：{note}")
        for route in self.decision_routes:
            lines.append(f"- 决策请求分流：{route['question'][:60]} → **{route['route']}**；"
                         f"理由：{route['rationale'][:100]}")
            if route.get("decision"):
                lines.append(f"  - 自主决定：{route['decision']}")
        lines.append(f"- 会话记录：{self.run_dir / 'session.json'}（brief/授权/尝试/评审）")
        lines.append("- 边界：真实网络请求未验证（仅声明扫描）；对比度与部署表现未验证；"
                     "截图为初始画面；改善判定限于本轮证据，不构成专业能力结论；"
                     "领域资源为草案（pending_human）。")
        lines.append("")
        return "\n".join(lines)


# ---- 指令组装与辅助 ----

def _measurement_anomalies(v) -> list:
    """单视口测量可信度：报错、尺寸为 0 或与目标视口不符都不可作为"通过"依据。"""
    problems = []
    if v.errors:
        problems.append("测量报错：" + "；".join(v.errors))
    if v.inner_width <= 0:
        problems.append(f"视口尺寸异常：innerWidth {v.inner_width}")
    elif v.inner_width != v.width:
        problems.append(f"视口宽度不符：请求 {v.width}px、实际 {v.inner_width}px"
                        "（布局不在目标视口，溢出数值不可信）")
    return problems


def deterministic_checks(pack: ObservationPack, preserved: list,
                         form_field_requirements: list | None) -> tuple[list, list, list]:
    """保留项（逐段）、表单逐字段必填与溢出的确定性复检（T4 提取为模块级，collab 复用）。

    返回 (findings, regression, unknown)：unknown 是"无法确认"项——
    测量不可信或缺记录时溢出记为未知，不得以零溢出判通过。
    """
    findings, regression, unknown = [], [], []
    text_blob = "\n".join(i.text for i in pack.structure.items)
    for label, needle in preserved:
        if needle in text_blob:
            findings.append(f"保留项 {label}（{needle}）：在")
        else:
            findings.append(f"保留项 {label}（{needle}）：缺失")
            regression.append(f"{label}缺失（{needle}）")
    inputs = [i for i in pack.structure.items if i.kind in ("input", "textarea")]
    by_text = {}
    for i in inputs:
        by_text.setdefault(i.text, []).append(i)
    for name in form_field_requirements or []:
        hits = by_text.get(name, [])
        if not hits:
            regression.append(f"表单字段 {name} 缺失")
            findings.append(f"表单字段 {name}：缺失")
        elif not any(h.extra.get("required") == "yes" for h in hits):
            regression.append(f"表单字段 {name} 的必填要求丢失")
            findings.append(f"表单字段 {name}：存在但必填要求丢失")
        else:
            findings.append(f"表单字段 {name}：存在且必填")
    if form_field_requirements and len(inputs) < len(form_field_requirements):
        regression.append(f"表单字段总数减少：要求 ≥{len(form_field_requirements)}，"
                          f"实际 {len(inputs)}")
        findings.append(f"表单字段总数：{len(inputs)}（要求 ≥{len(form_field_requirements)}）")
    if not pack.measurement.viewports:
        unknown.append("无渲染测量记录：溢出与表单行为无法确认")
    for v in pack.measurement.viewports:
        anomalies = _measurement_anomalies(v)
        if anomalies:
            unknown.append(f"{v.name} 视口测量不可信：{'；'.join(anomalies)}")
            findings.append(f"{v.name} 视口横向溢出：未知（测量不可信，不作为通过依据）")
            continue
        ni = v.network_intent or {}
        if ni.get("hasFetch") or ni.get("hasXHR") or ni.get("hasBeacon"):
            regression.append(f"{v.name} 视口出现网络调用声明（违反本地无网络约束）")
        if v.overflow_px > 0 or v.overflowing:
            regression.append(
                f"{v.name} 视口出现横向溢出 {v.overflow_px}px（{len(v.overflowing)} 个元素）")
        else:
            findings.append(f"{v.name} 视口无横向溢出")
    return findings, regression, unknown


def run_model_review(gateway, goal: str, html_path: Path, bundle: DiagnosisBundle,
                     defect_ids: list, pack: ObservationPack, images: list, out_path: Path,
                     use_model: bool = True, notify=None) -> tuple[list, list, list]:
    """模型复查：对照修改前诊断清单核对修改后观察与截图（T4 提取为模块级，collab 复用）。

    返回 (resolved, unresolved, new_issues)；输出落盘 out_path 并绑定当前产物哈希。
    """
    def _notify(message: str):
        if notify is not None:
            notify(message)

    if not use_model or not gateway.has_credentials():
        _notify("模型不可用：修订复查降级为仅确定性检查")
        return [], [f"d{i}" for i in defect_ids], []
    defect_list = "\n".join(
        f"- ({did}) {d.phenomenon}（证据：{'，'.join(d.evidence_refs)}）"
        for did, d in zip(defect_ids, bundle.valid_diagnoses))
    system = (
        "你是网页修改复查者。给你修改前的诊断清单和修改后的观察记录与截图。"
        "只输出 JSON：resolved（已解决的诊断编号数组）、unresolved（未解决的编号数组）、"
        "new_issues（新发现问题的字符串数组，没有则空数组）、summary（一句话）。"
        "编号必须来自诊断清单；不得虚构观察记录中不存在的证据。"
    )
    user_prompt = (
        f"[目标] {goal}\n\n[修改前的诊断清单]\n{defect_list}\n\n"
        f"[修改后的观察记录]\n{pack.render_text()}\n\n"
        "[输出要求] 只输出 JSON 对象：resolved/unresolved/new_issues/summary。"
        "判断每个编号对应的问题在修改后的页面中是否仍然存在。"
    )
    gw_record = (gateway.complete(system, user_prompt, images=images)
                 if images else gateway.complete(system, user_prompt))
    out_path.write_text(json.dumps({
        "ok": gw_record.ok, "mock": gw_record.mock, "model": gw_record.model,
        "usage": gw_record.usage, "error": gw_record.error,
        "image_refs": gw_record.image_refs,
        "reviewed_html_sha256": _sha256_file(html_path),  # 复查所针对产物版本的绑定
        "response": gw_record.response_text[:4000],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not gw_record.ok:
        _notify(f"模型复查调用失败（{gw_record.error}）：按未确认处理，不标改善")
        return [], list(defect_ids), []
    try:
        data = json.loads(gw_record.response_text)
    except json.JSONDecodeError as exc:
        _notify(f"模型复查输出不是合法 JSON（{exc}）：按未确认处理")
        return [], list(defect_ids), []
    known = set(defect_ids)
    resolved = [r for r in data.get("resolved", []) if r in known]
    unresolved = [r for r in data.get("unresolved", []) if r in known]
    unknown_refs = [r for r in (*data.get("resolved", []), *data.get("unresolved", []))
                    if r not in known]
    if unknown_refs:
        _notify(f"复查引用了不存在的诊断编号 {unknown_refs}：已剔除并记录")
    new_issues = [str(x) for x in data.get("new_issues", []) if str(x).strip()]
    return resolved, unresolved, new_issues


def compose_revision_instruction(bundle: DiagnosisBundle, defect_ids: list,
                                 pack: ObservationPack, preserved: list,
                                 constraints: list[str], user_answers: str = "",
                                 agent_decisions: list | None = None,
                                 extra_problems: list | None = None) -> str:
    """由诊断（模型）与测量（机器）组装修订指令；开发者不提供缺陷或修法。"""
    defect_lines = []
    for did, d in zip(defect_ids, bundle.valid_diagnoses):
        candidates = "；".join(d.modification_candidates)
        defect_lines.append(
            f"({did}) [{d.severity}] {d.phenomenon}｜证据：{'，'.join(d.evidence_refs)}"
            f"｜解释：{d.interpretation}｜Mentis 修改候选：{candidates}")
    preserved_lines = "\n".join(f"- {label}：{needle}" for label, needle in preserved)
    constraint_lines = "\n".join(f"- {c}" for c in constraints)
    answers_section = (f"\n[用户决定（诊断请求的回答，用户原创）]\n{user_answers}\n"
                       if user_answers else "")
    decisions_section = ""
    if agent_decisions:
        decisions_section = ("\n[Mentis 对诊断请求的自主分流（未转问用户，理由见报告）]\n"
                             + "\n".join(f"- {d}" for d in agent_decisions) + "\n")
    extra_section = ""
    if extra_problems:
        extra_section = ("\n[上一轮遗留问题（复检发现，必须一并解决）]\n"
                         + "\n".join(f"- {p}" for p in extra_problems) + "\n")
    return (
        "修改当前工作区已有的 index.html（不要重建整页；最小化改动，保持既有风格与结构）。\n\n"
        "[Mentis 诊断（本轮要解决的问题）]\n" + "\n".join(defect_lines) + "\n"
        + answers_section + decisions_section + extra_section +
        "\n[渲染测量证据（可重复执行的浏览器测量，数值为准）]\n"
        + pack.measurement.render_text() + "\n\n"
        "[必须保留的活动事实（复检将逐项核对，缺失即判回归）]\n" + preserved_lines + "\n\n"
        "[原约束（冻结自用户目标）]\n" + constraint_lines + "\n\n"
        "[硬要求] 1) 只改 index.html；2) 保持单文件、中文内容、无网络调用"
        "（不得引入 fetch/XHR/sendBeacon）；3) 桌面 1280 与移动 390 宽度都不得出现横向溢出；"
        "4) 不要读取本工作区以外的文件，不要访问网络；5) 不要新增素材中没有的活动事实。\n\n"
        "[反问规则] 如果缺少必要信息，创建 QUESTIONS.md（每行一个，前缀 Q:），不要猜测；"
        "没有问题就不要创建该文件。\n\n"
        "[交付] 修改后的 index.html 必须可直接用浏览器打开。"
    )


def verdict_is_improvement(regression: list, resolved: list, unresolved: list,
                           new_issues: list) -> bool:
    return not regression and not unresolved and not new_issues and bool(resolved)


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_baseline_sha(v1_dir: Path) -> str:
    import hashlib

    return hashlib.sha256((Path(v1_dir) / "index.html").read_bytes()).hexdigest()[:12]


def _measurement_to_dict(measurement) -> dict:
    from dataclasses import asdict

    return asdict(measurement)


def _diagnosis_system_prompt() -> str:
    from mentis.diagnosis import SYSTEM_PROMPT

    return SYSTEM_PROMPT + "\n" + GUIDANCE_SYSTEM_ADDENDUM


def _load_registry():
    from mentis.capabilities.registry import load_registry

    return load_registry()


def _bundle_from_markdown(path: Path) -> DiagnosisBundle:
    """恢复场景的兜底：诊断正文从 markdown 重建（仅保留现象行，供复查清单引用）。"""
    bundle = DiagnosisBundle(goal="", observation_source="", model="restored",
                             created_at=datetime.now().isoformat(timespec="seconds"))
    file = Path(path)
    if not file.is_file():
        return bundle
    from mentis.diagnosis import DiagnosisItem

    for line in file.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^- \[([a-z_]+)\] (.+)（证据：(.+?)）$", line.strip())
        if match:
            bundle.valid_diagnoses.append(DiagnosisItem(
                phenomenon=match.group(2), evidence_refs=match.group(3).split("，"),
                severity=match.group(1), interpretation="（从落盘诊断恢复）",
                modification_candidates=["（见落盘诊断文件）"]))
    return bundle


def _resolve_codex() -> str:
    from mentis.collab import resolve_codex_executable

    return resolve_codex_executable()


def _workspace_lock_digest(workspace: Path) -> str:
    import hashlib

    return hashlib.sha256(str(Path(workspace).resolve()).casefold().encode("utf-8")).hexdigest()[:20]


def guidance_from_state(state_path: str | Path, adapter=None, gateway=None,
                        browser_capture=None, measure=None) -> GuidanceCycle:
    """从落盘状态恢复循环（回答决定后继续；不重放历史委托）。"""
    state_path = Path(state_path)
    meta = json.loads(state_path.read_text(encoding="utf-8"))
    obj = GuidanceCycle.__new__(GuidanceCycle)
    obj.run_id = meta["run_id"]
    obj.baseline_dir = Path(meta["baseline_dir"])
    obj.run_dir = state_path.parent
    obj.max_rounds = int(meta.get("max_rounds", 2))
    obj.timeout_s = int(meta.get("timeout_s", 600))
    obj.use_model = True
    obj.include_images = True
    from mentis.browser_observation import capture_local_html

    obj.adapter = adapter or CodexCliAdapter(executable=_resolve_codex())
    obj.gateway = gateway if gateway is not None else ModelGateway(config=ModelConfig())
    obj.browser_capture = browser_capture if browser_capture is not None else capture_local_html
    obj.measure = measure or measure_page
    from mentis.persist import session_from_dict

    obj.session = session_from_dict(
        json.loads((state_path.parent / "session.json").read_text(encoding="utf-8")))
    obj.rounds = [RoundRecord(**r) for r in meta.get("rounds", [])]
    obj.decision_routes = list(meta.get("decision_routes", []))
    obj._restored_model_usage = dict(meta.get("model_usage", {}))
    obj.thread_id = meta.get("thread_id", "")
    obj.status = meta.get("status", "created")
    obj.verdict = meta.get("verdict", "")
    obj.workspace = Path(meta["workspace"])
    obj._preserved = [tuple(x) for x in meta.get("preserved_items", [])]
    obj._dropped_preserved = [tuple(x) for x in meta.get("preserved_dropped", [])]
    obj._goal = meta.get("goal", "")
    obj._constraints = [i.text for i in obj.session.current_brief.constraints]
    obj._v1_dir = obj.run_dir / "v1"
    obj._workspace_ready = obj.workspace.is_dir()
    obj._ensure_v1_reference()  # 从冻结 v1 重建逐段保留项与逐字段必填基准
    obj._finalized_emitted = False
    obj.trace = TraceWriter(obj.run_dir, obj.run_id, executor_version=obj.adapter.cli_version)
    obj.trace.emit("mentis", "run.resumed",
                   f"从落盘状态恢复（进入时状态 {obj.status}）；既有事件续排，不重放历史委托",
                   brief_version=obj.session.current_brief.version)
    return obj
