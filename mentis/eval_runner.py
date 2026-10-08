"""E1 公平运行器（E1 收尾）：同一冻结输入包上运行受测 M 与基线组（B-1 等）。

公平条件（docs/evaluation/e1/README.md）：
- 同一样本两臂使用逐字节相同的输入包（原文/作品/观察/资料/工具/公开要求/预算），
  仅方法提示词不同：B 臂由其 prompt.md 驱动执行器自检循环；M 臂由 Mentis 流程
  （评估计划→专业评审→可验证性路由→修订→盲比复检→判定分级）驱动。
- 每臂独立工作区、独立追踪（trace/ 契约）、独立隔离窗口准入（fail-closed：
  无证据/窗口未开/绑定不符/版本变化一律拒绝运行）。
- 执行预算逐项执行并记录：max_executor_attempts（3 次委托）、max_auto_revisions
  （2 次）、max_guidance_model_calls（4 次，仅 M）、per_turn_timeout_s（600s）。
  用量如实记录，不宣称货币硬上限；跨 process 用量分别记账。

边界：本运行器不证明质量改善（E2 对照才回答），只提供同条件的受控运行与记录；
候选→维度映射是词面启发式；盲比同源模型不建立独立性。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from mentis.contracts import AttemptStatus, Provenance, SourcedItem
from mentis.diagnosis import DiagnosisBundle, render_bundle_markdown, run_diagnosis
from mentis.evaluation_plan import (PlanPersistenceError, acceptance_block,
                                    build_evaluation_plan, derive_task_facts)
from mentis.eval_isolation import IsolationError, check_admission
from mentis.eval_readiness import (ReadinessError, binding, material_state, sha,
                                   workspace_hashes, write_json)
from mentis.eval_review import (grade_revision, map_candidate_to_dimension,
                                route_by_verifiability, run_blind_review)
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec
from mentis.guidance import (ObservationPack, _CHANGE_VERBS, _NEW_FACT_HINTS,
                             _RESERVED_NOUNS, _diagnosis_system_prompt,
                             _measurement_anomalies, _measurement_to_dict,
                             compose_revision_instruction, deterministic_checks)
from mentis.models.gateway import ModelConfig, ModelGateway
from mentis.observation import observe_structure
# C1：M 臂（含计划/诊断链）的任务专业配置；B-1 基线臂不使用，保持不记录
from mentis.professional import ConfigManager
from mentis.render_measurement import measure_page
from mentis.trace import (TraceArchiveError, TraceWriter, _sha256_file,
                          describe_executor_event)
from mentis.collab import resolve_codex_executable

RUN_SCHEMA = "mentis-eval-run/2"

# 与 collab._adjudicate_candidates 相同的保持现状提示词（真实运行 d1 教训）
_KEEP_HINTS = ("保持现状", "保持当前", "保留当前", "无需修改", "维持现状", "保留现状",
               "保持不变", "不建议修改", "无需改动", "无需调整")


class AdmissionError(IsolationError):
    """准入未通过：运行器拒绝启动该臂（fail-closed）。"""


class BudgetExhausted(RuntimeError):
    """执行预算耗尽：停止派发，如实留档，不冒充已解决。"""


@dataclass
class BudgetState:
    """执行预算（来自 suite policy；逐项执行并记账）。"""

    max_attempts: int
    max_revisions: int
    max_model_calls: int
    per_turn_timeout_s: int
    used: dict = field(default_factory=dict)

    @classmethod
    def from_policy(cls, policy: dict) -> "BudgetState":
        return cls(
            max_attempts=int(policy.get("max_executor_attempts", 3)),
            max_revisions=int(policy.get("max_auto_revisions", 2)),
            max_model_calls=int(policy.get("max_guidance_model_calls", 4)),
            per_turn_timeout_s=int(policy.get("per_turn_timeout_s", 600)),
            used={"attempts": 0, "revisions": 0, "model_calls": 0},
        )

    def take(self, key: str, limit: int, label: str) -> None:
        if self.used[key] >= limit:
            raise BudgetExhausted(f"预算耗尽：{label} {self.used[key]}/{limit}")
        self.used[key] += 1


def _flatten_package(arm_dir: Path, workspace: Path) -> None:
    """把冻结输入包展开为运行工作区：arm/workspace/* 到根，其余（input.json、
    prompt.md、input/、resources/、observation/）原样并列。两臂同一展开规则。"""
    if workspace.exists():
        raise AdmissionError(f"拒绝覆盖已有作品工作区：{workspace}")
    workspace.mkdir(parents=True)
    src_ws = arm_dir / "workspace"
    if src_ws.is_dir():
        for item in src_ws.iterdir():
            if item.is_dir():
                shutil.copytree(item, workspace / item.name)
            else:
                shutil.copy2(item, workspace / item.name)
    for name in ("input.json", "prompt.md", "input", "resources", "observation"):
        src = arm_dir / name
        if not src.exists():
            continue
        dest = workspace / name
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)


class FairRun:
    """一次臂运行：准入 → 包展开 → 方法循环 → 记录（trace/ + manifest）。"""

    def __init__(self, prepared_dir, arm: str, out_dir, suite_sha256: str,
                 evidence_path, adapter=None, gateway=None, browser_capture=None,
                 measure=None, run_id: str = "", base_dir=None, suite_path="",
                 run_spec=""):
        self.prepared_dir = Path(prepared_dir)
        self.arm = arm
        self.arm_dir = self.prepared_dir / arm
        self.suite_path = str(suite_path)
        self.suite_sha256 = suite_sha256
        self.evidence_path = Path(evidence_path)
        self.run_spec = str(Path(run_spec).resolve()) if run_spec else ""
        self.execution_binding = {}
        self.run_id = run_id or (datetime.now().strftime("%Y%m%d_%H%M%S")
                                 + "-" + os.urandom(2).hex() + "-"
                                 + arm.replace("/", "-"))
        self.base_dir = Path(base_dir) if base_dir else Path(out_dir)
        self.run_dir = self.base_dir / self.run_id
        self.existed = self.run_dir.exists()
        self.workspace = self.run_dir / "workspace"
        self.adapter = adapter or CodexCliAdapter(executable=resolve_codex_executable())
        self.gateway = gateway if gateway is not None else ModelGateway(config=ModelConfig())
        self.browser_capture = browser_capture
        self.measure = measure or measure_page
        self.thread_id = ""
        self.codex_usage: list = []
        self.trace = TraceWriter(self.run_dir, self.run_id,
                                 executor_version=self.adapter.cli_version)
        self.budget: BudgetState | None = None
        self.manifest: dict = {}
        self._model_stage = ""
        self._model_output_refs: list = []
        self._model_config: dict | None = None
        self.professional: ConfigManager | None = None  # C1：M 臂懒创建
        self.stages = self.trace.stage_tracker()  # O2：阶段起止记录
        self.state = {"schema": "mentis-eval-state/1", "stage": "new", "status": "new",
                      "answers": [], "checks": [], "checked_continuations": 0,
                      "reconciliation_closed": False,
                      "checkpoint": {}, "pending": {}, "model_requests": []}
        self._gateway_seen = 0

    # ---- 准入与准备 ----

    def _admission(self) -> dict:
        """隔离窗口准入（fail-closed）：绑定 prepared/arm/suite 哈希 + 身份 + 版本
        + 双端原始记录重验 + 当前 live 拒读。任一不通过 → 拒绝运行。"""
        expected = {"prepared_dir": str(self.prepared_dir.resolve()),
                    "target_arm": self.arm, "suite_sha256": self.suite_sha256}
        if self.run_spec:
            try:
                self.execution_binding = binding(Path(self.run_spec), self.suite_sha256)
            except ReadinessError as exc:
                raise AdmissionError(str(exc)) from exc
            if self.state.get("execution_binding") and self.state["execution_binding"] != self.execution_binding:
                raise AdmissionError("续办契约与原运行的代码/协议绑定不同")
            if (self.arm.endswith("/M") and isinstance(self.adapter, CodexCliAdapter)
                    and not self.gateway.has_credentials()):
                raise AdmissionError("M 臂缺少外层模型凭据；资源未就绪，零委托")
            expected["execution_binding"] = self.execution_binding
        result = check_admission(
            self.evidence_path,
            expected=expected,
            codex_version=self.adapter.cli_version if hasattr(self.adapter, "cli_version") else None,
        )
        iso = result["runtime_answer_isolation"]
        if not iso.get("passed"):
            failed = [f"{c['name']}: {c['detail'][:120]}"
                      for c in iso.get("checks", []) if not c["passed"]]
            raise AdmissionError("隔离准入未通过，运行器拒绝启动：；".join(failed))
        if self.evidence_path.is_file():
            admissions = self.run_dir / "admission"
            admissions.mkdir(parents=True, exist_ok=True)
            snapshot = admissions / f"evidence-{len(list(admissions.glob('evidence-*.json'))) + 1:03d}.json"
            snapshot.write_bytes(self.evidence_path.read_bytes())
            iso["evidence_snapshot"] = str(snapshot.resolve())
            iso["evidence_sha256"] = sha(snapshot)
            evidence = json.loads(snapshot.read_text(encoding="utf-8"))
            recovery = Path(evidence["window"]["recovery_log"])
            if sha(recovery) != evidence["raw_hashes"].get("recovery-log.json"):
                raise AdmissionError("开窗原始记录已变化，拒绝委托")
            recovery_snapshot = snapshot.with_name(snapshot.stem + "-window-open.json")
            recovery_snapshot.write_bytes(recovery.read_bytes())
            iso["recovery_snapshot"] = str(recovery_snapshot.resolve())
        return iso

    def _drain_model_calls(self):
        """O1：把新增网关调用记入 runner-state 与 trace/model-calls.jsonl。
        阶段与产出引用由调用方在发起调用前/产出落盘后设置；未设置的如实记
        unrecorded，不猜测归属。记录失败计追踪写失败（不静默）。
        C1：_model_config 非空时调用行携带配置版本/哈希与本阶段职责。"""
        calls = getattr(self.gateway, "calls", [])
        for call in calls[self._gateway_seen:]:
            self.state["model_requests"].append({
                "request_id": getattr(call, "request_id", ""),
                "ok": getattr(call, "ok", False), "mock": getattr(call, "mock", True),
                "http_attempted": getattr(call, "http_attempted", False),
                "usage": getattr(call, "usage", {}), "error": getattr(call, "error", "")})
            self.trace.record_model_call(call, stage=self._model_stage,
                                         output_refs=self._model_output_refs,
                                         config=self._model_config)
        self._gateway_seen = len(calls)

    def _professional_manager(self, goal: str) -> ConfigManager:
        """C1：M 臂专业配置管理器（懒创建；恢复/续办时读回已落盘配置并核验）。
        B-1 基线臂不走计划/诊断链，永不创建——其运行保持配置未记录（如实显示）。"""
        if getattr(self, "professional", None) is None:
            budget = getattr(self, "budget", None)
            self.professional = ConfigManager(
                self.run_dir, session=None, goal=goal, trace=self.trace,
                user_source="e1-input-package",
                budget_ref=(f"suite-policy:attempts<={budget.max_attempts},"
                            f"revisions<={budget.max_revisions},model<={budget.max_model_calls}"
                            if budget else "unrecorded"))
        return self.professional

    def _save_state(self):
        self._drain_model_calls()
        self.state.update({"run_id": self.run_id, "arm": self.arm,
                           "prepared_dir": str(self.prepared_dir.resolve()),
                           "suite_path": self.suite_path, "suite_sha256": self.suite_sha256,
                           "evidence_path": str(self.evidence_path.resolve()),
                           "run_spec": self.run_spec, "execution_binding": self.execution_binding,
                           "thread_id": self.thread_id, "codex_usage": self.codex_usage,
                           "budget": asdict(self.budget) if self.budget else None})
        write_json(self.run_dir / "runner-state.json", self.state)

    def _materials(self, meta):
        material = material_state(self.workspace, meta["input"].get("conversation_context", []),
                                  self.state["answers"])
        self.state["material_state"] = material
        write_json(self.run_dir / "material-provenance.json", material)
        return material

    def _request_decision(self, pending, question, stage):
        # 审查修正 A：稳定决定关联标识（注入回答按同一标识关闭对应等待）
        n = int(self.state.get("decision_requests", 0)) + 1
        self.state["decision_requests"] = n
        ix_id = f"IX-E{n}"
        self.state.update(status="needs_user", stage=stage,
                          pending={"fields": pending, "question": question,
                                   "interaction_id": ix_id})
        self.trace.emit("mentis", "decision.requested", question[:180], status="pending",
                        interaction_id=ix_id)
        # O2：等待用户决定是真实阶段——阻塞依赖工作，原因与开始时间入阶段记录
        self.stages.start("waiting_user", blocking=True, reason=question,
                          detail=f"待决阶段 {stage}；依赖工作暂停（沉默≠批准）",
                          interaction_id=ix_id)
        self._save_state()
        return {"status": "needs_user", "verdict": "unverified",
                "verdict_basis": question, "pending": self.state["pending"]}

    def _prepare_workspace(self) -> dict:
        if not self.arm_dir.is_dir():
            raise AdmissionError(f"目标组目录不存在：{self.arm_dir}")
        _flatten_package(self.arm_dir, self.workspace)
        return {}

    def _git_baseline(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True,
                       capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True,
                       capture_output=True)
        subprocess.run(["git", "-c", "user.name=Mentis", "-c", "user.email=mentis@local",
                        "commit", "-qm", "baseline (frozen E1 inputs)"],
                       cwd=self.workspace, check=True, capture_output=True)

    # ---- 派发管线（两臂共享） ----

    def _dispatch(self, action_key: str, prompt: str, resume: bool = False) -> dict:
        """一次执行器委托：先归档确切指令（失败不发起），预算记账，事件/用量入 trace。"""
        assert self.budget is not None
        self.budget.take("attempts", self.budget.max_attempts, "执行器委托")
        attempt_seq = self.budget.used["attempts"]
        attempt_id = f"attempt-{attempt_seq:03d}"
        # O2：委托即「执行器制作/修订」阶段的真实起止（失败/未知如实入阶段记录）
        stage_name = "revise" if action_key.startswith("revision") or "revise" in action_key \
            else "build"
        self.stages.start(stage_name, detail=f"委托 {action_key}", attempt_id=attempt_id,
                          plan={"version": 1, "sha256": (self.state.get("summary") or {})
                                .get("plan_sha256", "")} if isinstance(self.state.get("summary"), dict) else {})
        try:
            prompt_ref = self.trace.archive_prompt(attempt_id, prompt)
        except TraceArchiveError as exc:
            self.stages.finish(status="error", detail="prompt 落盘失败，委托未发起")
            self.trace.emit("mentis", "delegation.prepared",
                            f"prompt 落盘失败，本次委托不发起：{exc}",
                            attempt_id=attempt_id, status="failed")
            raise BudgetExhausted(f"prompt 落盘失败，不发起委托：{exc}") from exc
        self.trace.emit("mentis", "delegation.prepared",
                        f"已归档确切指令并委托 {action_key}（文本见 refs）",
                        attempt_id=attempt_id, refs=[prompt_ref])
        self.trace.record_delegation(attempt_id, 0, prompt_ref, action=action_key)

        def on_event(event: dict, meta: dict):
            # O1：与 collab/guide-revise 共用采集逻辑——公开消息全文归档+哈希引用，
            # 摘要首尾保留；命令状态明确且标「输出未采集」缺口；采集异常不静默。
            try:
                detail, refs = describe_executor_event(
                    event, meta,
                    archive_message=lambda text: self.trace.archive_executor_message(
                        attempt_id, text))
                self.trace.emit("executor", "executor.started" if meta.get("first")
                                else "executor.item", detail, attempt_id=attempt_id,
                                native_thread_id=self.thread_id or None, status="pending",
                                refs=refs)
            except Exception as exc:
                try:
                    self.trace.note_capture_failure(
                        f"executor 事件采集失败：{type(exc).__name__}（原文未入流）")
                except Exception:
                    pass

        import time

        started = time.monotonic()
        spec = CodexTaskSpec(workspace=self.workspace, prompt=prompt,
                             brief_version=1, sandbox="workspace-write",
                             timeout_s=self.budget.per_turn_timeout_s,
                             on_event=on_event)
        self.state.update(status="in_flight", pending={"action": action_key, "prompt": prompt,
                          "started_at": datetime.now().astimezone().isoformat()})
        self._save_state()  # 崩溃也不得自动重新派发；委托预算已消费。
        if resume and self.thread_id:
            result = self.adapter.continue_task(self.thread_id, attempt_id, spec)
        else:
            result = self.adapter.start_task(spec)
        duration = round(time.monotonic() - started, 1)
        self.thread_id = result.native_thread_id or self.thread_id
        self.codex_usage.append({"action": action_key, "status": result.status.value,
                                 "duration_s": duration, "usage": dict(result.usage)})
        self.trace.record_executor_result(attempt_id, result.status,
                                         self.thread_id, dict(result.usage), result.error)
        self.trace.emit("executor", "executor.completed",
                        f"{action_key} 终态 {result.status.value}；时长 {duration}s；"
                        f"用量 {result.usage or '未取得'}"
                        + (f"；错误 {result.error[:120]}" if result.error else ""),
                        attempt_id=attempt_id,
                        native_thread_id=result.native_thread_id or None,
                        status={"completed": "ok", "failed": "failed",
                                "unknown": "unknown"}[result.status.value],
                        executor_status=result.status.value, usage=dict(result.usage))
        # O2：阶段结束按执行器真实终态记录（unknown 保持 unknown，不冒充完成）
        self.stages.finish(status=result.status.value,
                           detail=f"{action_key} 终态 {result.status.value}；时长 {duration}s")
        self.state["status"] = result.status.value
        self.state["pending"]["executor_attempt_id"] = result.attempt_id
        self._save_state()
        return {"attempt_id": attempt_id, "status": result.status.value,
                "duration_s": duration, "error": result.error,
                "final_text": result.final_response[:400]}

    def _model_call_guard(self):
        assert self.budget is not None
        self.budget.take("model_calls", self.budget.max_model_calls, "外层模型调用")

    # ---- 基线臂（B-0/B-1/B-2/B-3：方法提示词驱动，执行器自检循环） ----

    def _run_baseline(self, initial_outcome=None, answer_prompt="") -> dict:
        prompt_md = (self.arm_dir / "prompt.md").read_text(encoding="utf-8")
        prompt = (prompt_md.rstrip()
                  + "\n\n[运行说明] 输入与公开资料都在当前工作区（input.json 为目标/反馈/预算，"
                  "input/ 为活动资料，resources/ 为领域资料，observation/ 为既有观察如适用）。"
                  "在当前工作区完成作品（index.html）。重要事实冲突需要提问时，将问题写入"
                  "QUESTIONS.md 并停止，不自行选择日期。收到回答后删除 QUESTIONS.md。\n"
                  "[已给出的用户上下文]\n"
                  + "\n".join(self.state["meta"]["input"].get("conversation_context", [])))
        prompt = answer_prompt or prompt
        self.state["stage"] = "baseline"
        outcome = {}
        for _ in range(self.budget.max_attempts):
            try:
                if initial_outcome is not None:
                    outcome, initial_outcome = initial_outcome, None
                else:
                    outcome = self._dispatch("baseline-turn", prompt, resume=bool(self.thread_id))
            except BudgetExhausted as exc:
                self.trace.emit("mentis", "run.finalized", str(exc)[:180],
                                status="failed")
                outcome = {"status": "budget_exhausted", "error": str(exc)}
                break
            if outcome["status"] == "completed":
                questions = self.workspace / "QUESTIONS.md"
                if questions.is_file() and questions.read_text(encoding="utf-8").strip():
                    question = questions.read_text(encoding="utf-8")
                    write_json(self.run_dir / f"questions-{self.budget.used['attempts']:03d}.json",
                               {"question": question, "source": "executor", "attempt": outcome["attempt_id"]})
                    self.state.setdefault("questions", []).append(question)
                    material = self._materials(self.state["meta"])
                    if material["pending"] and any(word in question for word in ("日期", "时间")):
                        return self._request_decision(material["pending"], question, "baseline-answer")
                    if any(word in question for word in (*_RESERVED_NOUNS, *_NEW_FACT_HINTS)):
                        if material["decisions"] and "日期" in question:
                            prompt = "[已有用户决定] " + json.dumps(material["decisions"], ensure_ascii=False)
                        else:
                            return self._request_decision([], question, "baseline-answer")
                    else:
                        prompt = "[自主决定] 视觉布局在授权范围内自行决定；已有事实保留，缺失资料不虚构。"
                    prompt += "\n在现有作品上续办，收到回答后删除 QUESTIONS.md。"
                    continue
                break
            if outcome["status"] == "unknown":
                # unknown：锁保留、不自动重发；如实停止等待核对（不冒充完成）
                break
            # failed：预算内可再委托一次（同 thread 续接，交同一执行器）
        return outcome

    # ---- M 臂（Mentis 流程：计划→观察→诊断→路由→修订→盲比→分级） ----

    def _observe_full(self, tag: str):
        index = self.workspace / "index.html"
        out = self.run_dir / "observation" / f"review-{tag}"
        out.mkdir(parents=True, exist_ok=True)
        structure = observe_structure(str(index))
        measurement = self.measure(index, out)
        (out / "measurement.json").write_text(
            json.dumps(_measurement_to_dict(measurement), ensure_ascii=False, indent=2),
            encoding="utf-8")
        snapshot = out / "page-snapshot.html"
        snapshot.write_bytes(index.read_bytes())
        images, image_names = [], []
        capture = self.browser_capture
        if capture is None:
            from mentis.browser_observation import capture_local_html

            capture = capture_local_html
        try:
            browser_obs = capture(index, out)
            for view in browser_obs.views:
                if view.screenshot_path and not view.error:
                    blob = Path(view.screenshot_path).read_bytes()
                    images.append((view.name + ".png", blob))
                    image_names.append(f"{view.name}.png（{view.actual_width}×{view.actual_height}）")
        except Exception as exc:
            self.trace.emit("observer", "observation.render",
                            f"截图采集失败（{type(exc).__name__}）：视觉维度未验证",
                            status="unknown")
        self.trace.emit("observer", "observation.structure",
                        f"观察 {len(structure.items)} 条目（sha256 前 12 位 {structure.sha256[:12]}）",
                        refs=[{"path": str(snapshot), "sha256": structure.sha256,
                               "role": "observation"}])
        return (ObservationPack(structure=structure, measurement=measurement,
                                image_names=image_names), images)

    def _plan_for(self, meta: dict, material: str):
        policy = meta.get("policy", {})
        from mentis.contracts import Brief

        brief_constraints = [SourcedItem(
            policy.get("execution_scope", "本地隔离工作区，不发布、不发送数据"),
            Provenance.SYSTEM, "e1-policy:execution_scope")]
        brief = Brief(brief_id="e1-m", version=1, objective=meta["input"]["user_goal"],
                      constraints=brief_constraints, preferences=[],
                      created_at=datetime.now().isoformat(timespec="seconds"))
        # C1：配置先落盘（一次选择+快照+绑定），计划随后派生；失败→不发起
        professional = self._professional_manager(goal=meta["input"]["user_goal"])
        _, _, resources = professional.ensure()
        self._model_config = professional.binding("plan")
        use_model = bool(self.gateway is not None and self.gateway.has_credentials())
        if use_model:
            self._model_call_guard()
        self._model_stage = "plan_instantiation"
        self._model_output_refs = []
        self.stages.start("plan", detail="评估计划组装（委托前）",
                          config=self._model_config)
        plan = build_evaluation_plan(
            goal=meta["input"]["user_goal"], brief=brief, material_text=material,
            resources=resources, gateway=self.gateway, use_model=use_model,
            notify=lambda m: self.trace.emit("mentis", "decision.routed",
                                             f"计划实例化：{m[:150]}"))
        plan_dir = self.run_dir / "evaluation-plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        path = plan_dir / "plan-v1.json"
        payload = plan.to_json_bytes()
        path.write_bytes(payload)
        plan.sha256 = hashlib.sha256(payload).hexdigest()
        self._model_output_refs = [{"path": str(path), "sha256": plan.sha256, "role": "plan"}]
        self._drain_model_calls()
        self._model_stage = ""
        self._model_output_refs = []
        self._model_config = None
        professional.note_plan(plan)
        self.trace.emit("mentis", "evaluation_plan.recorded",
                        f"评估计划 v1 已落盘（委托前）：{len(plan.entries)} 个维度、"
                        f"阻塞 {len(plan.blocking_entries())} 条；"
                        f"模型实例化 {plan.model_instantiation.get('status')}",
                        refs=[{"path": str(path), "sha256": plan.sha256, "role": "plan"}])
        self.stages.finish(status="ok",
                           detail=f"计划 v1 落盘；{len(plan.entries)} 个维度；"
                                  f"模型实例化 {plan.model_instantiation.get('status')}")
        return plan

    def _adjudicate(self, bundle, pack, plan) -> list:
        """与 collab._adjudicate_candidates 相同的守卫链与可验证性路由（V2 语义）。"""
        records = []
        measurement_errors = [e for v in pack.measurement.viewports for e in v.errors]
        trusted = [v for v in pack.measurement.viewports if not _measurement_anomalies(v)]
        total_overflow = sum(v.overflow_px for v in trusted)
        total_offenders = sum(len(v.overflowing) for v in trusted)
        for idx, d in enumerate(bundle.valid_diagnoses, 1):
            no = f"d{idx}"
            joined = (d.phenomenon + "｜" + d.interpretation + "｜"
                      + "；".join(d.modification_candidates))
            mapped, match = map_candidate_to_dimension(joined, plan)
            if any(h in joined for h in _KEEP_HINTS):
                decision, reason = "keep_as_is", "候选建议为保持现状：不进入修订任务"
            elif "溢出" in joined and trusted and total_overflow == 0 and total_offenders == 0:
                decision, reason = "reject", "候选主张溢出但可信测量为 0px：证据不支持，驳回"
            elif "溢出" in joined and (measurement_errors or not trusted):
                decision, reason = "pending", "候选主张溢出但测量不可信/缺失：不据此修订"
            elif (any(v in d.phenomenon for v in _CHANGE_VERBS)
                  and any(n in d.phenomenon for n in _RESERVED_NOUNS)):
                decision, reason = ("needs_user",
                                    "触及用户保留决定：待用户决定，不自行修改")
            elif any(h in d.phenomenon for h in _NEW_FACT_HINTS):
                decision, reason = ("needs_user",
                                    "需要素材外新事实：待用户提供，不虚构")
            else:
                decision, basis = route_by_verifiability(mapped)
                reason = (f"{basis}；映射 {match.get('matched_by')}"
                          f"（得分 {match.get('score')}）")
            records.append({"no": no, "phenomenon": d.phenomenon,
                            "dimension": mapped.entry_id if mapped else "",
                            "decision": decision, "reason": reason[:160],
                            "matched_by": match.get("matched_by"), "mapping_score": match.get("score"),
                            "guard": "overflow" if "溢出" in joined and decision in ("reject", "pending") else ""})
            self.trace.emit("mentis", "decision.routed",
                            f"候选审查 {no}「{d.phenomenon[:60]}」→ {decision}；{reason[:120]}",
                            status="ok" if decision in ("adopt", "reject", "keep_as_is")
                            else "pending")
        return records

    def _load_plan(self):
        from mentis.evaluation_plan import EvaluationPlan

        path = self.run_dir / "evaluation-plan" / "plan-v1.json"
        plan = EvaluationPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        plan.sha256 = sha(path)
        return plan

    def _run_mentis(self, meta: dict, resume_build=False) -> dict:
        materials = self._materials(meta)
        if materials["pending"]:
            return self._request_decision(materials["pending"],
                "公开活动素材中的事实冲突，请确认日期；确认前停止构建。", "m-facts")
        material = materials["material"]
        if not material:
            raise ReadinessError("公开活动素材缺失，不允许用空素材构建")
        plan = self._load_plan() if resume_build else self._plan_for(meta, material)
        derived = derive_task_facts(material)
        preserved = [(k, v) for k, v in derived["needles"].items()]
        if not (self.workspace / "index.html").is_file():
            # 端到端样本：先按计划派生验收项委托构建（计划已先落盘）
            requirements = self.workspace / "input" / "requirements.md"
            req_note = ("[公开要求] input/requirements.md" if requirements.is_file()
                        else "[公开要求] input.json policy 字段")
            build_prompt = (f"[目标] {meta['input']['user_goal']}\n"
                            f"[素材] {materials['effective_path']}（原始来源见 input/copy*.md；不得虚构）\n"
                            "[用户上下文] " + "；".join(meta["input"].get("conversation_context", [])) + "\n"
                            + "[事实决定] " + json.dumps(materials["decisions"], ensure_ascii=False) + "\n"
                            +
                            f"{req_note}\n"
                            "[硬要求] 单文件 index.html、中文、双视口可用无溢出、"
                            "表单必填校验+本地确认提示、无外链不联网；"
                            "不要读取工作区以外文件。\n"
                            + acceptance_block(plan))
            self.state["stage"] = "m-build"
            outcome = self._dispatch("build-page", build_prompt)
            if outcome["status"] != "completed":
                return {"plan_version": 1, "plan_sha256": plan.sha256,
                        "revisions": [outcome], "candidates": [],
                        "verdict": ("unverified" if outcome["status"] == "unknown"
                                    else outcome["status"]),
                        "verdict_basis": f"构建执行 {outcome['status']}：不判通过（需核对）"}
        pack, images = self._observe_full("first")
        form_reqs = sorted({i.text for i in pack.structure.items
                            if i.kind in ("input", "textarea")
                            and i.extra.get("required") == "yes"})

        user_feedback = meta["input"].get("user_feedback", "")
        self._model_call_guard()
        # C1：诊断取用同一冻结配置的领域依据（恢复/续办时读回并核验，不重冻结）
        professional = self._professional_manager(goal=meta["input"]["user_goal"])
        _, context, _ = professional.ensure()
        self._model_config = professional.binding("diagnose")
        self._model_stage = "diagnosis"
        self._model_output_refs = []
        self.stages.start("diagnose",
                          detail=f"诊断输入：观察 {len(pack.structure.items)} 条目、截图 {len(images)} 张",
                          config=self._model_config)
        bundle = run_diagnosis(self.gateway, meta["input"]["user_goal"], user_feedback,
                               pack, context, images=images,
                               system=_diagnosis_system_prompt())
        diagnosis_path = self.run_dir / "diagnosis.md"
        diagnosis_path.write_text(
            f"goal: {meta['input']['user_goal']}\nfeedback: {user_feedback}\n\n"
            + render_bundle_markdown(bundle), encoding="utf-8")
        self._model_output_refs = [{"path": str(diagnosis_path),
                                    "sha256": _sha256_file(diagnosis_path),
                                    "role": "diagnosis"}]
        self._drain_model_calls()
        self._model_stage = ""
        self._model_output_refs = []
        self._model_config = None
        self.trace.emit("mentis", "observation.diagnosis",
                        f"诊断（{'Mock' if bundle.mock else '真实模型'}）：有效 "
                        f"{len(bundle.valid_diagnoses)}、拒收 {len(bundle.invalid_diagnoses)}",
                        status="ok" if bundle.valid_diagnoses else "unknown",
                        refs=[{"path": str(diagnosis_path),
                               "sha256": _sha256_file(diagnosis_path), "role": "diagnosis"}])
        self.stages.finish(status="ok" if bundle.valid_diagnoses else "unknown",
                           detail=f"有效 {len(bundle.valid_diagnoses)}、拒收 {len(bundle.invalid_diagnoses)}"
                                  f"（{'Mock' if bundle.mock else '真实模型'}）")
        self.stages.start("adjudicate", detail="候选审查与可验证性路由",
                          config=professional.binding("adjudicate"))
        adjudication = self._adjudicate(bundle, pack, plan)
        self.stages.finish(status="ok",
                           detail=f"{len(adjudication)} 条候选处置完毕；"
                                  f"采纳 {sum(1 for c in adjudication if c['decision'] == 'adopt')}")
        adopted = [c for c in adjudication if c["decision"] == "adopt"]
        summary = {"plan_version": 1, "plan_sha256": plan.sha256,
                   "diagnosis": {"valid": len(bundle.valid_diagnoses),
                                 "invalid": len(bundle.invalid_diagnoses),
                                 "mock": bundle.mock},
                   "candidates": adjudication, "revisions": [], "blind": None,
                   "verdict": None, "verdict_basis": ""}
        if not adopted:
            pending = [c for c in adjudication if c["decision"] in ("pending", "needs_user")]
            measurement_unknown = any(_measurement_anomalies(v) for v in pack.measurement.viewports)
            summary["verdict"] = "unverified" if pending or measurement_unknown else "no_change"
            summary["verdict_basis"] = ("存在待决/不可信观察，零修订，不能验收"
                                        if pending or measurement_unknown else "可成立的自主修订 0 条，保留作品；质量仍待独立评分")
            self.trace.emit("mentis", "review.recorded",
                            summary["verdict_basis"], status="unknown" if pending else "ok")
            return summary
        # 成立问题 → 修订指令（含计划验收项）→ 预算内委托
        defect_ids = [c["no"] for c in adopted]
        adopted_items = [bundle.valid_diagnoses[int(c["no"][1:]) - 1] for c in adopted]
        filtered = DiagnosisBundle(goal=bundle.goal,
                                   observation_source=bundle.observation_source,
                                   model=bundle.model, valid_diagnoses=adopted_items)
        self.budget.take("revisions", self.budget.max_revisions, "自动修订")  # type: ignore[union-attr]
        instruction = (compose_revision_instruction(
            filtered, defect_ids, pack, preserved,
            [i.text for i in plan.entries[:0]] or
            [policy_text for policy_text in [meta.get("policy", {}).get("execution_scope", "")]])
            .rstrip() + "\n\n" + acceptance_block(plan))
        self.state["stage"] = "m-revision"
        self.state["checkpoint"] = {"summary": summary, "adopted": adopted,
                                    "preserved": preserved, "form_reqs": form_reqs,
                                    "before": self._pack_checkpoint(pack, images)}
        outcome = self._dispatch("revision-1", instruction, resume=bool(self.thread_id))
        summary["revisions"].append(outcome)
        if outcome["status"] != "completed":
            summary["verdict"] = ("unverified" if outcome["status"] == "unknown"
                                  else outcome["status"])
            summary["verdict_basis"] = f"修订执行 {outcome['status']}：不判通过（需核对）"
            return summary
        return self._verify_revision(summary, adopted, pack, images, preserved, form_reqs)

    def _pack_checkpoint(self, pack, images):
        refs = []
        for name, blob in images:
            path = self.run_dir / "observation" / "checkpoint" / Path(name).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            refs.append({"path": str(path.resolve()), "sha256": sha(path)})
        return {"structure": asdict(pack.structure), "measurement": asdict(pack.measurement),
                "image_names": pack.image_names, "images": refs}

    def _restore_pack(self, data):
        from mentis.observation import ObservationItem, ObservationRecord
        from mentis.render_measurement import (RenderMeasurement, ViewportMeasurement,
                                               FormCheck, OverflowElement)

        structure = dict(data["structure"])
        structure["items"] = [ObservationItem(**i) for i in structure["items"]]
        measurement = dict(data["measurement"])
        views = []
        for v in measurement["viewports"]:
            v = dict(v)
            for key in ("overflowing", "clipped_text"):
                v[key] = [OverflowElement(**e) for e in v[key]]
            v["forms"] = [FormCheck(**f) for f in v["forms"]]
            views.append(ViewportMeasurement(**v))
        measurement["viewports"] = views
        images = []
        for ref in data["images"]:
            p = Path(ref["path"])
            if sha(p) != ref["sha256"]:
                raise ReadinessError("续办前观察图片哈希不一致")
            images.append((p.name, p.read_bytes()))
        return ObservationPack(structure=ObservationRecord(**structure),
                               measurement=RenderMeasurement(**measurement),
                               image_names=data["image_names"]), images

    def _verify_revision(self, summary, adopted, pack, images, preserved, form_reqs):
        plan = self._load_plan()
        self.stages.start("reverify", detail="确定性复检 + 盲比两序")
        pack2, images2 = self._observe_full("verify")
        findings, regression, unknown = deterministic_checks(pack2, preserved, form_reqs)
        blind_path = self.run_dir / "blind-review.json"
        _prof_binding = (self.professional.binding("reverify")
                         if getattr(self, "professional", None) is not None else None)
        try:
            self._model_call_guard()
            self._model_stage = "blind_review"
            self._model_output_refs = []
            self._model_config = _prof_binding
            blind = run_blind_review(
                self.gateway, plan, pack, images, pack2, images2, blind_path,
                use_model=bool(self.gateway is not None and self.gateway.has_credentials()),
                rng=getattr(self, "_blind_rng", None),
                before_html_sha256=pack.sha256, after_html_sha256=pack2.sha256,
                professional=_prof_binding)
            if blind_path.is_file():
                self._model_output_refs = [{"path": str(blind_path),
                                            "sha256": _sha256_file(blind_path),
                                            "role": "review"}]
            verdict, basis = grade_revision(
                regression, unknown, pack.sha256 != pack2.sha256, blind,
                targeted_dimensions=[c.get("dimension", "") for c in adopted])
        except BudgetExhausted as exc:
            blind = {"overall": "unknown", "consistent": False, "per_dimension": {},
                     "mock": False, "usage_total": {}}
            verdict, basis = "unverified", f"模型预算耗尽，盲比未执行：{exc}"
        finally:
            self._drain_model_calls()
            self._model_stage = ""
            self._model_output_refs = []
            self._model_config = None
        summary["blind"] = {"overall": blind["overall"], "consistent": blind["consistent"],
                            "per_dimension": blind["per_dimension"],
                            "mock": blind["mock"], "usage_total": blind["usage_total"],
                            "detail": str(blind_path)}
        summary["verdict"] = verdict
        summary["verdict_basis"] = basis
        summary["deterministic"] = {"findings": findings, "regression": regression,
                                    "unknown": unknown}
        blind_refs = ([{"path": str(blind_path), "sha256": _sha256_file(blind_path),
                        "role": "review"}] if blind_path.is_file() else [])
        self.stages.finish(status="ok" if not regression and not unknown else "unknown",
                           detail=f"判定 {verdict}：{basis[:80]}")
        self.trace.emit("mentis", "review.recorded",
                        f"修订复检：回归 {regression or '无'}；盲比两序"
                        f"{'一致' if blind['consistent'] else '不一致'}"
                        f"（总体 {blind['overall']}）；判定 {verdict}——{basis[:80]}",
                        status="ok" if not regression and not unknown else "failed",
                        refs=blind_refs)
        return summary

    # ---- 入口 ----

    def run(self) -> dict:
        """准入 → 展开 → 方法循环 → manifest。准入失败抛 AdmissionError（零委托）。"""
        if self.existed:
            raise AdmissionError(f"运行目录已存在；使用 eval-answer/eval-continue 续办：{self.run_dir}")
        # O2：生产者活动信号（独立于旁观器；后台线程，停止后信号自然过期）
        self.trace.start_heartbeat(producer_role=f"eval-run:{self.arm}")
        iso = self._admission()
        meta = json.loads((self.arm_dir / "input.json").read_text(encoding="utf-8"))
        self.state["meta"] = meta
        policy = meta.get("policy", {})
        self.budget = BudgetState.from_policy(policy)
        self.trace.emit("mentis", "policy.set",
                        f"E1 臂 {self.arm}；预算 {self.budget.max_attempts} 委托/"
                        f"{self.budget.max_revisions} 修订/{self.budget.max_model_calls}"
                        f" 模型调用/{self.budget.per_turn_timeout_s}s 每 turn",
                        status="ok")
        arm_kind = self.arm.rsplit("/", 1)[-1]
        try:
            self._prepare_workspace()
            self._git_baseline()
            initial = self.workspace / "index.html"
            if initial.is_file():
                before = self.run_dir / "versions" / "initial.html"
                before.parent.mkdir(parents=True, exist_ok=True)
                before.write_bytes(initial.read_bytes())
            self._save_state()
            if arm_kind == "M":
                summary = self._run_mentis(meta)
            else:
                summary = self._run_baseline()
                summary.setdefault("verdict", None)
        except BudgetExhausted as exc:
            summary = {"verdict": "unverified", "verdict_basis": f"预算耗尽：{exc}",
                       "budget_exhausted": True}
            self.trace.emit("mentis", "run.finalized", f"预算耗尽收尾：{exc}"[:180],
                            status="failed")
        except Exception as exc:
            # 观察或方法异常也留下 manifest；不把异常等同质量失败。
            if self.state["status"] == "in_flight":
                self.state["status"] = "unknown"
            else:
                self.state["status"] = "error"
            summary = {"verdict": "unverified", "verdict_basis": f"运行异常：{type(exc).__name__}",
                       "error_type": type(exc).__name__}
            self.trace.emit("mentis", "run.error", summary["verdict_basis"], status="failed")
        return self._finish(summary, iso)

    def _finish(self, summary, iso):
        self.stages.start("finalize",
                          detail=f"收尾：判定 {summary.get('verdict', 'unrecorded')}；"
                                 f"状态 {self.state.get('status')}")
        self.stages.finish(status="ok")
        # 兜底：未关闭阶段如实记中断；待决的等待用户阶段保持打开（等待跨进程持续）
        self.stages.close_stray(keep_waiting=True)
        self.trace.stop_heartbeat()  # 信号随后自然过期，不写「已停止」冒充生产者状态
        self.state["summary"] = summary
        if self.state["status"] not in ("unknown", "needs_user", "error", "in_flight"):
            self.state["status"] = "finished"
        self._save_state()
        index = self.workspace / "index.html"
        artifact_sha = hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else ""
        versions = self.run_dir / "versions"
        versions.mkdir(exist_ok=True)
        snapshots = list(versions.glob("final-*.html"))
        final = versions / f"final-{len(snapshots) + 1:03d}.html"
        if index.is_file():
            final.write_bytes(index.read_bytes())
        manifest = {
            "schema_version": RUN_SCHEMA,
            "run_id": self.run_id, "arm": self.arm,
            "prepared_dir": str(self.prepared_dir.resolve()),
            "suite_path": self.suite_path,
            "suite_sha256": self.suite_sha256,
            "execution_binding": self.execution_binding,
            "execution_status": (self.codex_usage[-1]["status"] if self.codex_usage
                                 else "not_dispatched"),
            "runner_status": self.state["status"],
            "evidence_mode": ("live_local" if isinstance(self.adapter, CodexCliAdapter)
                              and not self.adapter._runner_injected
                              and self.adapter._default_stream_path
                              and not any(r["mock"] for r in self.state["model_requests"])
                              else "mechanism_fixture"),
            "isolation": {"window_id": iso.get("window_id", ""),
                          "evidence": str(self.evidence_path),
                          "evidence_snapshot": iso.get("evidence_snapshot", ""),
                          "evidence_sha256": iso.get("evidence_sha256", ""),
                          "recovery_snapshot": iso.get("recovery_snapshot", ""),
                          "admission": "passed"},
            "budget": {"limits": {"attempts": self.budget.max_attempts,
                                  "revisions": self.budget.max_revisions,
                                  "model_calls": self.budget.max_model_calls,
                                  "per_turn_timeout_s": self.budget.per_turn_timeout_s},
                       "used": dict(self.budget.used)},
            "codex_usage": self.codex_usage,
            "model_usage": self.gateway.usage_summary() if self.gateway else {},
            "model_requests": {"total": len(self.state["model_requests"]),
                               "http_attempts": sum(r["http_attempted"] for r in self.state["model_requests"]),
                               "records": self.state["model_requests"]},
            "artifact": {"path": str(final.resolve()) if index.is_file() else str(index.resolve()),
                         "workspace_path": str(index.resolve()), "sha256": artifact_sha,
                         "exists": index.is_file()},
            "decisions": self.state.get("material_state", {}).get("decisions", []),
            "questions": self.state.get("questions", []),
            "controlled_answers": self.state["answers"],
            "checked_continuations": self.state["checked_continuations"],
            "reconciliation": self.state["checks"],
            "state_sha256": sha(self.run_dir / "runner-state.json"),
            "summary": summary,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "note": "同条件受控运行记录；不构成质量改善结论（E2 对照回答）。"
                    "跨 process 用量分别记账；Codex 用量按 thread 记录，未取得如实标注。",
        }
        self.manifest = manifest
        write_json(self.run_dir / "run-manifest.json", manifest)
        self.trace.finalize(outcome=f"arm {self.arm} verdict={summary.get('verdict')}")
        return manifest

    @classmethod
    def reopen(cls, run_dir, **dependencies):
        directory = Path(run_dir).resolve()
        state_path = directory / "runner-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema") != "mentis-eval-state/1" or directory.name != state.get("run_id"):
            raise AdmissionError("检查点格式/运行目录不匹配，拒绝续办")
        manifest_path = directory / "run-manifest.json"
        if not manifest_path.is_file():
            raise AdmissionError("上次运行未完整收尾：先人工核对，不能自动续办")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sha(state_path) != manifest.get("state_sha256"):
            raise AdmissionError("运行检查点哈希不一致，拒绝续办")
        run = cls(state["prepared_dir"], state["arm"], directory.parent,
                  state["suite_sha256"], state["evidence_path"],
                  run_id=state["run_id"], suite_path=state["suite_path"],
                  run_spec=state["run_spec"], **dependencies)
        run.state = state
        run.execution_binding = state["execution_binding"]
        run.thread_id = state["thread_id"]
        run.codex_usage = state["codex_usage"]
        run.budget = BudgetState(**state["budget"])
        run.manifest = manifest
        return run

    def _archive_previous(self):
        history = self.run_dir / "history"
        history.mkdir(exist_ok=True)
        sequence = len(list(history.glob("manifest-*.json"))) + 1
        for src, name in (("run-manifest.json", "manifest"), ("runner-state.json", "state")):
            shutil.copyfile(self.run_dir / src, history / f"{name}-{sequence:03d}.json")

    def answer(self, answer: str, answered_by: str, source="controlled_simulation"):
        """仅在已请求决定之后注入回答；受控模拟与真人来源分别记录。"""
        if self.state["status"] != "needs_user" or not answer.strip() or not answered_by.strip():
            raise AdmissionError("没有待决问题或缺少回答/回答人，拒绝注入")
        iso = self._admission()
        material = self._materials(self.state["meta"])
        record = {"answer": answer, "answered_by": answered_by, "source": source,
                  "created_at": datetime.now().astimezone().isoformat(),
                  "question": self.state["pending"].get("question", "")}
        # 当前公开案例只接日期决定。其它问题继续待决，不能泛用答案绕过保留决定。
        trial = material_state(self.workspace, self.state["meta"]["input"].get("conversation_context", []),
                               self.state["answers"] + [record], write_effective=False)
        if not material["pending"] or trial["pending"]:
            raise AdmissionError("回答未唯一解决公开素材的日期冲突；本契约不处理其它决定")
        if self.budget.used["attempts"] >= self.budget.max_attempts:
            raise AdmissionError("委托预算耗尽，不能提交回答续办")
        self._archive_previous()
        stage = self.state["stage"]
        self.state["answers"].append(record)
        ix_id = str(self.state.get("pending", {}).get("interaction_id", ""))
        self.trace.emit("user", "decision.received", f"{source}：{answer[:120]}", status="ok",
                        interaction_id=ix_id or None)
        # 等待用户阶段关闭：按关联标识只关闭对应等待；无法匹配时退回最近遗留等待
        if not self.stages.finish_waiting(status="ok",
                                          detail=f"用户决定已注入（来源 {source}）；依赖工作继续",
                                          interaction_id=ix_id or None):
            self.stages.finish(status="ok", detail=f"用户决定已注入（来源 {source}）")
        self.state["status"] = "ready"
        self._save_state()
        try:
            summary = (self._run_mentis(self.state["meta"]) if stage == "m-facts"
                       else self._run_baseline(answer_prompt=f"[用户确认，来源 {source}] {answer}\n"
                                                   "在当前作品上续办，事实以此次确认为准，删除 QUESTIONS.md。"))
        except Exception as exc:
            summary = {"verdict": "unverified", "error_type": type(exc).__name__}
            self.state["status"] = "unknown" if self.state["status"] == "in_flight" else "error"
        return self._finish(summary, iso)

    def checked_continue(self, checked_by: str, basis: str, artifact_complete=False, process_check=None):
        """核对后最多同 thread 续办一次；unknown 尝试和作品核对不改写为成功。"""
        if (self.state["status"] != "unknown" or self.state["checked_continuations"] >= 1
                or self.state.get("reconciliation_closed")):
            raise AdmissionError("仅允许 unknown 后一次核对续办")
        if not checked_by.strip() or not basis.strip():
            raise AdmissionError("必须记录核对人和核对依据")
        iso = self._admission()
        from mentis.eval_isolation import _codex_processes_since

        pending = self.state["pending"]
        if process_check is not None:
            processes = process_check(pending["started_at"])
        else:
            # 真实查询带 scope：node 名进程仅在本运行路径上出现才算写入者
            # （会话工具的无关 MCP server 不阻塞续办）；测试替身保持单参签名。
            processes = _codex_processes_since(pending["started_at"],
                                               scope=str(self.run_dir))
        if processes is None or processes:
            raise AdmissionError("未确认执行器已停止；拒绝解锁和续办")
        hashes = workspace_hashes(self.workspace)
        questions_file = self.workspace / "QUESTIONS.md"
        question_pending = (questions_file.is_file()
                            and questions_file.read_text(encoding="utf-8").strip() != "")
        if "index.html" not in hashes and not question_pending:
            raise AdmissionError("作品缺失且无待决问题，无法核对续办")
        if not artifact_complete and "index.html" in hashes and (
                not self.thread_id or self.budget.used["attempts"] >= self.budget.max_attempts):
            raise AdmissionError("缺少原会话或委托预算耗尽，不能续办")
        if artifact_complete and "index.html" not in hashes:
            raise AdmissionError("artifact-complete 核对需要工作区已有作品")
        check = {"checked_by": checked_by, "basis": basis,
                 "checked_at": datetime.now().astimezone().isoformat(),
                 "processes": processes, "workspace_hashes": hashes,
                 "artifact_complete": artifact_complete, "original_status": "unknown"}
        stage = self.state["stage"]
        if stage.startswith("m-"):
            plan_path = self.run_dir / "evaluation-plan" / "plan-v1.json"
            if sha(plan_path) != self.state["summary"].get("plan_sha256"):
                raise AdmissionError("评估计划在 unknown 后被修改；拒绝续办")
        if stage == "m-revision":
            self._restore_pack(self.state["checkpoint"]["before"])
        self._archive_previous()
        self.state["checks"].append(check)
        self.trace.emit("user", "executor.reconciled", f"核对者 {checked_by}；{basis[:120]}", status="ok")
        if artifact_complete:
            self.state["reconciliation_closed"] = True
            summary = dict(self.state["summary"])
            summary.update(verdict="unknown", artifact_state="unknown_artifact_checked",
                           verdict_basis="人工核对作品已包含预期修改；原执行状态仍 unknown，未续办")
            return self._finish(summary, iso)
        if "index.html" not in hashes:
            # 问题待决核对（2026-10-04 真实运行发现的覆盖缺口）：决定样本中执行器
            # 在构建前正确停止提问，但 turn 终态可能因失败子命令被保守判 unknown，
            # 而基线循环只在 completed 上检查 QUESTIONS.md。此分支不重发委托、
            # 不计续办次数：按工作区 QUESTIONS.md 走 needs_user（与基线循环
            # completed 分支同语义），回答仍只能经 eval-answer 注入。
            question = questions_file.read_text(encoding="utf-8")
            if stage != "baseline" or not any(word in question for word in ("日期", "时间")):
                raise AdmissionError("无产物且非日期类待决问题：续办机制不处理该状态")
            seq = self.budget.used["attempts"]
            write_json(self.run_dir / f"questions-{seq:03d}.json",
                       {"question": question, "source": "executor",
                        "attempt": pending.get("executor_attempt_id", "")})
            self.state.setdefault("questions", []).append(question)
            self._save_state()
            material = self._materials(self.state["meta"])
            return self._finish(
                self._request_decision(material["pending"], question, "baseline-answer"), iso)
        self.state["checked_continuations"] += 1
        self._save_state()
        try:
            unlock = getattr(self.adapter, "release_reconciled_lock", None)
            if unlock:
                unlock(self.workspace, pending["started_at"], pending.get("executor_attempt_id", ""))
            outcome = self._dispatch(pending["action"],
                "[核对后续办] 原尝试状态 unknown。先核对现有文件，继续尚未完成部分，保留已完成修改。\n"
                + pending["prompt"], resume=True)
            if outcome["status"] != "completed":
                summary = {"verdict": "unverified", "revisions": [outcome],
                           "verdict_basis": "核对续办未完成，终止本臂，不能再次续办"}
            elif stage == "baseline":
                summary = self._run_baseline(initial_outcome=outcome)
            elif stage == "m-build":
                summary = self._run_mentis(self.state["meta"], resume_build=True)
            elif stage == "m-revision":
                checkpoint = self.state["checkpoint"]
                pack, images = self._restore_pack(checkpoint["before"])
                summary = dict(checkpoint["summary"])
                summary["revisions"] = [outcome]
                summary = self._verify_revision(summary, checkpoint["adopted"], pack, images,
                                                checkpoint["preserved"], checkpoint["form_reqs"])
            else:
                raise ReadinessError(f"不支持的续办阶段：{stage}")
        except Exception as exc:
            self.state["status"] = "unknown" if self.state["status"] == "in_flight" else "error"
            summary = {"verdict": "unverified", "error_type": type(exc).__name__}
        return self._finish(summary, iso)
