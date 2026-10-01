"""P2.3 默认委托、可选共同设计的 CLI 协作循环。

把既有件接成最小闭环：目标 → 缺口分流与版本化 brief → Codex 委托（隔离工作区）
→ 实际产物 → 双视口截图与结构观察 → 硬要求检查 → 有界自主修复 → 用户可见结果。

纪律：
- notify_progress 不阻塞；request_decision 只阻塞依赖动作（P1.2 机制）。
- 执行器反问先由 Mentis 分流：素材可答、常规专业问题自主回答；只有用户独有
  事实/取舍/许可才请求用户。
- 共同设计是用户主动选择（--design）；"不知道"记为暂定假设继续，不阻塞。
- T4 补修：Codex 首稿是供 Mentis 审查的工作稿——交付前做专业评审（结构+渲染
  测量+截图+模型诊断），候选逐条审查（采纳/驳回/保持现状/待用户，理由留档）；
  成立问题在预算内转成修订指令委托 Codex 并复检；合格不改但留接受依据。
- 不声称货币硬上限；只记录调用次数、时长与可取得的 token 用量。
"""

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mentis.capabilities.registry import load_registry
from mentis.capabilities.routing import Gap, route_gap
from mentis.capabilities.selection import select_for_task
from mentis.contracts import AttemptStatus, DelegationPolicy, Disposition, Provenance, SourcedItem
from mentis.diagnosis import DiagnosisBundle, render_bundle_markdown, run_diagnosis
from mentis.executors.codex_cli import CodexCliAdapter, CodexTaskResult, CodexTaskSpec
# T4 补修：交付前专业评审复用 guidance 的观察/诊断/指令/复检机制（提取为模块级函数），
# 不另建平行流程。
from mentis.guidance import (AUTONOMOUS_DECISION_TEXT, ObservationPack, _CHANGE_VERBS,
                             _NEW_FACT_HINTS, _RESERVED_NOUNS, _diagnosis_system_prompt,
                             _measurement_anomalies, _measurement_to_dict,
                             compose_revision_instruction, deterministic_checks,
                             run_model_review)
from mentis.models.gateway import ModelConfig, ModelGateway
from mentis.observation import observe_structure
from mentis.persist import save_session, session_from_dict
from mentis.render_measurement import measure_page
from mentis.session import TaskSession
from mentis.trace import TraceArchiveError, TraceWriter, _sha256_file

COLLAB_DIR = Path("runtime") / "collab"
STATE_VERSION = "p2.3"
# trace 契约 §7 状态映射：执行器终态 → 事件 status
STATUS_VALUE = {"completed": "ok", "failed": "failed", "unknown": "unknown",
                "dispatched": "pending", "running": "pending"}
COPY_DECK = """# 拾光咖啡 · 周六手冲入门工作坊（合成素材，虚构品牌）

- 活动名称：周六手冲入门工作坊
- 日期时间：2026-11-14（周六）14:00—16:00
- 地点：拾光咖啡 · 梧桐街店（演示地址：梧桐街 12 号）
- 价格：¥99/人，限 8 人
- 报名方式：留下姓名和联系方式（本地演示，不发送任何数据）
- 讲师：林小豆（虚构人物），三年手冲教学经验
- 常见问题：
  1. 需要带什么？——什么都不用带，好奇心即可。
  2. 零基础可以吗？——可以，课程从磨豆开始。
  3. 可以退款吗？——活动前 48 小时可全额退款。
- 氛围要求：温暖、简洁，像朋友聚会，不像促销广告。
"""

RESERVED_KEYWORDS = ("日期", "时间改", "价格", "费用", "报名费", "改价", "涨价",
                     "地点", "上线", "发布", "部署", "对外", "账号", "密钥", "隐私",
                     "退款政策改")
MATERIAL_KEYWORDS = ("素材", "文案", "活动信息", "有没有", "是否已有", "哪里")


def resolve_codex_executable() -> str:
    """优先显式配置；其次 npm 全局包内的真实 exe；最后交给 PATH。"""
    override = os.environ.get("MENTIS_CODEX_BIN")
    if override and Path(override).is_file():
        return override
    npm_pkg = Path.home() / "AppData/Roaming/npm/node_modules/@openai/codex"
    for hit in npm_pkg.glob("**/bin/codex.exe"):
        return str(hit)
    import shutil

    return shutil.which("codex") or "codex"


def build_delegation_prompt(brief_text: str, direction: str, revision_note: str = "") -> str:
    parts = [
        "在当前工作区创建 index.html：一个本地单页活动介绍页。",
        f"[目标] {brief_text}",
        "[必读素材] input/copy.md（活动信息与文案的唯一来源，不得虚构活动事实）",
        f"[风格方向] {direction}",
        "[硬要求] "
        "1) 单文件 index.html，可内嵌 CSS/JS，中文内容；"
        "2) 必须包含活动名称、日期时间（2026-11-14 周六 14:00—16:00）、地点、价格 ¥99、"
        "报名表单（姓名与联系方式必填校验，提交后显示本地确认提示，不发送任何网络请求）；"
        "3) 桌面 1280 与移动 390 宽度都可用，无横向溢出；"
        "4) 图片只能用内联 SVG 或 CSS 图形，禁止外链与联网；"
        "5) 不要读取本工作区以外的文件，不要访问网络。",
        "[反问规则] 如果缺少必要信息，创建 QUESTIONS.md（每行一个问题，前缀 Q:），"
        "不要猜测；没有问题就不要创建该文件。",
        "[交付] index.html 必须存在并可直接用浏览器打开。",
    ]
    if revision_note:
        parts.append(f"[本轮修订] {revision_note}")
    return "\n".join(parts)


@dataclass
class CodexRunSummary:
    action_key: str
    attempt_id: str
    task_id: str
    thread_id: str
    status: str
    duration_s: float
    usage: dict
    artifacts: list
    final_text: str = ""
    error: str = ""


def load_state(state_path: str | Path) -> tuple[dict, TaskSession]:
    state_path = Path(state_path)
    meta = json.loads(state_path.read_text(encoding="utf-8"))
    session = session_from_dict(json.loads((state_path.parent / "session.json").read_text(encoding="utf-8")))
    return meta, session


def collaboration_from_state(state_path: str | Path, interactive: bool = False,
                             adapter=None, gateway=None, browser_capture=None) -> "Collaboration":
    """从落盘状态恢复运行对象（不重放历史委托）。"""
    meta, session = load_state(state_path)
    obj = Collaboration.__new__(Collaboration)
    obj.run_id = meta["run_id"]
    obj.mode = meta["mode"]
    obj.interactive = interactive
    obj.max_repairs = int(meta.get("max_repairs", 1))
    obj.timeout_s = int(meta.get("timeout_s", 420))
    obj.use_model = True
    obj._direction = ""
    obj._last_answer = ""
    obj.session = session
    obj.base_dir = Path(meta["base_dir"]) if meta.get("base_dir") else COLLAB_DIR
    obj.workspace = Path(meta["workspace"])
    # 观察目录跟随恢复运行自身的 base_dir（原硬编码 COLLAB_DIR 会把自定义位置
    # 运行的截图泄漏回 runtime/collab——T4 轮修复）
    obj.obs_dir = obj.base_dir / obj.run_id / "observation"
    obj.adapter = adapter or CodexCliAdapter(executable=resolve_codex_executable())
    obj.gateway = gateway if gateway is not None else ModelGateway(config=ModelConfig())
    obj.browser_capture = browser_capture
    obj.codex_runs = [CodexRunSummary(**r) for r in meta["codex_runs"]]
    obj.question_routes = list(meta.get("question_routes", []))
    obj.thread_id = meta.get("thread_id", "")
    obj.repairs_used = int(meta.get("repairs_used", 0))
    obj.applied_answers = bool(meta.get("applied_answers", False))
    obj.status = meta.get("status", "created")
    obj.last_diagnosis = None
    obj.last_professional_review = {}
    obj._review_form_requirements = []
    obj.measure = measure_page
    obj.run_dir = state_path.parent if isinstance(state_path, Path) else Path(state_path).parent
    obj._finalized_emitted = False
    obj.trace = TraceWriter(obj.run_dir, obj.run_id, executor_version=obj.adapter.cli_version)
    obj.trace.emit("mentis", "run.resumed",
                   f"从落盘状态恢复（进入时状态 {obj.status}）；既有事件续排，不重放历史委托",
                   brief_version=obj.session.current_brief.version)
    return obj


