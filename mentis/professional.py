"""P2.4-C1 最小任务专业配置：职责、资源快照、任务条件与阶段绑定。

范围来源：LOCAL_PRODUCT_ROADMAP §3。六类最小记录：
- 专业职责：需求/方案/指导/观察/验收责任与明确不支持范围——程序预置并标注来源，
  不靠"资深工程师"措辞授予能力；
- 专业依据：资源 id、版本、内容 SHA-256、适用理由、来源与复核状态；按允许材料把
  资源内容快照进运行目录（professional-config/resources/），保证内容可复现；
- 任务条件：brief/策略版本引用、用户来源、保留要求、授权与预算引用；
- 能力范围：有资料/可执行/可观察/已验证范围分列（复用 CapabilityContext 四分）；
- 评价依据：配置先落盘，评估计划随后派生；bindings.jsonl 关联配置↔brief↔计划
  id/哈希（配置文件不含自身哈希，避免循环哈希）；
- 调用关联：各阶段调用引用配置版本/哈希与本阶段职责（model-calls 可选字段、
  stage 行可选 config 字段）。

一致性规则（大纲 §3）：
1. 首次使用前落盘；无法核验时不声称已绑定，依赖配置的委托不发起
   （ConfigPersistenceError，与 V1 PlanPersistenceError 同一 fail-closed 模式）。
2. 计划、诊断、委托与复检取用同一有效配置的相关部分；阶段角色保留，盲评只接收
   计划标准与观察（不接收原诊断答案、组别或配置内容）。
3. 注册表内容改变时，同一任务继续使用已绑定快照并记录漂移（不静默换内容）。
4. 目标/brief 改变时记录原因生成新版本；受影响旧计划/委托/评审以新版本为准，
   旧版本文件不改写；未受影响的适用授权保留。
5. 历史运行配置缺失显示未采集，不回填；旧运行字节与结论不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mentis.capabilities.registry import META_RE, ResourceMeta, load_registry, load_resource
from mentis.capabilities.selection import CapabilityContext, select_for_task

CONFIG_SCHEMA = "mentis-professional-config/1"
BINDING_SCHEMA = "mentis-config-binding/1"

# collab/guidance 各阶段决策需要的并集（计划层曾用 促销感来源，诊断层曾用 视觉层级；
# 统一为一次选择，避免阶段间悄悄改变选择输入）。
UNIFIED_DECISION_NEEDS = ["信息密度", "主操作层级", "表单校验行为", "促销感来源", "视觉层级"]
UNIFIED_TASK_KEYWORDS = ["单页活动介绍页"]

STAGE_RESPONSIBILITY = {
    "plan": "需求→计划：把目标/brief/素材整理为评估计划维度（程序预置职责）",
    "build": "方案→委托：组装委托指令与硬要求（程序预置职责）",
    "observe": "观察：结构解析、双视口截图、渲染测量（程序预置职责）",
    "diagnose": "指导诊断：基于观察与领域依据提出候选（程序预置职责）",
    "adjudicate": "处置审查：确定性核对与可验证性路由（程序预置职责）",
    "revise": "修订指导：修订指令与保留项（程序预置职责）",
    "reverify": "复检：确定性核对与盲比两序——只接收计划标准与观察（程序预置职责）",
}

RESPONSIBILITIES = {
    "requirements": "需求澄清：把用户目标/约束/素材整理为版本化 brief 与评估计划维度",
    "approach": "方案：委托指令、硬要求与验收项组装",
    "guidance": "指导：诊断候选审查、修订指令与保留项核对",
    "observation": "观察：结构解析、双视口截图、渲染测量（不可信时标未知）",
    "acceptance": "验收：计划硬要求核对、盲比复检与判定分级",
    "unsupported": [
        "点击/异步交互行为取证（当前观察不含）",
        "真实网络请求取证（工作区离线约束）",
        "观察不可信维度的专业结论（标未知，不冒充）",
        "未接入领域资源的其他领域专业判断",
    ],
    "source_note": "职责为程序预置并逐条标注；资源为草案且未经人工复核（pending_human）；"
                   "本配置不授予专业资格，专业水平由作品对照与独立评价另行验证",
}


class ConfigPersistenceError(RuntimeError):
    """专业配置落盘/核验失败：依赖配置的计划与委托不发起（fail-closed）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def config_dir(run_dir) -> Path:
    return Path(run_dir) / "professional-config"


