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

from dataclasses import dataclass, field
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
from mentis.eval_review import (grade_revision, map_candidate_to_dimension,
                                route_by_verifiability, run_blind_review)
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskSpec
from mentis.guidance import (ObservationPack, _CHANGE_VERBS, _NEW_FACT_HINTS,
                             _RESERVED_NOUNS, _diagnosis_system_prompt,
                             _measurement_anomalies, _measurement_to_dict,
                             compose_revision_instruction, deterministic_checks)
from mentis.capabilities.registry import load_registry
from mentis.capabilities.selection import select_for_task
from mentis.models.gateway import ModelConfig, ModelGateway
from mentis.observation import observe_structure
from mentis.render_measurement import measure_page
from mentis.trace import TraceArchiveError, TraceWriter, _sha256_file
from mentis.collab import resolve_codex_executable

RUN_SCHEMA = "mentis-eval-run/1"

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
        shutil.rmtree(workspace)
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
                 measure=None, run_id: str = "", base_dir=None, suite_path=""):
        self.prepared_dir = Path(prepared_dir)
        self.arm = arm
        self.arm_dir = self.prepared_dir / arm
        self.suite_path = str(suite_path)
        self.suite_sha256 = suite_sha256
        self.evidence_path = Path(evidence_path)
        self.run_id = run_id or (datetime.now().strftime("%Y%m%d_%H%M%S")
                                 + "-" + os.urandom(2).hex() + "-"
                                 + arm.replace("/", "-"))
        self.base_dir = Path(base_dir) if base_dir else Path("runtime") / "evaluation" / "e1-runs"
        self.run_dir = self.base_dir / self.run_id
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

    # ---- 准入与准备 ----

    def _admission(self) -> dict:
        """隔离窗口准入（fail-closed）：绑定 prepared/arm/suite 哈希 + 身份 + 版本
        + 双端原始记录重验 + 当前 live 拒读。任一不通过 → 拒绝运行。"""
        result = check_admission(
            self.evidence_path,
            expected={"prepared_dir": str(self.prepared_dir.resolve()),
                      "target_arm": self.arm,
                      "suite_sha256": self.suite_sha256},
            codex_version=self.adapter.cli_version if hasattr(self.adapter, "cli_version") else None,
        )
        iso = result["runtime_answer_isolation"]
        if not iso.get("passed"):
            failed = [f"{c['name']}: {c['detail'][:120]}"
                      for c in iso.get("checks", []) if not c["passed"]]
            raise AdmissionError("隔离准入未通过，运行器拒绝启动：；".join(failed))
        return iso

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
        try:
            prompt_ref = self.trace.archive_prompt(attempt_id, prompt)
        except TraceArchiveError as exc:
            self.trace.emit("mentis", "delegation.prepared",
                            f"prompt 落盘失败，本次委托不发起：{exc}",
                            attempt_id=attempt_id, status="failed")
            raise BudgetExhausted(f"prompt 落盘失败，不发起委托：{exc}") from exc
        self.trace.emit("mentis", "delegation.prepared",
                        f"已归档确切指令并委托 {action_key}（文本见 refs）",
                        attempt_id=attempt_id, refs=[prompt_ref])
        self.trace.record_delegation(attempt_id, 0, prompt_ref, action=action_key)

        def on_event(event: dict, meta: dict):
            try:
                kind = str(event.get("type", "?"))
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                detail = f"{kind} {item.get('type', '')}".strip()
                if kind == "item.completed" and item.get("type") == "agent_message":
                    detail = f"最终消息：「{str(item.get('text', ''))[:160]}」"
                elif item.get("type") == "command_execution":
                    detail = f"命令 {str(item.get('command', ''))[:140]}（{item.get('status', '')}）"
                if meta.get("first"):
                    detail = f"首事件延迟 {meta.get('first_event_latency_s', 0):.1f}s；" + detail
                self.trace.emit("executor", "executor.started" if meta.get("first")
                                else "executor.item", detail, attempt_id=attempt_id,
                                native_thread_id=self.thread_id or None, status="pending")
            except Exception:
                pass

        import time

        started = time.monotonic()
        spec = CodexTaskSpec(workspace=self.workspace, prompt=prompt,
                             brief_version=1, sandbox="workspace-write",
                             timeout_s=self.budget.per_turn_timeout_s,
                             on_event=on_event)
        if resume and self.thread_id:
            result = self.adapter.continue_task(self.thread_id, attempt_id, spec)
        else:
            result = self.adapter.start_task(spec)
        duration = round(time.monotonic() - started, 1)
        self.thread_id = result.native_thread_id or self.thread_id
        self.codex_usage.append({"action": action_key, "status": result.status.value,
                                 "duration_s": duration, "usage": dict(result.usage)})
        self.trace.emit("executor", "executor.completed",
                        f"{action_key} 终态 {result.status.value}；时长 {duration}s；"
                        f"用量 {result.usage or '未取得'}"
                        + (f"；错误 {result.error[:120]}" if result.error else ""),
                        attempt_id=attempt_id,
                        native_thread_id=result.native_thread_id or None,
                        status={"completed": "ok", "failed": "failed",
                                "unknown": "unknown"}[result.status.value],
                        executor_status=result.status.value, usage=dict(result.usage))
        return {"attempt_id": attempt_id, "status": result.status.value,
                "duration_s": duration, "error": result.error,
                "final_text": result.final_response[:400]}

    def _model_call_guard(self):
        assert self.budget is not None
        self.budget.take("model_calls", self.budget.max_model_calls, "外层模型调用")

    # ---- 基线臂（B-0/B-1/B-2/B-3：方法提示词驱动，执行器自检循环） ----

    def _run_baseline(self) -> dict:
        prompt_md = (self.arm_dir / "prompt.md").read_text(encoding="utf-8")
        prompt = (prompt_md.rstrip()
                  + "\n\n[运行说明] 输入与公开资料都在当前工作区（input.json 为目标/反馈/预算，"
                  "input/ 为活动资料，resources/ 为领域资料，observation/ 为既有观察如适用）。"
                  "在当前工作区完成作品（index.html）。\n")
        outcome = {}
        for _ in range(self.budget.max_attempts):
            try:
                outcome = self._dispatch("baseline-turn", prompt,
                                         resume=bool(self.thread_id))
            except BudgetExhausted as exc:
                self.trace.emit("mentis", "run.finalized", str(exc)[:180],
                                status="failed")
                outcome = {"status": "budget_exhausted", "error": str(exc)}
                break
            if outcome["status"] == "completed":
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
        _, resources = select_for_task(
            load_registry(), task_label="评估计划", task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
            observation_capabilities={"structure_only": True, "screenshots": True,
                                     "render_measurement": True}, can_execute=True)
        use_model = bool(self.gateway is not None and self.gateway.has_credentials())
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
        self.trace.emit("mentis", "evaluation_plan.recorded",
                        f"评估计划 v1 已落盘（委托前）：{len(plan.entries)} 个维度、"
                        f"阻塞 {len(plan.blocking_entries())} 条；"
                        f"模型实例化 {plan.model_instantiation.get('status')}",
                        refs=[{"path": str(path), "sha256": plan.sha256, "role": "plan"}])
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
                  and any(n in d.phenomenon for v in _RESERVED_NOUNS)):
                decision, reason = ("needs_user",
                                    "触及用户保留决定：本运行器记录待决并跳过修订"
                                    "（受控模拟协议接线属决定类样本，运行器未接）")
            elif any(h in d.phenomenon for h in _NEW_FACT_HINTS):
                decision, reason = ("needs_user",
                                    "需要素材外新事实：不虚构、不修订（模拟协议接线属决定类样本）")
            else:
                decision, basis = route_by_verifiability(mapped)
                reason = (f"{basis}；映射 {match.get('matched_by')}"
                          f"（得分 {match.get('score')}）")
            records.append({"no": no, "phenomenon": d.phenomenon,
                            "dimension": mapped.entry_id if mapped else "",
                            "decision": decision, "reason": reason[:160]})
            self.trace.emit("mentis", "decision.routed",
                            f"候选审查 {no}「{d.phenomenon[:60]}」→ {decision}；{reason[:120]}",
                            status="ok" if decision in ("adopt", "reject", "keep_as_is")
                            else "pending")
        return records

    def _run_mentis(self, meta: dict) -> dict:
        material_path = self.workspace / "input" / "copy.md"
        material = material_path.read_text(encoding="utf-8") if material_path.is_file() else ""
        plan = self._plan_for(meta, material)
        derived = derive_task_facts(material)
        preserved = [(k, v) for k, v in derived["needles"].items()]
        if not (self.workspace / "index.html").is_file():
            # 端到端样本：先按计划派生验收项委托构建（计划已先落盘）
            requirements = self.workspace / "input" / "requirements.md"
            req_note = ("[公开要求] input/requirements.md" if requirements.is_file()
                        else "[公开要求] input.json policy 字段")
            build_prompt = (f"[目标] {meta['input']['user_goal']}\n"
                            "[素材] input/copy.md（活动事实唯一来源，不得虚构）\n"
                            f"{req_note}\n"
                            "[硬要求] 单文件 index.html、中文、双视口可用无溢出、"
                            "表单必填校验+本地确认提示、无外链不联网；"
                            "不要读取工作区以外文件。\n"
                            + acceptance_block(plan))
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
        context, _ = select_for_task(
            load_registry(), task_label="E1 M 臂诊断", task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
            observation_capabilities={"structure_only": True,
                                     "screenshots": bool(images),
                                     "render_measurement": True}, can_execute=True)
        bundle = run_diagnosis(self.gateway, meta["input"]["user_goal"], user_feedback,
                               pack, context, images=images,
                               system=_diagnosis_system_prompt())
        diagnosis_path = self.run_dir / "diagnosis.md"
        diagnosis_path.write_text(
            f"goal: {meta['input']['user_goal']}\nfeedback: {user_feedback}\n\n"
            + str(getattr(bundle, "render_markdown", "") or ""), encoding="utf-8")
        self.trace.emit("mentis", "observation.diagnosis",
                        f"诊断（{'Mock' if bundle.mock else '真实模型'}）：有效 "
                        f"{len(bundle.valid_diagnoses)}、拒收 {len(bundle.invalid_diagnoses)}",
                        status="ok" if bundle.valid_diagnoses else "unknown",
                        refs=[{"path": str(diagnosis_path),
                               "sha256": _sha256_file(diagnosis_path), "role": "diagnosis"}])
        adjudication = self._adjudicate(bundle, pack, plan)
        adopted = [c for c in adjudication if c["decision"] == "adopt"]
        summary = {"plan_version": 1, "plan_sha256": plan.sha256,
                   "diagnosis": {"valid": len(bundle.valid_diagnoses),
                                 "invalid": len(bundle.invalid_diagnoses),
                                 "mock": bundle.mock},
                   "candidates": adjudication, "revisions": [], "blind": None,
                   "verdict": None, "verdict_basis": ""}
        if not adopted:
            summary["verdict"] = "no_change"
            summary["verdict_basis"] = "成立问题 0 条（含保持现状/不可修订路由）：合格不改，零修订"
            self.trace.emit("mentis", "review.recorded",
                            "候选审查完毕、自主修订 0——合格不改（接受依据=审查记录）",
                            status="ok")
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
        outcome = self._dispatch("revision-1", instruction, resume=bool(self.thread_id))
        summary["revisions"].append(outcome)
        if outcome["status"] != "completed":
            summary["verdict"] = ("unverified" if outcome["status"] == "unknown"
                                  else outcome["status"])
            summary["verdict_basis"] = f"修订执行 {outcome['status']}：不判通过（需核对）"
            return summary
        pack2, images2 = self._observe_full("verify")
        findings, regression, unknown = deterministic_checks(pack2, preserved, form_reqs)
        blind_path = self.run_dir / "blind-review.json"
        try:
            self._model_call_guard()
            blind = run_blind_review(
                self.gateway, plan, pack, images, pack2, images2, blind_path,
                use_model=bool(self.gateway is not None and self.gateway.has_credentials()),
                rng=getattr(self, "_blind_rng", None),
                before_html_sha256=pack.sha256, after_html_sha256=pack2.sha256)
        except BudgetExhausted as exc:
            blind = {"overall": "unknown", "consistent": False, "per_dimension": {},
                     "mock": False, "usage_total": {}}
            verdict, basis = "unverified", f"模型预算耗尽，盲比未执行：{exc}"
        else:
            verdict, basis = grade_revision(
                regression, unknown, pack.sha256 != pack2.sha256, blind,
                targeted_dimensions=[c.get("dimension", "") for c in adopted])
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
        iso = self._admission()
        meta = json.loads((self.arm_dir / "input.json").read_text(encoding="utf-8"))
        policy = meta.get("policy", {})
        self.budget = BudgetState.from_policy(policy)
        self._prepare_workspace()
        self._git_baseline()
        self.trace.emit("mentis", "policy.set",
                        f"E1 臂 {self.arm}；预算 {self.budget.max_attempts} 委托/"
                        f"{self.budget.max_revisions} 修订/{self.budget.max_model_calls}"
                        f" 模型调用/{self.budget.per_turn_timeout_s}s 每 turn",
                        status="ok")
        arm_kind = self.arm.rsplit("/", 1)[-1]
        try:
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
        index = self.workspace / "index.html"
        artifact_sha = hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else ""
        manifest = {
            "schema_version": RUN_SCHEMA,
            "run_id": self.run_id, "arm": self.arm,
            "prepared_dir": str(self.prepared_dir.resolve()),
            "suite_path": self.suite_path,
            "suite_sha256": self.suite_sha256,
            "isolation": {"window_id": iso.get("window_id", ""),
                          "evidence": str(self.evidence_path),
                          "admission": "passed"},
            "budget": {"limits": {"attempts": self.budget.max_attempts,
                                  "revisions": self.budget.max_revisions,
                                  "model_calls": self.budget.max_model_calls,
                                  "per_turn_timeout_s": self.budget.per_turn_timeout_s},
                       "used": dict(self.budget.used)},
            "codex_usage": self.codex_usage,
            "model_usage": self.gateway.usage_summary() if self.gateway else {},
            "artifact": {"path": str(index), "sha256": artifact_sha,
                         "exists": index.is_file()},
            "summary": summary,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "note": "同条件受控运行记录；不构成质量改善结论（E2 对照回答）。"
                    "跨 process 用量分别记账；Codex 用量按 thread 记录，未取得如实标注。",
        }
        self.manifest = manifest
        (self.run_dir / "run-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.trace.finalize(outcome=f"arm {self.arm} verdict={summary.get('verdict')}")
        return manifest