class Collaboration:
    def __init__(self, goal: str, mode: str = "autonomous", direction: str = "",
                 interactive: bool = False, adapter=None, gateway=None,
                 browser_capture=None, max_repairs: int = 1, timeout_s: int = 420,
                 use_model: bool = True, run_id: str = "", base_dir=None, measure=None):
        self.base_dir = Path(base_dir) if base_dir else COLLAB_DIR
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + os.urandom(2).hex()
        self.mode = mode
        self.interactive = interactive
        self.max_repairs = max_repairs
        self.timeout_s = timeout_s
        self.use_model = use_model
        self.measure = measure or measure_page  # 渲染测量（T4 补修：可注入替身）
        self._direction = direction
        self._last_answer = ""
        self.session = TaskSession(goal=goal)
        self._setup_session()
        self.workspace = self.base_dir / self.run_id / "workspace"
        self.obs_dir = self.base_dir / self.run_id / "observation"
        self.adapter = adapter or CodexCliAdapter(executable=resolve_codex_executable())
        self.gateway = gateway if gateway is not None else ModelGateway(config=ModelConfig())
        self.browser_capture = browser_capture
        self.codex_runs: list[CodexRunSummary] = []
        self.question_routes: list[dict] = []
        self.thread_id = ""
        self.repairs_used = 0
        self.applied_answers = False
        self.status = "created"
        self.last_diagnosis = None
        self.last_professional_review: dict = {}
        self._review_form_requirements: list = []
        self.run_dir = self.base_dir / self.run_id
        self._finalized_emitted = False
        # P2.4-T1：追踪写入器（契约 mentis-trace/1）；委托/复检/收尾点写入
        self.trace = TraceWriter(self.run_dir, self.run_id,
                                 executor_version=self.adapter.cli_version)
        self.trace.emit("mentis", "goal.recorded", self.session.goal,
                        brief_version=self.session.current_brief.version)
        self.trace.emit("mentis", "policy.set",
                        "代决范围：" + "；".join(self.session.current_policy.decided_scope)
                        + "；保留决定：" + "；".join(self.session.current_policy.reserved_decisions),
                        brief_version=self.session.current_brief.version)
        self.trace.emit("mentis", "authorization.granted",
                        "exec-workspace（来源 user-cli-invocation）",
                        brief_version=self.session.current_brief.version)

    # ---- 初始化 ----

    def _setup_session(self):
        s = self.session
        s.init_brief(s.goal)
        brief = s.current_brief
        # T4 来源修正：此约束为程序预置（用户命令只给了目标），不得标 user_selected
        brief.constraints.append(SourcedItem(
            "本地单页、无网络、不发送真实数据；改动限于隔离工作区",
            Provenance.SYSTEM, "system-preset:isolation-no-network"))
        if self.mode == "design" and self._direction:
            brief.preferences.append(SourcedItem(
                f"风格方向（用户选定）：{self._direction}",
                Provenance.USER_SELECTED, "user-choice:direction"))
        else:
            brief.preferences.append(SourcedItem(
                "温暖、简洁，像朋友聚会；避免促销感（暂定默认，可被用户反馈覆盖）",
                Provenance.PROVISIONAL, "default:adr002"))
        s.set_policy(DelegationPolicy(
            policy_id="policy-1", version=1,
            decided_scope=["视觉与布局实现细节", "文案措辞", "技术实现选择（单文件/内联样式）"],
            reserved_decisions=["活动日期/地点/价格变更", "对外发布或部署", "超出工作区的授权"],
            execution_scopes=["exec-workspace"],
            budget={"max_auto_revisions": self.max_repairs},
            intervention_triggers=["执行器提出保留决定相关反问", "硬要求无法自主满足"],
            provenance=Provenance.AGENT_SELECTED,
            source_ref="derived:user-command + ADR-002（默认自主委托）",
        ))
        s.authorize("exec-workspace", granted_by="user-cli-invocation")

    # ---- 基础动作 ----

    def notify(self, message: str):
        self.session.notify_progress(message)
        print(f"[通知] {message}")

    def ask_user(self, question: str, blocking_action: str, blocking_reason: str,
                 unique: str, excluded: str) -> bool:
        """请求用户决定；交互模式当场等待回答，否则保持未决（沉默≠批准）。"""
        ix = self.session.request_decision(question, blocking_action=blocking_action)
        self.trace.emit("mentis", "decision.requested",
                        f"请求决定 {ix.interaction_id}（阻塞 {blocking_action}）：{question}｜"
                        f"阻塞原因：{blocking_reason}",
                        brief_version=self.session.current_brief.version, status="pending")
        if self.interactive:
            print(f"\n[需要你决定] {question}")
            print(f"  为什么现在问：{blocking_reason}")
            print(f"  为什么只有你能答：{unique}")
            print(f"  已排除的自主路径：{excluded}")
            answer = input("  你的回答（直接回车=保持未决）：").strip()
            if answer:
                self.session.answer_interaction(ix.interaction_id, answer, source="user")
                self._last_answer = answer
                self.trace.emit("user", "decision.answered",
                                f"回答 {ix.interaction_id}：「{answer[:120]}」",
                                brief_version=self.session.current_brief.version)
                return True
        self.notify(f"请求已记录（{ix.interaction_id}），依赖动作 {blocking_action} 暂停；"
                    "其余独立工作继续，不越权。")
        return False

    def prepare_workspace(self):
        ws = self.workspace
        if ws.exists():
            shutil.rmtree(ws)
        (ws / "input").mkdir(parents=True)
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=ws, check=True, capture_output=True)
        (ws / "brief.md").write_text(
            f"# 目标\n{self.session.current_brief.objective}\n\n# 约束与偏好\n"
            + "\n".join(f"- {i.text}" for i in
                        self.session.current_brief.constraints + self.session.current_brief.preferences)
            + "\n", encoding="utf-8")
        (ws / "input" / "copy.md").write_text(COPY_DECK, encoding="utf-8")
        expected = {".git", "brief.md", "input"}
        actual = {p.name for p in ws.iterdir()}
        if actual != expected:
            raise RuntimeError(f"工作区初始化出现意外文件: {actual ^ expected}")
        self.notify(f"隔离工作区已建立：{ws}（仅 brief 与素材，无任何秘密文件）")

    def current_context(self):
        return select_for_task(
            load_registry(), task_label="页面诊断",
            task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为", "促销感来源"],
            observation_capabilities={"structure_only": True, "screenshots": True},
            can_execute=True,
        )

    # ---- 委托与反问分流 ----

    def _dispatch_codex(self, action_key: str, prompt: str, resume: bool = False,
                        revision_id: str | None = None):
        attempt = self.session.dispatch(action_key, "exec-workspace", revision_id=revision_id)
        brief_version = self.session.current_brief.version
        # 契约 §1：确切委托文本在实际调用前落盘；失败则不发起该次委托
        try:
            prompt_ref = self.trace.archive_prompt(attempt.attempt_id, prompt)
        except TraceArchiveError as exc:
            self.session.fail_attempt(attempt.attempt_id)
            self.trace.emit("mentis", "delegation.prepared",
                            f"prompt 落盘失败，本次委托不发起：{exc}", attempt_id=attempt.attempt_id,
                            brief_version=brief_version, status="failed")
            self.notify(f"{action_key} 未委托：{exc}")
            blocked = CodexTaskResult(
                task_id="", attempt_id=attempt.attempt_id, brief_version=brief_version,
                workspace=str(self.workspace), status=AttemptStatus.FAILED,
                error="追踪 prompt 落盘失败，未发起委托")
            return attempt, blocked
        self.trace.emit("mentis", "delegation.prepared",
                        f"已归档确切指令并委托 {action_key}（文本见 refs；与实际发送同一对象）",
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        refs=[prompt_ref])
        self.trace.record_delegation(attempt.attempt_id, brief_version, prompt_ref, action=action_key)

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
            workspace=self.workspace, prompt=prompt,
            brief_version=brief_version,
            sandbox="workspace-write", timeout_s=self.timeout_s,
            on_event=on_executor_event,
        )
        import time

        started = time.monotonic()
        if resume and self.thread_id:
            result = self.adapter.continue_task(self.thread_id, attempt.attempt_id, spec)
        else:
            result = self.adapter.start_task(spec)
        duration = round(time.monotonic() - started, 1)
        self.thread_id = result.native_thread_id or self.thread_id
        self.trace.emit("executor", "executor.completed",
                        f"{action_key} 终态 {result.status.value}；时长 {duration}s；"
                        f"用量 {result.usage or '未取得'}" + (f"；错误 {result.error[:120]}" if result.error else ""),
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        native_thread_id=result.native_thread_id or None,
                        status=STATUS_VALUE[result.status.value],
                        executor_status=result.status.value, usage=dict(result.usage))
        self.trace.record_executor_result(attempt.attempt_id, result.status,
                                          native_thread_id=result.native_thread_id,
                                          usage=dict(result.usage), error=result.error)
        self.codex_runs.append(CodexRunSummary(
            action_key=action_key, attempt_id=attempt.attempt_id, task_id=result.task_id,
            thread_id=result.native_thread_id, status=result.status.value,
            duration_s=duration, usage=dict(result.usage),
            artifacts=list(result.artifact_paths), final_text=result.final_response[:400],
            error=result.error,
        ))
        if result.status is AttemptStatus.COMPLETED:
            index = self.workspace / "index.html"
            self.session.complete_attempt(
                attempt.attempt_id,
                artifact_path=str(index) if index.is_file() else None,
                artifact_producer="codex-cli",
            )
        elif result.status is AttemptStatus.FAILED:
            self.session.fail_attempt(attempt.attempt_id)
        elif result.status is AttemptStatus.UNKNOWN:
            self.session.mark_unknown(attempt.attempt_id)
        # UNKNOWN：状态保持 unknown，锁由 Adapter 保留，不自动重发
        self.notify(f"{action_key} → 执行器状态 {result.status.value}"
                    + (f"（{result.error}）" if result.error else ""))
        return attempt, result

    def route_executor_questions(self, build_attempt) -> tuple[bool, object]:
        """执行器反问先由 Mentis 分流。返回 (是否有等待用户的请求, 用于收尾的尝试)。"""
        questions_path = self.workspace / "QUESTIONS.md"
        if not questions_path.is_file():
            self.notify("执行器无反问（QUESTIONS.md 不存在）")
            return False, build_attempt
        questions = [
            line[2:].strip() for line in
            questions_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip().startswith("Q:")
        ]
        if not questions:
            return False, build_attempt
        autonomous, user_questions, user_answered = [], [], []
        for q in questions:
            if any(k in q for k in RESERVED_KEYWORDS):
                route = route_gap(Gap(q, "user", "blocking", "irreversible", reserved_decision=True))
                user_questions.append((q, route))
            elif any(k in q for k in MATERIAL_KEYWORDS):
                route = route_gap(Gap(q, "workspace", "plan_changing", "reversible"))
                hits = [ln.strip() for ln in COPY_DECK.splitlines() if any(w in q for w in ("价格", "日期", "时间", "地点", "报名", "讲师", "退款", "人数"))]
                answer = "；".join(hits[:2]) if hits else "素材中没有该信息；请按素材既有内容处理，不要新增事实"
                autonomous.append((q, route, answer))
            else:
                route = route_gap(Gap(q, "model_knowledge", "plan_changing", "reversible"))
                autonomous.append((q, route, self._answer_by_model(q)))
            self.question_routes.append({
                "question": q, "route": route.action, "rationale": route.rationale,
                "answered_autonomously": route.action != "request_user",
            })
            self.trace.emit("mentis", "decision.routed",
                            f"执行器反问「{q[:80]}」→ {route.action}；理由：{route.rationale[:120]}",
                            attempt_id=build_attempt.attempt_id if build_attempt else None,
                            brief_version=self.session.current_brief.version,
                            status="pending" if route.action == "request_user" else "ok")
        if user_questions:
            for q, route in user_questions:
                if self.ask_user(
                    q, blocking_action="apply-answers",
                    blocking_reason="涉及用户保留决定，Mentis 不得代答",
                    unique="该信息或许可只有用户持有",
                    excluded="素材与常规实现路径已穷尽，均无法回答该问题",
                ):
                    user_answered.append((q, self._last_answer))
        if autonomous or user_answered:
            lines = ["# 对执行器反问的回答"]
            lines += [f"- Q: {q}\n  A: {a}（Mentis 自主回答，来源 {route.action}）"
                      for q, route, a in autonomous]
            lines += [f"- Q: {q}\n  A: {a}（用户回答）" for q, a in user_answered]
            (self.workspace / "ANSWERS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.notify(f"反问处理：自主回答 {len(autonomous)} 条，用户回答 {len(user_answered)} 条")
        pending = any(
            ix.blocking_action == "apply-answers" and ix.answer_source is None
            for ix in self.session.interactions if ix.kind.value == "request_decision"
        )
        if pending:
            # 有等待用户的保留决定：不派发 apply-answers（会被门禁拒绝），保持未决
            self.notify("存在等待用户的保留决定；ANSWERS.md 已就绪但继续动作保持暂停")
            return True, build_attempt
        if autonomous or user_answered:
            attempt, _ = self._dispatch_codex(
                "apply-answers",
                "阅读工作区中的 ANSWERS.md（对你的反问的回答），据此继续完成 index.html；"
                "完成后确保 index.html 存在。不要再创建 QUESTIONS.md。",
                resume=bool(self.thread_id),
            )
            self.applied_answers = True
            build_attempt = attempt
        return False, build_attempt

    def _answer_by_model(self, question: str) -> str:
        if self.use_model and self.gateway.has_credentials():
            record = self.gateway.complete(
                "你是网页制作助手。用一两句话直接回答执行者的问题；"
                "素材中没有的事实不要编造。",
                f"[活动素材]\n{COPY_DECK}\n\n[执行者的问题] {question}",
                json_mode=False,
            )
            if record.ok and record.response_text.strip():
                return record.response_text.strip()[:400]
            self.notify(f"模型回答失败（{record.error[:80]}），改用素材规则兜底")
        return "按素材既有内容与常规网页实践处理；不要新增未在素材中出现的事实。"

    # ---- 观察、检查、修复 ----

    def observe_artifact(self):
        index = self.workspace / "index.html"
        if not index.is_file():
            return None, None
        structure = observe_structure(str(index))
        browser_obs = None
        capture = self.browser_capture
        if capture is None:
            from mentis.browser_observation import capture_local_html

            capture = capture_local_html
        try:
            browser_obs = capture(index, self.obs_dir)
        except Exception as exc:
            self.notify(f"截图失败：{type(exc).__name__}（渲染观察不可用，单独记账）")
        return structure, browser_obs

    def _expected_values(self) -> dict:
        """从 brief 全版本取预期值（后版本覆盖先版本），避免用户改价后检查仍盯旧值。"""
        expected = {"活动名称": "手冲", "日期时间": "2026-11-14", "地点": "梧桐街",
                    "价格": "¥99", "报名方式": "报名"}
        import re

        for b in self.session.briefs:
            for item in b.constraints + b.preferences:
                hits = re.findall(r"¥\s*(\d+)", item.text)
                if hits:
                    expected["价格"] = f"¥{hits[-1]}"  # “从 ¥99 改为 ¥129”取改后值
        return expected

    def hard_requirement_review(self, structure):
        findings, blocking = [], []
        if structure is None:
            return ["index.html 不存在"], ["产物缺失"]
        text_blob = "\n".join(i.text for i in structure.items)
        expected = self._expected_values()
        for label, needle in expected.items():
            if needle in text_blob:
                findings.append(f"{label}：可见")
            else:
                findings.append(f"{label}：未在结构/正文文本中发现")
                blocking.append(label)
        inputs = [i for i in structure.items if i.kind in ("input", "textarea")]
        if inputs:
            findings.append(f"表单字段 {len(inputs)} 个")
        else:
            findings.append("未发现报名表单字段")
            blocking.append("报名表单")
        bad_alt = [i.item_id for i in structure.items
                   if i.kind == "img" and i.extra.get("alt_present") == "no"]
        if bad_alt:
            findings.append(f"图片缺少 alt：{bad_alt}")
        findings.append("结构层硬要求检查通过（视觉/交互未验证，见截图与 limitations）" if not blocking
                        else "存在阻塞项：未通过")
        return findings, blocking

    def _finalize(self, attempt) -> bool:
        """观察 → 硬要求检查 → 有界修复 → **交付前专业评审与指导修订** → 接受判定。

        返回是否交付；最终状态（含 UNKNOWN/UNVERIFIED/FAILED）由本方法设定，
        调用方不得再用 delivered 覆盖（T4 补修）。
        """
        structure, browser_obs = self.observe_artifact()
        findings, blocking = self.hard_requirement_review(structure)
        self.session.record_review(
            attempt.attempt_id, findings=findings, passed=not blocking,
            disposition=Disposition.REVISE if blocking else Disposition.ACCEPT)
        self.trace.emit("mentis", "review.recorded",
                        "结构硬要求检查：" + ("；".join(findings)[:300] if blocking else "通过")
                        + f"（视觉/交互未验证；结论 {'不通过' if blocking else '通过'}）",
                        attempt_id=attempt.attempt_id,
                        brief_version=self.session.current_brief.version,
                        status="failed" if blocking else "ok")
        self.notify("硬要求检查：" + ("未通过（" + "、".join(blocking) + "）" if blocking else "结构层通过"))
        while blocking and self.repairs_used < self.max_repairs:
            self.repairs_used += 1
            self.notify(f"自主修复第 {self.repairs_used} 轮（预算 {self.max_repairs}）")
            fix_prompt = build_delegation_prompt(
                self._brief_text(), self._direction_text(),
                revision_note="上轮结构检查未通过：" + "；".join(blocking)
                + "。修正后确保 index.html 完整。")
            repair_attempt, result = self._dispatch_codex(
                f"repair-{self.repairs_used}", fix_prompt, resume=True)
            if result.status is not AttemptStatus.COMPLETED:
                self.notify("修复未完成，停止自动循环")
                break
            structure, browser_obs = self.observe_artifact()
            findings, blocking = self.hard_requirement_review(structure)
            self.session.record_review(
                repair_attempt.attempt_id, findings=findings, passed=not blocking,
                disposition=Disposition.REVISE if blocking else Disposition.ACCEPT)
            self.trace.emit("mentis", "review.recorded",
                            "修复轮结构检查：" + ("；".join(findings)[:300] if blocking else "通过"),
                            attempt_id=repair_attempt.attempt_id,
                            brief_version=self.session.current_brief.version,
                            status="failed" if blocking else "ok")
            attempt = repair_attempt
        if blocking:
            self.notify("硬要求仍未满足：不交付、不接受（completed ≠ accepted）")
            self.status = "NOT_DELIVERED"
            self._persist(structure, browser_obs, delivered=False)
            return False
        review = self._professional_review(attempt)
        if review.get("block_delivery"):
            self.status = review["status"]
            self.notify(f"运行收尾：{self.status}；{review.get('delivery_note', '')}")
            self._persist(review.get("structure") or structure,
                          review.get("browser_obs") or browser_obs, delivered=False)
            return False
        final_attempt = review.get("accept_attempt") or attempt
        delivered = False
        try:
            self.session.accept(final_attempt.attempt_id)
            delivered = True
            self.notify("结构检查与专业评审完成 → 初稿已交付（可查看）；" + review.get("delivery_note", ""))
        except Exception as exc:
            self.notify(f"接受被拒绝：{exc}")
        self.status = "DELIVERED" if delivered else "NOT_DELIVERED"
        self._persist(review.get("structure") or structure,
                      review.get("browser_obs") or browser_obs, delivered=delivered)
        return delivered

    # ---- 交付前专业评审（T4 补修：Codex 首稿是供 Mentis 审查的工作稿） ----

    def _observe_full(self, tag: str, attempt_id: str | None = None):
        """专业评审观察：结构 + 渲染测量 + 双视口截图（复用 guidance 的观察语义）。

        返回 (ObservationPack, images[(名称, PNG 字节)], browser_obs)；
        截图失败不中断——诊断将不声称看过画面（视觉维度未验证）。
        """
        index = self.workspace / "index.html"
        out = self.obs_dir / f"review-{tag}"
        structure = observe_structure(str(index))
        measurement = self.measure(index, out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "measurement.json").write_text(
            json.dumps(_measurement_to_dict(measurement), ensure_ascii=False, indent=2),
            encoding="utf-8")
        # 观察引用绑定不可变副本（工作区 index.html 会随后续修订合法演化，
        # 直接引用会令回放审计误报哈希不匹配——T4 真实运行发现）
        snapshot = out / "page-snapshot.html"
        snapshot.write_bytes(index.read_bytes())
        images, image_names, browser_obs = [], [], None
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
            self.notify(f"截图采集失败（{type(exc).__name__}）：诊断不声称看过画面（视觉未验证）")
        brief_version = self.session.current_brief.version
        self.trace.emit("observer", "observation.structure",
                        f"专业评审观察 {len(structure.items)} 条目（sha256 前 12 位 {structure.sha256[:12]}）",
                        attempt_id=attempt_id, brief_version=brief_version,
                        refs=[{"path": str(snapshot), "sha256": structure.sha256, "role": "observation"}])
        self.trace.emit("observer", "observation.render",
                        "渲染测量：" + "; ".join(
                            f"{v.name} 溢出 {v.overflow_px}px/异常 {len(v.errors)}"
                            for v in measurement.viewports),
                        attempt_id=attempt_id, brief_version=brief_version,
                        refs=[{"path": str(out / "measurement.json"),
                               "sha256": _sha256_file(out / "measurement.json"),
                               "role": "observation"}])
        pack = ObservationPack(structure=structure, measurement=measurement,
                               image_names=image_names)
        return pack, images, browser_obs

    def _preserved_items(self) -> list:
        """修订必须保留的活动事实（预期值来自素材推导，随 brief 版本更新）。"""
        return [(label, needle) for label, needle in self._expected_values().items()]

    def _adjudicate_candidates(self, bundle: DiagnosisBundle, pack: ObservationPack,
                               attempt_id: str | None) -> list:
        """逐一审查诊断候选："字段合法、引用存在"不等于问题成立。

        确定性核对 + 规则分流：保持现状类建议不进入修订任务；溢出主张对照可信
        渲染测量数值；触及保留决定（变更意图+保留名词）转用户；其余可逆专业取舍
        自主采纳。候选与审查依据同源模型，独立性未建立——复检以独立观察为准。
        """
        keep_hints = ("保持现状", "保持当前", "保留当前", "无需修改", "维持现状", "保留现状",
                      "保持不变", "不建议修改", "无需改动", "无需调整")
        records = []
        measurement_errors = [e for v in pack.measurement.viewports for e in v.errors]
        trusted = [v for v in pack.measurement.viewports if not _measurement_anomalies(v)]
        total_overflow = sum(v.overflow_px for v in trusted)
        total_offenders = sum(len(v.overflowing) for v in trusted)
        brief_version = self.session.current_brief.version
        for idx, d in enumerate(bundle.valid_diagnoses, 1):
            no = f"d{idx}"
            joined = d.phenomenon + "｜" + d.interpretation + "｜" + "；".join(d.modification_candidates)
            if any(h in joined for h in keep_hints):
                decision, status = "keep_as_is", "ok"
                reason = "候选建议为保持现状：不进入修订任务（不为增加交流轮数强行修改）"
            elif "溢出" in joined and trusted and total_overflow == 0 and total_offenders == 0:
                decision, status = "reject", "ok"
                reason = ("候选主张横向溢出，但可信渲染测量显示双视口溢出 0px、无溢出元素"
                          "——证据不支持，驳回")
            elif "溢出" in joined and (measurement_errors or not trusted):
                decision, status = "pending", "unknown"
                reason = "候选主张溢出但渲染测量不可信/缺失：留待观察修复后核对，不据此修订"
            elif (any(v in d.phenomenon for v in _CHANGE_VERBS)
                  and any(n in d.phenomenon for n in _RESERVED_NOUNS)):
                decision, status = "needs_user", "pending"
                reason = "触及用户保留决定（变更意图+保留事项）：修订暂停，等待用户决定（沉默≠批准）"
            else:
                decision, status = "adopt", "ok"
                reason = ("证据引用可解析且与观察记录不矛盾；可逆的专业取舍由 Mentis 自主处理"
                          "（ADR-002）；候选与审查同源模型、独立性未建立——复检以独立观察为准")
            records.append({"no": no, "severity": d.severity, "phenomenon": d.phenomenon,
                            "evidence_refs": list(d.evidence_refs),
                            "decision": decision, "reason": reason})
            self.trace.emit("mentis", "decision.routed",
                            f"候选审查 {no}「{d.phenomenon[:60]}」→ {decision}；理由：{reason[:130]}",
                            attempt_id=attempt_id, brief_version=brief_version, status=status)
        return records

    def _professional_review(self, attempt) -> dict:
        result = self._professional_review_impl(attempt)
        self.last_professional_review["note"] = result.get("delivery_note", "")
        return result

    def _professional_review_impl(self, attempt) -> dict:
        """交付前专业评审与指导修订。结构硬要求通过后调用；不改现有权限规则。

        返回 {"accept_attempt", "block_delivery", "status", "delivery_note",
              "structure", "browser_obs"}——_finalize 据此决定接受与最终状态。
        依据：当前目标与约束、领域规则（草案）、当前版本截图与可信测量；
        所有结论关联当前产物版本；缺观察的维度如实标未验证。
        """
        result: dict = {"accept_attempt": None, "block_delivery": False, "status": None,
                        "delivery_note": "", "structure": None, "browser_obs": None}
        brief_version = self.session.current_brief.version
        if not (self.use_model and self.gateway.has_credentials()):
            self.last_professional_review = {"performed": False, "reason": "模型不可用"}
            self.notify("专业评审未执行（模型不可用）：初稿仅通过结构硬要求检查，"
                        "不宣称领域标准评审通过")
            self.trace.emit("mentis", "review.recorded",
                            "专业评审未执行（模型不可用）；仅结构硬要求检查"
                            "——不宣称领域标准评审通过",
                            attempt_id=attempt.attempt_id, brief_version=brief_version,
                            status="unknown")
            result["delivery_note"] = "专业评审未执行（模型不可用）；最终完成与满意度待你判断"
            return result
        origin = ""
        try:
            pack, images, browser_obs = self._observe_full("professional", attempt.attempt_id)
        except Exception as exc:
            self.last_professional_review = {"performed": False, "reason": f"观察失败：{type(exc).__name__}"}
            self.notify(f"专业评审观察失败（{type(exc).__name__}）：不冒充评审通过；"
                        "初稿按结构检查结果处理")
            self.trace.emit("mentis", "review.recorded",
                            f"专业评审观察失败：{type(exc).__name__}：{str(exc)[:120]}；评审未执行",
                            attempt_id=attempt.attempt_id, brief_version=brief_version,
                            status="unknown")
            result["delivery_note"] = "专业评审观察失败（测量/截图不可用）；评审未执行"
            return result
        self._review_form_requirements = sorted({
            i.text for i in pack.structure.items
            if i.kind in ("input", "textarea") and i.extra.get("required") == "yes"})
        # 诊断（领域规则草案 + 截图 + 渲染测量；无截图时模型被明确禁止声称看过画面）
        context, _ = select_for_task(
            load_registry(), task_label="页面诊断与修改指导",
            task_keywords=["单页活动介绍页"],
            decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
            observation_capabilities={"structure_only": True, "screenshots": bool(images),
                                      "render_measurement": True},
            can_execute=True)
        bundle = run_diagnosis(self.gateway, self.session.current_brief.objective, "",
                               pack, context, images=images, system=_diagnosis_system_prompt())
        self.last_diagnosis = bundle
        diagnosis_path = self.run_dir / "diagnosis-professional.md"
        image_note = ""
        if getattr(self.gateway, "calls", None):
            refs = getattr(self.gateway.calls[-1], "image_refs", [])
            if refs:
                image_note = f"\n- 图像输入：{';'.join(refs)}（随诊断请求发送的截图）"
        diagnosis_path.write_text(render_bundle_markdown(bundle) + image_note, encoding="utf-8")
        origin = "Mock 固定响应" if bundle.mock else f"真实模型 {bundle.model}"
        self.trace.emit("mentis", "observation.diagnosis",
                        f"专业评审诊断（{'Mock' if bundle.mock else '真实模型'}）："
                        f"有效 {len(bundle.valid_diagnoses)}、拒收 {len(bundle.invalid_diagnoses)}、"
                        f"决策请求 {len(bundle.valid_decision_requests)}",
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        status="ok" if bundle.valid_diagnoses else "unknown",
                        refs=[{"path": str(diagnosis_path),
                               "sha256": _sha256_file(diagnosis_path), "role": "diagnosis"}])
        self.notify(f"专业评审诊断（{origin}）：有效 {len(bundle.valid_diagnoses)}、"
                    f"拒收 {len(bundle.invalid_diagnoses)}、决策请求 "
                    f"{len(bundle.valid_decision_requests)}"
                    + ("" if images else "；**无截图输入，视觉维度未验证**"))
        # 决策请求分流：常规可逆取舍自主处理；用户独有事实/重要取舍/许可才请求决定
        agent_decisions: list = []
        pending_user = False
        for request in bundle.valid_decision_requests:
            q = request.question
            change_intent = any(v in q for v in _CHANGE_VERBS)
            touches_reserved = any(n in q for n in _RESERVED_NOUNS)
            asks_new_fact = any(h in q for h in _NEW_FACT_HINTS) and not touches_reserved
            if (change_intent and touches_reserved) or asks_new_fact:
                answered = self.ask_user(
                    q, blocking_action="review-fix",
                    blocking_reason=request.blocking_reason,
                    unique=request.user_unique_info_or_auth_basis,
                    excluded=request.excluded_autonomous_paths)
                if answered:
                    agent_decisions.append(f"问：{q}\n  用户回答：{self._last_answer}")
                else:
                    pending_user = True
            else:
                agent_decisions.append(f"问：{q}\n  Mentis 自主决定：{AUTONOMOUS_DECISION_TEXT}")
                self.trace.emit("mentis", "decision.routed",
                                f"agent_decide：{q}；可逆专业取舍；决定 {AUTONOMOUS_DECISION_TEXT[:80]}",
                                attempt_id=attempt.attempt_id, brief_version=brief_version)
        # 候选审查（采纳/驳回/保持现状/待用户，理由逐条留档）
        adjudication = self._adjudicate_candidates(bundle, pack, attempt.attempt_id)
        adopted = [c for c in adjudication if c["decision"] == "adopt"]
        rejected = [c for c in adjudication if c["decision"] == "reject"]
        keep = [c for c in adjudication if c["decision"] == "keep_as_is"]
        needs_decision = [c for c in adjudication if c["decision"] == "needs_user"]
        pending_check = [c for c in adjudication if c["decision"] == "pending"]
        review_doc = {
            "reviewed_artifact_sha256": pack.sha256,
            "observation": {"images": pack.image_names,
                            "measurement_errors": [e for v in pack.measurement.viewports
                                                   for e in v.errors]},
            "source_note": (f"候选来自模型诊断（{origin}）；审查=Mentis 确定性核对与规则分流；"
                            "与诊断同源模型，独立性未建立——修订后复检以独立观察为准"),
            "candidates": adjudication,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        adjudication_path = self.run_dir / "professional-review.json"
        adjudication_path.write_text(
            json.dumps(review_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        self.trace.emit("mentis", "review.recorded",
                        f"专业评审候选审查：候选 {len(adjudication)} 条 → 采纳 {len(adopted)}、"
                        f"驳回 {len(rejected)}、保持现状 {len(keep)}、"
                        f"待用户/待查 {len(needs_decision) + len(pending_check)}"
                        f"（审查针对产物 {pack.sha256[:12]}；依据见 refs）",
                        attempt_id=attempt.attempt_id, brief_version=brief_version,
                        status="ok" if adjudication or not bundle.valid_diagnoses else "unknown",
                        refs=[{"path": str(adjudication_path),
                               "sha256": _sha256_file(adjudication_path), "role": "review"}])
        self.last_professional_review = {
            "performed": True, "origin": origin, "candidates": len(adjudication),
            "adopted": len(adopted), "rejected": len(rejected), "keep": len(keep),
            "needs_user": len(needs_decision) + len(pending_check), "verdict": None,
            "unverified_visual": not images,
        }
        if pending_user or needs_decision:
            self.notify("专业评审：存在等待你的决定——修订暂停（沉默≠批准）；"
                        "初稿按当前版本处理，其余成立问题已留档")
            result["delivery_note"] = ("专业评审完成；存在待你决定的问题，"
                                       "修订暂停（沉默≠批准）")
            return result
        if pending_check:
            self.notify("专业评审：存在待查候选（渲染测量不可信/缺失）——修订暂缓，"
                        "先修复观察链再核对；不据不可信观察修订")
            result["delivery_note"] = "专业评审完成；待查候选暂缓修订（观察不可信）"
            return result
        if not adopted:
            # 合格不改：必须留下接受依据，不为增加交流轮数强行修改
            self.notify(f"专业评审：{len(adjudication)} 条候选审查完毕、采纳 0——合格不改，"
                        "接受依据已留档（professional-review.json）")
            result["delivery_note"] = (f"专业评审（{origin}）：候选 {len(adjudication)} 条、"
                                       "采纳 0——合格不改（接受依据见评审记录）")
            return result
        if self.repairs_used >= self.max_repairs:
            self.notify(f"专业评审发现 {len(adopted)} 条成立问题，但修订预算已用尽"
                        f"（{self.max_repairs}）：留档不执行，不冒充已解决")
            result["delivery_note"] = (f"成立问题 {len(adopted)} 条因修订预算耗尽未修订"
                                       "（留档）；不宣称已解决")
            return result
        # 成立问题 → 明确修订指令（问题位置/证据/目的/方向/保留项/复检办法）→ 预算内委托
        defect_ids = [c["no"] for c in adopted]
        adopted_items = [bundle.valid_diagnoses[int(c["no"][1:]) - 1] for c in adopted]
        filtered = DiagnosisBundle(goal=bundle.goal, observation_source=bundle.observation_source,
                                   model=bundle.model, valid_diagnoses=adopted_items)
        instruction = compose_revision_instruction(
            filtered, defect_ids, pack, self._preserved_items(),
            [i.text for i in self.session.current_brief.constraints],
            agent_decisions=agent_decisions)
        self.repairs_used += 1
        self.notify(f"专业评审：{len(adopted)} 条成立问题转入修订"
                    f"（修订预算 {self.repairs_used}/{self.max_repairs}）")
        self._snapshot_current_artifact()
        trace_ix = self.session.notify_progress(
            "Mentis 专业评审发起修订（notify 不阻塞）")
        revision = self.session.record_revision(
            feedback_interaction_id=trace_ix.interaction_id,
            from_attempt_id=attempt.attempt_id,
            instruction=f"[Mentis 专业评审修订指令] 确切文本见 trace/prompts/"
                        f"（哈希=实际发送；依据链见 professional-review.json）",
            round_no=self.repairs_used)
        rev_attempt, rev_result = self._dispatch_codex(
            f"review-fix-{self.repairs_used}", instruction, resume=True,
            revision_id=revision.revision_id)
        revision.to_attempt_id = rev_attempt.attempt_id
        if rev_result.status is AttemptStatus.UNKNOWN:
            self.notify("修订执行状态未知（超时/无终态）：不判通过、不自动重发；需核对后续接")
            result.update(block_delivery=True, status="UNKNOWN",
                          delivery_note="专业评审修订执行状态未知：需人工核对（不冒充完成）")
            return result
        if rev_result.status is AttemptStatus.FAILED:
            self.notify("修订执行失败：如实停止，不自动重试")
            result.update(block_delivery=True, status="FAILED",
                          delivery_note="专业评审修订执行失败")
            return result
        # 修订完成 → 重新观察并复检（目标问题/保留项/回归/新问题；不采信 Codex 自查）
        pack2, images2, browser_obs2 = self._observe_full(
            "professional-verify", rev_attempt.attempt_id)
        result["structure"], result["browser_obs"] = pack2.structure, browser_obs2
        findings2, regression, unknown = deterministic_checks(
            pack2, self._preserved_items(), self._review_form_requirements)
        if self.browser_capture is not None and not images2:
            unknown.append("复查截图缺失：视觉类解决声明无法核对（不作为通过依据）")
        model_review_path = self.run_dir / "model-review-professional.json"
        resolved, unresolved, new_issues = run_model_review(
            self.gateway, self.session.current_brief.objective,
            self.workspace / "index.html", filtered, defect_ids, pack2, images2,
            model_review_path, use_model=self.use_model, notify=self.notify)
        review_passed = not regression and not unresolved and not new_issues and not unknown
        verify_doc = {
            "reviewed_html_sha256": pack2.sha256,
            "deterministic_findings": findings2,
            "preserved_regression": regression,
            "measurement_unknown": unknown,
            "model_review": {"resolved": resolved, "unresolved": unresolved,
                             "new_issues": new_issues,
                             "detail": str(model_review_path)},
            "measurement_v2": {v.name: {"overflow_px": v.overflow_px,
                                        "overflowing": len(v.overflowing),
                                        "clipped": len(v.clipped_text),
                                        "inner_width": v.inner_width,
                                        "errors": list(v.errors)}
                               for v in pack2.measurement.viewports},
        }
        verify_path = self.run_dir / "professional-verify.json"
        verify_path.write_text(
            json.dumps(verify_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        self.session.record_review(
            rev_attempt.attempt_id, findings=findings2 + [
                f"模型复查：已解决 {resolved or '无'}；未解决 {unresolved or '无'}；"
                f"新问题 {new_issues or '无'}"] + [f"测量未知：{u}" for u in unknown],
            passed=review_passed,
            disposition=Disposition.ACCEPT if review_passed else Disposition.REVISE)
        if regression:
            verdict = "regression"
        elif unknown:
            verdict = "unverified"
        elif resolved and not unresolved and not new_issues:
            verdict = "improved"
        elif not unresolved and not new_issues:
            verdict = "not_improved"
        else:
            verdict = "partially_improved"
        self.last_professional_review["verdict"] = verdict
        self.trace.emit("mentis", "review.recorded",
                        f"修订复检：保留项回归 {regression or '无'}；测量未知 {unknown or '无'}；"
                        f"模型复查已解决 {resolved or '无'}/未解决 {unresolved or '无'}/"
                        f"新问题 {new_issues or '无'}；判定 {verdict}"
                        f"（复查绑定产物 {pack2.sha256[:12]}）",
                        attempt_id=rev_attempt.attempt_id, brief_version=brief_version,
                        status="ok" if review_passed else "failed",
                        refs=[{"path": str(verify_path),
                               "sha256": _sha256_file(verify_path), "role": "review"}])
        if verdict == "regression":
            self.notify("修订复检发现回归：" + "；".join(regression)[:150]
                        + "；不交付该修订版（修订前快照在 versions/）")
            result.update(block_delivery=True, status="NOT_DELIVERED",
                          delivery_note="专业评审修订造成回归：不交付（快照保留修订前版本）")
            return result
        if verdict == "unverified":
            self.notify("修订后复检存在未知（测量不可信/缺截图/缺记录）：不判通过也不判失败；"
                        "需先核对观察链")
            result.update(block_delivery=True, status="UNVERIFIED",
                          delivery_note="修订后复检存在未知：不判通过（见复检记录）")
            return result
        result["accept_attempt"] = rev_attempt
        if verdict == "improved":
            result["delivery_note"] = (f"专业评审：采纳 {len(adopted)} 条成立问题→修订→复检通过"
                                       f"（已解决 {resolved}）")
        else:
            leftover = "；".join(unresolved + new_issues)[:150] or "（无明确遗留）"
            result["delivery_note"] = (f"专业评审修订后复检判定 {verdict}；遗留问题：{leftover}"
                                       "（修订预算已用尽，如实交付，不为遗留问题冒充已解决）")
        return result

    # ---- 主流程 ----

    def _build_phase(self) -> str:
        attempt, result = self._dispatch_codex(
            "build-page",
            build_delegation_prompt(self._brief_text(), self._direction_text()))
        if result.status is AttemptStatus.UNKNOWN:
            self.status = "UNKNOWN"
            self.notify("执行器状态未知（超时）：锁已保留，需人工核对后处理；不自动重发")
            self._persist(None, None, delivered=False)
            return self.status
        if result.status is AttemptStatus.COMPLETED:
            needs_user, attempt = self.route_executor_questions(attempt)
            if needs_user:
                self.status = "PENDING"
                self._persist(None, None, delivered=False)
                return self.status
            # _finalize 内部已设定最终状态（含专业评审后的 UNKNOWN/UNVERIFIED/FAILED）
            self._finalize(attempt)
        else:
            self.status = "FAILED"
            self.notify("委托未成功完成，不进入收尾（可 resume 重试或核对工作区）")
            self._persist(None, None, delivered=False)
        return self.status

    def run(self) -> str:
        try:
            self.notify(f"开始协作：{self.session.goal}（模式 {self.mode}，"
                        f"{'交互' if self.interactive else '非交互'}）")
            self.prepare_workspace()
            if self.mode == "design":
                answered = self.ask_user(
                    "首版风格方向：A 暖棕纸质感大图 / B 白底几何简约 / 或直接描述你的想法",
                    blocking_action="build-page",
                    blocking_reason="风格方向影响整版实现，返工成本高",
                    unique="审美偏好只有你能表达",
                    excluded="常规布局与信息组织已可自主决定；已有默认暖色方向可作暂定",
                )
                if answered:
                    self.session.update_brief(add_preferences=[SourcedItem(
                        f"风格方向（用户选定）：{self._last_answer}",
                        Provenance.USER_SELECTED, "user-choice:direction")])
                else:
                    self.status = "PENDING"
                    self.notify("方向未定：不委托、不猜测；等待你在 resume 中回答")
                    self._persist(None, None, delivered=False)
                    return self.status
            return self._build_phase()
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
                    self.notify(f"异常收尾：尝试 {attempt.attempt_id} 无执行器终态，"
                                "改记 unknown（需核对；不判失败、不自动重发）")
            self.notify(f"运行异常终止：{type(exc).__name__}：{str(exc)[:160]}；"
                        "已按 ERROR 收尾落盘，可从 state.json 恢复")
            self.status = "ERROR"
            self._finalized_emitted = True
            self.trace.emit("mentis", "run.finalized",
                            f"运行异常终止：{type(exc).__name__}：{str(exc)[:200]}"
                            "（无终态尝试已记 unknown；已记录的执行器终态与事件不变）",
                            brief_version=self.session.current_brief.version,
                            status="failed")
            self._persist(None, None, delivered=False)
        except Exception:
            pass

    def _codex_processes_since(self, since_iso: str) -> list:
        """列出晚于 since 启动的 codex 进程（判断是否仍有执行器在写入）。"""
        import subprocess

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

    def confirm_outcome_after_review(self, final_status: AttemptStatus) -> str:
        """unknown 的人工核对路径：确认无活跃写入者 → 释放锁 → 显式确认结局 → 收尾。

        调用前提（由调用者保证并记录）：已核对产物与执行器状态。本方法会再次
        检查是否存在晚于本次运行启动的 codex 进程，存在则拒绝确认。
        """
        started_at = self.session.created_at
        active = self._codex_processes_since(started_at)
        if active:
            self.notify(f"仍有 {len(active)} 个 codex 进程晚于本次运行启动，拒绝确认；请先核对执行器")
            self.status = "UNKNOWN"
            return self.status
        # 无活跃写入者：释放本运行持有的写锁（Adapter 超时时故意保留）
        import hashlib

        digest = hashlib.sha256(str(self.workspace.resolve()).casefold().encode("utf-8")).hexdigest()[:20]
        lock = Path("runtime") / "executor_locks" / f"{digest}.lock"
        if lock.is_file():
            lock.unlink()
            self.notify(f"写锁已释放（核对通过）：{lock}")
        attempt = next((a for a in self.session.attempts if a.status.value == "unknown"), None)
        if attempt is None:
            running = [a for a in self.session.attempts if a.status.value == "running"]
            unknown_runs = [r for r in self.codex_runs if r.status == "unknown"]
            if running and unknown_runs:
                attempt = running[-1]
                self.session.mark_unknown(attempt.attempt_id)
                self.notify("修正：旧版本持久化把超时尝试记为 running；"
                            "按执行器实际结果改记 unknown（透明修正）")
        if attempt is None:
            # 无终态事件被记为 failed，但执行器有最终文本且产物可核对 → 按不确定处理
            failed = [a for a in self.session.attempts if a.status.value == "failed"]
            uncertain = [r for r in self.codex_runs
                         if r.status == "failed" and r.final_text
                         and "未报告成功终态" in (r.error or "")]
            if failed and uncertain:
                attempt = failed[-1]
                self.notify("执行器缺少终态事件但有最终文本；按不确定处理，交由产物核对决定")
        if attempt is None:
            pending_accept = [a for a in self.session.attempts
                              if a.status.value == "completed" and not a.accepted]
            if pending_accept:
                self.notify("无待确认尝试；对最近的已完成尝试按当前检查标准重新收尾")
                delivered = self._finalize(pending_accept[-1])
                self.status = "DELIVERED" if delivered else "NOT_DELIVERED"
                return self.status
            self.notify("没有需要核对的尝试")
            return self.status
        self.session.confirm_outcome(attempt.attempt_id, final_status)
        index = self.workspace / "index.html"
        if final_status is AttemptStatus.COMPLETED and index.is_file():
            artifact = self.session.add_artifact(attempt.attempt_id, str(index),
                                                 "codex-cli(核对后补记)")
            attempt.artifact_id = artifact.artifact_id
        # T4：核对确认本身入事件流（问题 5 的依据——否则 ERROR→确认→交付的过程只存在于终端输出）
        self.trace.emit("mentis", "executor.confirmed",
                        f"人工核对后确认 {attempt.attempt_id} 结局 {final_status.value}"
                        "（依据：产物存在且可解析、无活跃写入者；不声称执行器完成其全部计划步骤）",
                        attempt_id=attempt.attempt_id,
                        brief_version=self.session.current_brief.version,
                        status=STATUS_VALUE[final_status.value],
                        executor_status=final_status.value)
        self.notify(f"执行器结局已确认为 {final_status.value}（依据：产物存在且可解析、"
                    "无活跃写入者；不声称执行器完成了其全部计划步骤）")
        if final_status is AttemptStatus.COMPLETED:
            self._finalize(attempt)
        else:
            self.status = "FAILED"
            self._persist(None, None, delivered=False)
        return self.status

    def resume(self, answers: dict | None = None, feedback: str = "",
               goal_new: str = "", revoke: bool = False,
               confirm_outcome: AttemptStatus | None = None) -> str:
        """恢复一次运行（异常同样收尾落盘，见 _finalize_on_error）。"""
        try:
            return self._resume_impl(answers=answers, feedback=feedback, goal_new=goal_new,
                                     revoke=revoke, confirm_outcome=confirm_outcome)
        except Exception as exc:
            self._finalize_on_error(exc)
            raise

    def _resume_impl(self, answers: dict | None = None, feedback: str = "",
                     goal_new: str = "", revoke: bool = False,
                     confirm_outcome: AttemptStatus | None = None) -> str:
        """恢复一次运行：回答未决请求 / 反馈修订 / 改目标 / 撤回 / 核对确认。"""
        if confirm_outcome is not None:
            return self.confirm_outcome_after_review(confirm_outcome)
        if revoke:
            for auth in self.session.authorizations:
                if auth.status.value == "active":
                    self.session.revoke_authorization(auth.auth_id)
            self.trace.emit("user", "authorization.revoked", "用户撤回授权；后续委托全部拒绝",
                            brief_version=self.session.current_brief.version, status="failed")
            self.status = "WITHDRAWN"
            self.notify("授权已撤回：后续任何委托都会被拒绝；已产出文件保留待核对")
            self._persist(None, None, delivered=False)
            return self.status
        if goal_new:
            self.session.update_brief(objective=goal_new)
            self.trace.emit("user", "brief.updated",
                            f"目标改为：「{goal_new[:120]}」（旧版本结果不再适用）",
                            brief_version=self.session.current_brief.version)
            self.notify(f"目标已改为：{goal_new}（brief v{self.session.current_brief.version}）；"
                        "旧版本尝试不能被接受（版本门禁），将按新目标重新委托")
            self._snapshot_current_artifact()
            attempt, result = self._dispatch_codex(
                "rebuild-new-goal",
                build_delegation_prompt(self._brief_text(), self._direction_text()))
            if result.status is AttemptStatus.COMPLETED:
                needs_user, attempt = self.route_executor_questions(attempt)
                if needs_user:
                    self.status = "PENDING"
                    self._persist(None, None, delivered=False)
                    return self.status
                self._finalize(attempt)
            else:
                self.status = "FAILED"
                self._persist(None, None, delivered=False)
            return self.status
        if answers:
            for ix_id, text in answers.items():
                self.session.answer_interaction(ix_id, text, source="user")
            self.notify(f"已记录你的 {len(answers)} 条回答")
        pending_apply = any(
            ix.blocking_action == "apply-answers" and ix.answer_source is None
            for ix in self.session.interactions if ix.kind.value == "request_decision"
        )
        if pending_apply:
            self.status = "PENDING"
            self.notify("仍有未回答的请求；依赖动作保持暂停（沉默≠批准）")
            self._persist(None, None, delivered=False)
            return self.status
        # 1) 反问回答齐备且尚未应用 → 应用答案并收尾
        has_apply = any(
            ix.blocking_action == "apply-answers" and ix.answer_source
            for ix in self.session.interactions if ix.kind.value == "request_decision"
        )
        if has_apply and not self.applied_answers:
            attempt, result = self._dispatch_codex(
                "apply-answers",
                "阅读工作区中的 ANSWERS.md（对你的反问的回答），据此继续完成 index.html；"
                "完成后确保 index.html 存在。不要再创建 QUESTIONS.md。",
                resume=bool(self.thread_id),
            )
            self.applied_answers = True
            if result.status is AttemptStatus.COMPLETED:
                self._finalize(attempt)
            else:
                self.status = "FAILED"
                self._persist(None, None, delivered=False)
            return self.status
        # 1.5) 设计方向刚被回答 → 现在才委托构建
        direction_answered = any(
            ix.blocking_action == "build-page" and ix.answer_source
            for ix in self.session.interactions if ix.kind.value == "request_decision"
        )
        if direction_answered and not self.session.attempts:
            decision = next(ix for ix in self.session.interactions
                            if ix.kind.value == "request_decision"
                            and ix.blocking_action == "build-page" and ix.answer_source)
            already = any(i.provenance.value == "user_selected" and "风格方向" in i.text
                          for b in self.session.briefs for i in b.preferences)
            if not already:
                self.session.update_brief(add_preferences=[SourcedItem(
                    f"风格方向（用户选定）：{decision.answer_text}",
                    Provenance.USER_SELECTED, "user-choice:direction")])
            self.notify("方向已确认，开始委托构建")
            return self._build_phase()
        # 2) 用户反馈 → 新 brief 版本 + 续接修订
        if feedback:
            if not self.thread_id:
                self.status = "FAILED"
                self.notify("没有可续接的执行会话，无法按反馈修订")
                self._persist(None, None, delivered=False)
                return self.status
            self.session.update_brief(add_preferences=[SourcedItem(
                f"用户修正：{feedback}", Provenance.USER_SELECTED, "user-feedback")])
            self.trace.emit("user", "brief.updated",
                            f"用户修正进入 brief v{self.session.current_brief.version}：「{feedback[:120]}」",
                            brief_version=self.session.current_brief.version)
            trace = self.session.notify_progress(f"用户明确修正：{feedback}")
            revision = self.session.record_revision(
                feedback_interaction_id=trace.interaction_id,
                from_attempt_id=self.session.attempts[-1].attempt_id,
                instruction=feedback,
            )
            self.notify(f"反馈已进入 brief v{self.session.current_brief.version}（{revision.revision_id}）；"
                        "旧版本结果不再适用，按新版本修订")
            self._snapshot_current_artifact()
            attempt, result = self._dispatch_codex(
                f"revision-b{self.session.current_brief.version}",
                build_delegation_prompt(
                    self._brief_text(), self._direction_text(),
                    revision_note=f"用户明确修正：{feedback}。保留未被告知改变的部分。"),
                resume=True, revision_id=revision.revision_id,
            )
            if result.status is AttemptStatus.COMPLETED:
                self._finalize(attempt)
            else:
                self.status = "FAILED"
                self._persist(None, None, delivered=False)
            return self.status
        # 3) 无未决：收尾最近完成的尝试
        last_completed = [a for a in self.session.attempts if a.status.value == "completed"]
        if last_completed:
            self._finalize(last_completed[-1])
        else:
            self.status = "NO_WORK"
            self.notify("没有可收尾的已完成尝试")
            self._persist(None, None, delivered=False)
        return self.status

    # ---- 记录 ----

    def _snapshot_current_artifact(self) -> Path | None:
        """T4：修订/重建委托前快照当前 index.html（"看变化"需要前后可对照）。

        修订会覆盖工作区内同名产物；快照保留旧版文件本体，其内容哈希与
        session.artifacts 中旧尝试的记录一致。幂等：同名字快照不覆盖。
        """
        index = self.workspace / "index.html"
        if not index.is_file() or not self.session.attempts:
            return None
        versions = self.run_dir / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        snapshot = versions / f"index-{self.session.attempts[-1].attempt_id}.html"
        if not snapshot.exists():
            shutil.copyfile(index, snapshot)
            self.notify(f"旧版产物已快照：{snapshot.name}（修订委托将覆盖工作区版本）")
        return snapshot

    def _brief_text(self):
        b = self.session.current_brief
        return b.objective + "；" + "；".join(i.text for i in b.constraints + b.preferences)

    def _direction_text(self):
        prefs = [i.text for i in self.session.current_brief.preferences]
        return "；".join(prefs) if prefs else "温暖简洁"

    def _persist(self, structure, browser_obs, delivered: bool):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self._finalized_emitted:
            self.trace.emit("mentis", "run.finalized",
                            f"运行收尾：状态 {self.status}；交付 {'是' if delivered else '否'}",
                            brief_version=self.session.current_brief.version,
                            status="ok" if delivered else "unknown")
            self._finalized_emitted = True
        self.trace.finalize(outcome=f"{self.status}（delivered={delivered}）")
        save_session(self.session, str(self.run_dir / "session.json"))
        meta = {
            "state_version": STATE_VERSION,
            "run_id": self.run_id, "mode": self.mode, "status": self.status,
            "goal": self.session.goal, "workspace": str(self.workspace),
            "base_dir": str(self.base_dir),
            "thread_id": self.thread_id, "repairs_used": self.repairs_used,
            "timeout_s": self.timeout_s, "max_repairs": self.max_repairs,
            "applied_answers": self.applied_answers,
            "delivered": delivered,
            "codex_runs": [r.__dict__ for r in self.codex_runs],
            "question_routes": self.question_routes,
            "model_usage": self.gateway.usage_summary() if self.gateway else {},
            "screenshots": ([v.screenshot_path for v in browser_obs.views if v.screenshot_path]
                            if browser_obs else []),
            "observation_source": (structure.source_path if structure else ""),
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
        (self.run_dir / "report.md").write_text(
            self.render_report(meta, browser_obs), encoding="utf-8")

    def render_report(self, meta, browser_obs) -> str:
        lines = [f"# Mentis 协作运行报告 {self.run_id}", ""]
        lines.append(f"- 状态：**{self.status}**；模式 {self.mode}；目标：{self.session.goal}")
        lines.append(f"- 费用口径：{meta['currency_cap']}")
        lines.append("")
        lines.append("## 看结果")
        index = self.workspace / "index.html"
        lines.append(f"- 作品：`{index.resolve()}`（{'存在' if index.is_file() else '未生成'}，可直接用浏览器打开）")
        for shot in meta.get("screenshots", []):
            lines.append(f"- 截图：`{Path(shot).resolve()}`")
        if browser_obs:
            lines.append(f"- 浏览器：{browser_obs.browser} {browser_obs.browser_version}")
        pr = getattr(self, "last_professional_review", None) or {}
        if pr.get("performed"):
            lines.append(
                f"- 交付前专业评审：已执行（{pr.get('origin')}）；候选 {pr.get('candidates')} 条 → "
                f"采纳 {pr.get('adopted')}、驳回 {pr.get('rejected')}、保持现状 {pr.get('keep')}、"
                f"待用户/待查 {pr.get('needs_user')}"
                + (f"；修订复检判定 **{pr.get('verdict')}**" if pr.get("verdict") else "")
                + ("；**无截图输入，视觉维度未验证**" if pr.get("unverified_visual") else ""))
            if pr.get("note"):
                lines.append(f"- 评审结论：{pr['note']}")
        else:
            lines.append(f"- 交付前专业评审：**未执行**（{pr.get('reason', '未运行')}）"
                         "——不宣称领域标准评审通过")
        lines.append("- 阶段性交付语义（PROJECT_PLAN §7）：**DELIVERED = 本轮初稿已产出、"
                     "结构层硬要求检查通过、可查看**；不表示最终任务完成或用户满意，"
                     "你的意见将形成下一轮任务并继续委托修改")
        feedback = self.session.user_feedback
        if feedback:
            latest = sorted(feedback.items(), key=lambda kv: kv[1].get("at", ""))[-1]
            lines.append(f"- 用户是否满意：已记录 {len(feedback)} 条（最新 "
                         f"{'满意' if latest[1].get('satisfied') else '不满意'}，"
                         f"逐字原话见 session.json user_feedback）")
        else:
            lines.append("- 用户是否满意：**未反馈**（沉默≠满意；可稍后用 "
                         "`field-feedback` 或 `collab-resume --feedback` 提交原话）")
        lines.append("- 视觉与表单交互：**未验证真实点击/提交与网络行为**"
                     "（截图为初始画面；渲染测量仅覆盖声明行为与布局数值，提交测试强制阻止导航）")
        lines.append("")
        lines.append("## 看变化")
        for b in self.session.briefs:
            state = f"被 v{b.superseded_by} 替代" if b.superseded_by else "当前"
            lines.append(f"- brief v{b.version}（{state}）：{b.objective}")
            for item in b.constraints + b.preferences:
                lines.append(f"  - [{item.provenance.value}] {item.text[:60]}")
        for r in self.codex_runs:
            lines.append(f"- 委托 {r.action_key}（{r.attempt_id}，{r.status}，{r.duration_s}s，"
                         f"用量 {r.usage}）：产物 {r.artifacts or '（无新增文件）'}")
        versions = self.run_dir / "versions"
        if versions.is_dir():
            for snap in sorted(versions.glob("index-*.html")):
                lines.append(f"- 修订前快照：`{snap.resolve()}`（旧版产物本体，可与当前版本对照）")
        lines.append("")
        lines.append("## 看依据")
        if getattr(self, "_conversation_ready", False):
            lines.append(f"- Mentis 与 Codex 的可见交流：`{(self.run_dir / 'conversation.md').resolve()}`"
                         "（确切委托、公开回复与动作摘要；本地私有，不含内部推理）")
        else:
            lines.append("- 交流视图未生成；可从 trace/events.jsonl 与 trace/prompts/ 核对。")
        lines.append("- 授权：exec-workspace（user-cli-invocation）；策略代决范围："
                     + "；".join(self.session.current_policy.decided_scope))
        for q in self.question_routes:
            lines.append(f"- 反问分流：{q['question'][:60]} → {q['route']}；理由：{q['rationale'][:90]}")
        for ix in self.session.interactions:
            if ix.kind.value == "request_decision":
                state = "已回答（" + (ix.answer_source or "") + "）" if ix.answer_source else "**未回答（沉默≠批准）**"
                lines.append(f"- 请求决定：{ix.message[:60]}（阻塞 {ix.blocking_action}）——{state}")
        lines.append(f"- 模型用量：{meta['model_usage']}")
        for name, label in (("professional-review.json", "专业评审候选审查（采纳/驳回/保持现状+理由）"),
                            ("diagnosis-professional.md", "专业评审诊断（真实模型/Mock，含证据引用）"),
                            ("model-review-professional.json", "修订后模型复查（绑定产物哈希）"),
                            ("professional-verify.json", "修订后确定性复检（保留项/回归/测量）")):
            evidence = self.run_dir / name
            if evidence.is_file():
                lines.append(f"- {label}：`{evidence.resolve()}`")
        if self.last_diagnosis is not None:
            lines.append(f"- 模型诊断候选：{self.run_dir / 'diagnosis-professional.md'}"
                         f"（有效 {len(self.last_diagnosis.valid_diagnoses)} / 拒收 "
                         f"{len(self.last_diagnosis.invalid_diagnoses)}）")
        lines.append("- 未验证：视觉美观、点击/表单真实提交与网络行为、对比度、部署表现；"
                     "截图仅初始画面；资源与案例为草案（pending_human）；"
                     "专业评审候选与审查同源模型（独立性未建立），不构成专家能力结论。")
        lines.append("")
        return "\n".join(lines)