def _config_path(run_dir, version: int) -> Path:
    return config_dir(run_dir) / f"config-v{version}.json"


def list_config_versions(run_dir) -> list:
    d = config_dir(run_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("config-v*.json"):
        m = re.fullmatch(r"config-v(\d+)\.json", p.name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def load_bindings(run_dir) -> list:
    path = config_dir(run_dir) / "bindings.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"schema": "corrupt_line", "raw": line[:120]})
    return rows


def _resource_status(text: str) -> str:
    m = META_RE.search(text)
    if m is None:
        return "unrecorded"
    try:
        return str(json.loads(m.group(1)).get("status", "unrecorded"))
    except json.JSONDecodeError:
        return "unrecorded"


@dataclass
class ProfessionalConfig:
    """一次任务的有效专业配置（mentis-professional-config/1）。

    sha256 不写入文件（避免循环哈希）；由写入方计算后保存在 bindings.jsonl
    与事件 refs 中，作为绑定主键。
    """

    version: int
    created_at: str
    goal: str
    reason_kind: str                      # first_freeze / goal_change / brief_revision / authorization_change
    reason_detail: str
    supersedes: int | None
    responsibilities: dict
    grounds: list                         # list[dict]（资源快照记录）
    task_conditions: dict
    capability_scope: dict
    sha256: str = ""                      # 内存值：写入后计算；不序列化

    def to_dict(self) -> dict:
        return {
            "schema": CONFIG_SCHEMA,
            "version": self.version,
            "created_at": self.created_at,
            "goal": self.goal,
            "reason_kind": self.reason_kind,
            "reason_detail": self.reason_detail,
            "supersedes": self.supersedes,
            "responsibilities": self.responsibilities,
            "grounds": self.grounds,
            "task_conditions": self.task_conditions,
            "capability_scope": self.capability_scope,
            "evaluation_basis": {
                "note": "评估计划随后派生；配置↔brief↔计划关联见 bindings.jsonl"
                        "（本文件不含自身哈希，避免循环哈希）",
            },
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "ProfessionalConfig":
        return cls(
            version=int(data["version"]), created_at=data.get("created_at", ""),
            goal=data.get("goal", ""), reason_kind=data.get("reason_kind", ""),
            reason_detail=data.get("reason_detail", ""),
            supersedes=data.get("supersedes"),
            responsibilities=data.get("responsibilities", {}),
            grounds=data.get("grounds", []),
            task_conditions=data.get("task_conditions", {}),
            capability_scope=data.get("capability_scope", {}),
        )

    def binding(self, stage: str) -> dict | None:
        """阶段调用关联：配置版本/哈希与本阶段职责；未冻结时返回 None（不填 0）。"""
        if not self.version or not self.sha256:
            return None
        return {"version": self.version, "sha256": self.sha256,
                "stage_responsibility": STAGE_RESPONSIBILITY.get(stage, "unrecorded")}


class ConfigManager:
    """任务内唯一的专业配置管理器：一次选择、快照、绑定与按需改版。

    - ensure()：首次调用冻结 v1（选择一次、资源快照落盘、配置先落盘、绑定与事件
      随后）；之后复用同一版本；brief/目标变化时记录原因生成新版本。
    - 所有阶段从同一 config/context/资源取用相关部分；注册表内容变化只记漂移，
      不静默换内容。
    - 恢复/续办：新实例从运行目录读回配置与快照并核验（哈希不符 → 不声称已绑定，
      依赖配置的委托不发起）。
    """

    def __init__(self, run_dir, session=None, goal: str = "",
                 task_label: str = "页面诊断与修改指导",
                 task_keywords: list | None = None,
                 decision_needs: list | None = None,
                 observation_capabilities: dict | None = None,
                 can_execute: bool = True, user_source: str = "",
                 budget_ref=None, trace=None, notify=None,
                 registry_path=None):
        self.run_dir = Path(run_dir)
        self.session = session
        self.goal = goal
        self.task_label = task_label
        self.task_keywords = list(task_keywords or UNIFIED_TASK_KEYWORDS)
        self.decision_needs = list(decision_needs or UNIFIED_DECISION_NEEDS)
        self.observation_capabilities = dict(observation_capabilities or
                                             {"structure_only": True, "screenshots": True,
                                              "render_measurement": True})
        self.can_execute = can_execute
        self.user_source = user_source
        self.budget_ref = budget_ref
        self.trace = trace
        self.notify = notify
        self.registry_path = registry_path
        self._config: ProfessionalConfig | None = None
        self._context: CapabilityContext | None = None
        self._resources: list = []
        self._drift_noted: set = set()

    # ---- 对外入口 ----

    def ensure(self, brief=None) -> tuple:
        """返回 (config, context, 已加载资源)；同任务内重复调用复用同一版本。"""
        brief = brief if brief is not None else (
            self.session.current_brief if self.session is not None else None)
        if self._config is not None:
            # 审查修正 C：同一实例的复用与冷恢复采用同一核验——每次使用前重验
            # 配置字节、绑定记录与资源快照，不可核验即失败（不静默沿用缓存）。
            self._reverify()
            if self._conditions_changed(self._config, brief):
                reason_kind, detail = self._change_reason(self._config, brief)
                return self.revise(reason_kind, detail, brief)
            self._note_drift()
            return self._config, self._context, self._resources
        versions = list_config_versions(self.run_dir)
        if versions:
            config = self._load_and_verify(versions[-1])
            # 审查修正 D：首次恢复即比较磁盘配置与当前任务条件——目标/brief 已变时
            # 第一次使用前完成有原因改版，不依赖第二次调用纠正。
            if self._conditions_changed(config, brief):
                reason_kind, detail = self._change_reason(config, brief)
                return self.revise(reason_kind, detail, brief)
            self._config = config
            self._context, self._resources = self._restore(config)
            self._note_drift()
            return self._config, self._context, self._resources
        return self.freeze(brief)

    def loaded_resources(self) -> list:
        return list(self._resources)

    def binding(self, stage: str) -> dict | None:
        if self._config is None:
            return None
        return self._config.binding(stage)

    # ---- 冻结与改版 ----

    def freeze(self, brief=None) -> tuple:
        goal = brief.objective if brief is not None else self.goal
        config = ProfessionalConfig(
            version=1, created_at=_now(), goal=goal,
            reason_kind="first_freeze",
            reason_detail="首次使用前落盘（配置先于计划与委托）",
            supersedes=None,
            responsibilities=dict(RESPONSIBILITIES),
            grounds=[],
            task_conditions=self._task_conditions(brief),
            capability_scope={},
        )
        return self._write_new(config, brief)

    def revise(self, reason_kind: str, detail: str, brief=None) -> tuple:
        versions = list_config_versions(self.run_dir)
        if self._config is None and not versions:
            return self.freeze(brief)
        previous = self._config or self._load_and_verify(versions[-1])
        brief = brief if brief is not None else (
            self.session.current_brief if self.session is not None else None)
        config = ProfessionalConfig(
            version=previous.version + 1, created_at=_now(),
            goal=brief.objective if brief is not None else previous.goal,
            reason_kind=reason_kind, reason_detail=detail,
            supersedes=previous.version,
            responsibilities=dict(previous.responsibilities),
            grounds=previous.grounds,
            task_conditions=self._task_conditions(brief),
            capability_scope=previous.capability_scope,
        )
        return self._write_new(config, brief)

    def _write_new(self, config: ProfessionalConfig, brief=None) -> tuple:
        """一次选择 → 资源快照 → 配置落盘 → 绑定行 → 事件（顺序即审计顺序）。"""
        context, resources = self._select()
        config.grounds = self._snapshot(config, context, resources)
        config.capability_scope = {
            **context.to_dict()["ability_labels"],
            "observation_capabilities": dict(context.observation_capabilities),
            "matched_needs": list(context.matched_needs),
            "unknown_needs": list(context.unknown_needs),
            "declared_conflicts": list(context.declared_conflicts),
        }
        path = _config_path(self.run_dir, config.version)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = config.to_json_bytes()
            path.write_bytes(payload)
        except OSError as exc:
            raise ConfigPersistenceError(
                f"专业配置落盘失败，依赖配置的计划与委托不发起：{exc}") from exc
        config.sha256 = self._sha256_bytes(payload)
        reason = (f"{config.reason_kind}：{config.reason_detail}"
                  if config.version > 1 else config.reason_detail)
        if not self._append_binding(config, plan=None, reason=reason):
            raise ConfigPersistenceError(
                "专业配置绑定记录写入失败，不声称已绑定；依赖配置的计划与委托不发起")
        self._config = config
        self._context, self._resources = context, list(resources)
        kind = ("professional_config.revised" if config.version > 1
                else "professional_config.recorded")
        summary = (
            f"专业配置 v{config.version} 已落盘（{reason}）："
            f"资源 {len(config.grounds)} 项（{'、'.join(g['resource_id'] + '@' + g['content_sha256'][:12] for g in config.grounds) or '无'}；"
            f"复核状态 {'、'.join(sorted({g['review_status'] for g in config.grounds}) or {'unrecorded'})}）；"
            f"能力范围：{config.capability_scope.get('verified_scope', 'unrecorded')}"
            + (f"；受影响旧计划/委托/评审以新版本为准，旧版本文件不改写"
               if config.version > 1 else ""))
        if self.trace is not None:
            self.trace.emit("mentis", kind, summary,
                            refs=[{"path": str(path), "sha256": config.sha256,
                                   "role": "professional_config"}])
        if self.notify is not None:
            self.notify(f"专业配置 v{config.version} 已冻结（先于计划/委托落盘；"
                        f"{len(config.grounds)} 项资源快照绑定）")
        return self._config, self._context, self._resources

    def note_plan(self, plan) -> bool:
        """把配置与已落盘计划关联（bindings.jsonl 追加一行）；同版计划不重复记。"""
        if self._config is None or plan is None:
            return False
        for row in load_bindings(self.run_dir):
            if (row.get("event") == "bind_plan"
                    and row.get("config_version") == self._config.version
                    and row.get("plan_sha256") == (plan.sha256 or "")):
                return False
        return self._append_binding(
            self._config, plan={"version": plan.version, "sha256": plan.sha256 or ""},
            reason="计划落盘后关联（配置先落盘，计划随后派生）")

    # ---- 内部：选择/快照/恢复/核验 ----

    def _select(self) -> tuple:
        try:
            registry = load_registry(self.registry_path)
            return select_for_task(
                registry, task_label=self.task_label, task_keywords=self.task_keywords,
                decision_needs=self.decision_needs,
                observation_capabilities=self.observation_capabilities,
                can_execute=self.can_execute, registry_path=self.registry_path)
        except ConfigPersistenceError:
            raise
        except Exception as exc:
            raise ConfigPersistenceError(
                f"资源选择失败（注册表不可读/解析错误），不使用缺失依据冻结：{exc}") from exc

    def _registry_base(self) -> Path:
        if self.registry_path:
            return Path(self.registry_path).parent
        from mentis.capabilities.registry import default_registry_path
        return default_registry_path().parent

    def _snapshot(self, config: ProfessionalConfig, context, resources) -> list:
        base = self._registry_base()
        grounds = []
        res_dir = config_dir(self.run_dir) / "resources"
        for sel, res in zip(context.selections, resources):
            src = base / res.meta.file
            try:
                content = src.read_bytes()
            except OSError as exc:
                raise ConfigPersistenceError(
                    f"资源 {sel.resource_id} 内容无法读取，不使用缺失依据冻结：{exc}") from exc
            content_sha = self._sha256_bytes(content)
            # 审查修正 B：快照按内容哈希寻址（不可变）——内容不同→不同文件，改版
            # 不覆盖旧版本引用的快照；内容相同→同一文件幂等复用（字节不变）。
            name = f"{content_sha[:16]}-{sel.resource_id}{Path(res.meta.file).suffix or '.md'}"
            target = res_dir / name
            if not target.is_file():  # 已存在即同内容（哈希寻址），不重写既有字节
                try:
                    res_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                except OSError as exc:
                    raise ConfigPersistenceError(
                        f"资源 {sel.resource_id} 快照落盘失败，不声称已绑定：{exc}") from exc
            grounds.append({
                "resource_id": sel.resource_id,
                "version": sel.version,
                "source_file": res.meta.file,
                "content_sha256": content_sha,
                "snapshot": f"resources/{name}",
                "applicability_rationale": list(sel.rationale),
                "matched_decision_questions": list(sel.matched_decision_questions),
                "provenance": res.meta.provenance,
                "review_status": _resource_status(content.decode("utf-8", errors="replace")),
            })
        return grounds

    def _restore(self, config: ProfessionalConfig) -> tuple:
        """从快照重建资源与上下文（不用 live 注册表内容）。"""
        res_base = config_dir(self.run_dir) / "resources"
        resources, selections = [], []
        for g in config.grounds:
            meta = ResourceMeta(
                id=g["resource_id"], version=g["version"],
                provenance=g.get("provenance", ""), domain="",
                applies_to=[], excludes=[], decision_questions=[],
                observation_requirements=[], file=Path(g["snapshot"]).name)
            res = load_resource(meta, res_base / "_.json")
            resources.append(res)
            selections.append(_selection_from_ground(g, res))
        context = _context_from_config(config, selections)
        return context, resources

    def _load_and_verify(self, version: int) -> ProfessionalConfig:
        path = _config_path(self.run_dir, version)
        try:
            payload = path.read_bytes()
            data = json.loads(payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigPersistenceError(f"专业配置 v{version} 无法读取，不声称已绑定：{exc}") from exc
        config = ProfessionalConfig.from_dict(data)
        self._verify_against(config, self._sha256_bytes(payload))
        config.sha256 = self._sha256_bytes(payload)
        return config

    def _reverify(self):
        """审查修正 C：同一实例缓存复用前重验（与冷恢复同一规则）——配置字节、
        绑定记录、资源快照任一不可核验即抛 ConfigPersistenceError。"""
        if self._config is None:
            return
        path = _config_path(self.run_dir, self._config.version)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ConfigPersistenceError(
                f"专业配置 v{self._config.version} 无法读取，不声称已绑定：{exc}") from exc
        actual = self._sha256_bytes(payload)
        if actual != self._config.sha256:
            raise ConfigPersistenceError(
                f"专业配置 v{self._config.version} 字节与冻结时不符（现 {actual[:12]}）："
                "不声称已绑定，依赖配置的计划与委托不发起")
        self._verify_against(self._config, actual)

    def _verify_against(self, config: ProfessionalConfig, actual_sha: str) -> None:
        expected = self._expected_sha(config.version)
        if not expected:
            raise ConfigPersistenceError(
                f"专业配置 v{config.version} 缺少绑定记录，无法核验：不声称已绑定，"
                "依赖配置的计划与委托不发起")
        if expected != actual_sha:
            raise ConfigPersistenceError(
                f"专业配置 v{config.version} 字节与绑定记录不符（现 {actual_sha[:12]}，"
                f"绑定期望 {expected[:12]}）：不声称已绑定，依赖配置的计划与委托不发起")
        for g in config.grounds:
            snap = config_dir(self.run_dir) / g.get("snapshot", "")
            try:
                content = snap.read_bytes()
            except OSError as exc:
                raise ConfigPersistenceError(
                    f"资源 {g['resource_id']} 快照缺失/不可读，不使用缺失依据派发：{exc}") from exc
            if self._sha256_bytes(content) != g.get("content_sha256"):
                raise ConfigPersistenceError(
                    f"资源 {g['resource_id']} 快照内容与配置记录不符：不声称已绑定，"
                    "依赖配置的计划与委托不发起")

    def _expected_sha(self, version: int) -> str:
        for row in load_bindings(self.run_dir):
            if row.get("event") == "bind" and row.get("config_version") == version:
                return str(row.get("config_sha256", ""))
        return ""

    def _task_conditions(self, brief) -> dict:
        session = self.session
        conditions: dict = {
            "brief_version": brief.version if brief is not None else None,
            "goal": brief.objective if brief is not None else self.goal,
            "user_source": self.user_source or "unrecorded",
            "budget_ref": self.budget_ref if self.budget_ref else "unrecorded",
        }
        if session is not None:
            try:
                policy = session.current_policy
            except (IndexError, AttributeError):
                policy = None
            conditions["policy"] = {
                "version": policy.version if policy else None,
                "reserved_decisions": list(policy.reserved_decisions) if policy else [],
                "decided_scope": list(policy.decided_scope) if policy else [],
                "source_ref": policy.source_ref if policy else "",
            }
            conditions["authorizations"] = [
                {"auth_id": a.auth_id, "scope": a.scope, "granted_by": a.granted_by,
                 "status": getattr(getattr(a, "status", None), "value", str(getattr(a, "status", "")))}
                for a in session.authorizations]
        else:
            conditions["policy"] = {}
            conditions["authorizations"] = []
        return conditions

    def _conditions_changed(self, config: ProfessionalConfig, brief) -> bool:
        if config is None or brief is None:
            return False
        tc = config.task_conditions
        return (tc.get("brief_version") != brief.version
                or tc.get("goal") != brief.objective)

    def _change_reason(self, config: ProfessionalConfig, brief) -> tuple:
        tc = config.task_conditions
        if brief is not None and tc.get("goal") != brief.objective:
            return "goal_change", (f"目标变化：「{str(tc.get('goal', ''))[:40]}」→"
                                   f"「{brief.objective[:40]}」")
        return "brief_revision", (f"brief 改版：v{tc.get('brief_version')}→v{brief.version}"
                                  "（用户修正/新增偏好进入 brief，受影响条目随之更新）")

    def _note_drift(self):
        """注册表内容与快照不同 → 记录漂移（一次），继续使用快照，不静默换内容。"""
        if self._config is None or self.trace is None:
            return
        base = self._registry_base()
        for g in self._config.grounds:
            if g["resource_id"] in self._drift_noted:
                continue
            try:
                live = (base / g.get("source_file", "")).read_bytes()
                drifted = self._sha256_bytes(live) != g.get("content_sha256")
            except OSError:
                live, drifted = b"", True
            if drifted:
                self._drift_noted.add(g["resource_id"])
                self.trace.emit(
                    "mentis", "professional_config.drift",
                    f"注册表资源 {g['resource_id']} 当前内容与任务快照不同"
                    f"（{'文件缺失' if not live else '内容变化'}）：本任务继续使用快照"
                    f" v{self._config.version}，不静默换内容",
                    refs=[{"path": str(config_dir(self.run_dir) / g.get("snapshot", "")),
                           "sha256": g.get("content_sha256", ""),
                           "role": "professional_config"}])

    def _append_binding(self, config: ProfessionalConfig, plan=None, reason: str = "") -> bool:
        row = {
            "schema": BINDING_SCHEMA,
            "created_at": _now(),
            "event": "bind_plan" if plan else "bind",
            "config_version": config.version,
            "config_sha256": config.sha256,
            "brief_version": config.task_conditions.get("brief_version"),
            "reason": reason[:200],
        }
        if plan:
            row["plan_version"] = plan["version"]
            row["plan_sha256"] = plan["sha256"]
        path = config_dir(self.run_dir) / "bindings.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as out:
                out.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            return False
        return True

    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        import hashlib
        return hashlib.sha256(payload).hexdigest()


def _selection_from_ground(g: dict, res):
    from mentis.capabilities.selection import ResourceSelection
    return ResourceSelection(
        resource_id=g["resource_id"], version=g["version"],
        rationale=list(g.get("applicability_rationale", [])),
        matched_decision_questions=list(g.get("matched_decision_questions", [])),
        rules_loaded=res.rules,
        observation_requirements=[])


def _context_from_config(config: ProfessionalConfig, selections) -> CapabilityContext:
    scope = config.capability_scope
    labels = scope if scope else {}
    return CapabilityContext(
        task_label="任务专业配置",
        selections=selections,
        matched_needs=list(scope.get("matched_needs", [])),
        unknown_needs=list(scope.get("unknown_needs", [])),
        declared_conflicts=list(scope.get("declared_conflicts", [])),
        observation_capabilities=dict(scope.get("observation_capabilities", {})),
        can_execute=bool(labels.get("can_execute", False)),
        created_at=config.created_at)
