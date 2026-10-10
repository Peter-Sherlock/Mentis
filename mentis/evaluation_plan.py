"""P2.4-V1 评估计划先行（ADR-008 第 1—5、9 条）。

每个 collab 任务在首次委托执行器之前形成版本化评估计划（EvaluationPlan），
先落盘、计算哈希、写入 trace，再委托；此后的硬要求检查读计划中的确定性条目，
不再读硬编码的试点值。

纪律（ADR-008）：
- 只组装，不发明：条目来自四层来源——内核（与领域无关的固定标准）、用户
  （brief 约束与偏好，沿用 Provenance/source_ref）、领域（已加载资源规则，
  id@版本+性质）、任务实例（从任务素材推导的具体验收项）。逐条标注来源。
- Mentis 或模型推导的条目标 agent_selected+provisional，不能阻塞交付；
  hypothesis 规则不阻塞交付，也不作为修改依据。
- 每个维度写明：含义、类别、来源、需要的观察、检查方式、通过线、是否阻塞、
  无法检查时的处置。检查方式四选一：deterministic / pairwise_blind /
  user_judgment / unverifiable。标准写结果，不写实现。
- 计划只能有原因地修订（目标变化/brief 改版、标准本身有错、发现新盲区），
  改了出新版本并写明原因；旧版本文件不改写。
- 模型实例化只允许引用已加载规则 id 或用户/brief/素材原话；虚构来源拒收留痕；
  无凭据或调用失败退回纯确定性组装并如实标注，不阻塞任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import uuid

PLAN_SCHEMA = "mentis-eval-plan/1"

CHECK_METHODS = ("deterministic", "pairwise_blind", "user_judgment", "unverifiable")
CATEGORIES = ("hard_requirement", "professional_advice", "user_preference", "hypothesis")
SOURCE_LAYERS = ("core", "user", "domain", "task_instance")
REVISION_REASONS = ("brief_revision", "goal_change", "standard_error", "new_blind_spot")

CATEGORY_LABEL = {
    "hard_requirement": "硬要求",
    "professional_advice": "专业建议",
    "user_preference": "用户偏好",
    "hypothesis": "待验证假设",
}
CHECK_METHOD_LABEL = {
    "deterministic": "确定性检查",
    "pairwise_blind": "前后盲比",
    "user_judgment": "用户判断",
    "unverifiable": "当前无法验证",
}

# 可执行确定性检查（collab.hard_requirement_review / 专业评审段按 kind 分派）
SPEC_TEXT_CONTAINS = "text_contains"
SPEC_FORM_FIELDS = "form_fields_present"
SPEC_TEXT_ABSENT = "text_absent"
SPEC_NO_EXTERNAL_REFS = "no_external_refs"
SPEC_RENDER_OVERFLOW = "render_overflow"

MODEL_ENTRY_LIMIT = 4  # 模型实例化条目上限，保持计划短（试点目标 5—10 维度量级）


class PlanPersistenceError(RuntimeError):
    """评估计划落盘失败：调用方必须停止本次委托（先落盘后委托）。"""


def blocking_rule(category: str, check_method: str, provisional: bool) -> bool:
    """阻塞判定：硬要求 + 确定性可查 + 非暂定。provisional/hypothesis/不可验证一律不阻塞。"""
    return (category == "hard_requirement" and check_method == "deterministic"
            and not provisional)


@dataclass
class PlanEntry:
    """评估计划的一个维度：写结果标准，不写实现。"""

    entry_id: str
    name: str
    meaning: str
    category: str
    source_layer: str
    source_refs: list
    observation: str
    check_method: str
    pass_line: str
    blocking: bool
    disposition: str = ""       # 无法检查时的处置；空表示当前可查
    priority: int = 2
    agent_selected: bool = False
    provisional: bool = False
    check_spec: dict = field(default_factory=dict)  # 可执行确定性检查；无则为 {}
    derived_by: str = "deterministic_assembly"
    note: str = ""
    search_text: str = ""       # 候选→维度映射的检索文本（聚合条目含被聚合规则原文）

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "PlanEntry":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EvaluationPlan:
    """版本化评估计划；落盘文件为规范化 JSON，哈希由 trace refs 与调用方持有。"""

    schema_version: str
    plan_id: str
    version: int
    brief_version: int
    goal: str
    created_at: str
    entries: list                            # list[PlanEntry]
    priorities_note: str
    tradeoffs: list                          # 优先级与取舍
    stop_conditions: list                    # 停止条件
    out_of_scope: list                       # 不评估的范围
    model_instantiation: dict                # {status, model, usage, rejected, note}
    revision: dict = field(default_factory=dict)  # v1 为 {}；改版含 reason_kind/detail/affected
    material_assessment: dict = field(default_factory=dict)  # 目标—素材主体一致性判定（审计 #1）
    supersedes_sha256: str = ""
    sha256: str = ""                         # 落盘时对文件字节计算

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["entries"] = [e.to_dict() for e in self.entries]
        return data

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
                + "\n").encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "EvaluationPlan":
        data = dict(data)
        data["entries"] = [PlanEntry.from_dict(e) for e in data.get("entries", [])]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    # ---- 查询 ----

    def entry(self, entry_id: str) -> PlanEntry | None:
        for e in self.entries:
            if e.entry_id == entry_id:
                return e
        return None

    def executable_entries(self) -> list:
        """结构观察阶段可执行的确定性条目（check_spec 非空且非渲染测量类）。"""
        return [e for e in self.entries
                if e.check_spec and e.check_spec.get("kind") not in (SPEC_RENDER_OVERFLOW,)]

    def blocking_entries(self) -> list:
        return [e for e in self.entries if e.blocking]

    def fact_needles(self) -> dict:
        """任务实例事实 needle（label → 文本），供保留项核对与预期值视图。"""
        needles: dict = {}
        for e in self.entries:
            if e.check_spec.get("kind") == SPEC_TEXT_CONTAINS:
                needles.update(e.check_spec.get("needles", {}))
        return needles

    def form_fields(self) -> list:
        for e in self.entries:
            if e.check_spec.get("kind") == SPEC_FORM_FIELDS:
                return list(e.check_spec.get("required_named", []))
        return []

    def blind_review_standards(self) -> list:
        """盲比复检可用的标准条目：排除 hypothesis（不作为依据）与 unverifiable（无从判）。"""
        return [e for e in self.entries
                if e.check_method in ("deterministic", "pairwise_blind", "user_judgment")
                and e.category != "hypothesis"]

    def search_blob(self, entry: "PlanEntry") -> str:
        text = entry.search_text or "；".join(
            [entry.name, entry.meaning, entry.pass_line, entry.observation, *entry.source_refs])
        return text


# ---- 任务素材事实推导（任务实例层；标注化素材格式，机械提取不发明） ----

_FACT_LABELS = {"活动名称": "name", "日期时间": "datetime", "地点": "venue",
                "价格": "price", "报名方式": "signup"}
_SIGNUP_FIELD_KEYWORDS = (("姓名", "姓名"), ("联系方式", "联系方式"),
                          ("电话", "电话"), ("邮箱", "邮箱"))


def derive_task_facts(material_text: str, briefs=()) -> dict:
    """从标注化任务素材（“- 标签：值”行）机械提取关键事实；brief 后版本价格修正覆盖。

    只提取已知标签，不做语义推断；提取不到的标签如实缺失，不虚构默认值。
    """
    facts: dict = {}
    for line in (material_text or "").splitlines():
        body = line.strip()
        if not body.startswith("- "):
            continue
        label, sep, value = body[2:].partition("：")
        if not sep:
            continue
        key = _FACT_LABELS.get(label.strip())
        if key and key not in facts:
            facts[key] = value.strip()
    needles: dict = {}
    if facts.get("name"):
        needles["活动名称"] = facts["name"]
    if facts.get("datetime"):
        match = re.search(r"\d{4}-\d{2}-\d{2}", facts["datetime"])
        if match:
            needles["日期时间"] = match.group(0)
    if facts.get("venue"):
        venue = re.sub(r"（[^）]*）", "", facts["venue"])
        venue = venue.split("·")[-1].strip() if "·" in venue else venue.strip()
        if venue:
            needles["地点"] = venue
    if facts.get("price"):
        match = re.search(r"¥\s*\d+", facts["price"])
        if match:
            needles["价格"] = match.group(0).replace(" ", "")
    price_updates = []
    for b in briefs or ():
        for item in (b.constraints or []) + (b.preferences or []):
            for match in re.finditer(r"¥\s*(\d+)", item.text):
                needles["价格"] = f"¥{match.group(1)}"  # “从 ¥99 改为 ¥129”取改后值
                price_updates.append(item.text[:60])
    form_fields = [name for kw, name in _SIGNUP_FIELD_KEYWORDS
                   if facts.get("signup") and kw in facts["signup"]]
    return {"facts": facts, "needles": needles, "form_fields": form_fields,
            "price_updates": price_updates}


# ---- 目标—素材一致性判定（P3.4 审计 #1：确定性、可审计、不确定时不判不一致） ----

_QUOTED_RE = re.compile(r"[「“]([^」”]{2,24})[”」]")
# 词面对照的通用词停用表：品牌/品类/载体词不足以判「素材就是为本目标准备的」
_GENERIC_GRAMS = {
    "拾光", "咖啡", "本地", "单页", "介绍", "页面", "活动", "简洁", "温暖", "一个",
    "做一", "制作", "产品", "新品", "上新", "虚拟", "虚构", "品牌", "信息", "内容",
}


def _cn_bigrams(text: str) -> set:
    cleaned = re.sub(r"[\s\W]+", "", text or "")
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def assess_material_goal_consistency(goal: str, material_text: str) -> dict:
    """确定性判断任务素材的主体是否与目标一致（审计 20261010 #1 的结构修复）。

    规则（全部词面、可审计；判不了就 undetermined，不假装知道）：
    1. 取素材主体：facts["活动名称"]，缺则取首个 "# " 标题行（去括注）。
    2. 目标主体：目标中「」/“” 引用的片段（如 「焦糖苹果拿铁）。
    3. 有目标主体：任一主体词出现在素材中 → consistent；否则素材主体出现在
       目标中 → consistent；否则 → mismatch（两边互不含对方主体）。
    4. 无目标主体：素材主体与目标做去停用词的二元组重叠，共享 ≥2 → consistent；
       0 → mismatch；否则 undetermined。
    """
    goal = goal or ""
    facts = derive_task_facts(material_text).get("facts", {})
    subject = (facts.get("name") or "").strip()
    if not subject:
        for line in (material_text or "").splitlines():
            body = line.strip()
            if body.startswith("# ") and len(body) > 3:
                subject = re.sub(r"（[^）]*）", "", body[2:]).strip()
                break
    if not subject:
        return {"verdict": "undetermined", "material_subject": "",
                "goal_subjects": [], "shared": [], "rule": "素材主体不可确定"}
    goal_subjects = [q.strip() for q in _QUOTED_RE.findall(goal) if q.strip()]
    if goal_subjects:
        hit = [q for q in goal_subjects if q in (material_text or "")]
        if hit:
            return {"verdict": "consistent", "material_subject": subject,
                    "goal_subjects": goal_subjects, "shared": hit,
                    "rule": "目标引用主体出现在素材中"}
        if subject in goal:
            return {"verdict": "consistent", "material_subject": subject,
                    "goal_subjects": goal_subjects, "shared": [subject],
                    "rule": "素材主体出现在目标中"}
        return {"verdict": "mismatch", "material_subject": subject,
                "goal_subjects": goal_subjects, "shared": [],
                "rule": "目标引用主体不出现在素材中，且素材主体不出现在目标中"}
    subj_grams = {g for g in _cn_bigrams(subject) if g not in _GENERIC_GRAMS}
    goal_grams = {g for g in _cn_bigrams(goal) if g not in _GENERIC_GRAMS}
    shared = sorted(subj_grams & goal_grams)
    if len(shared) >= 2:
        return {"verdict": "consistent", "material_subject": subject,
                "goal_subjects": [], "shared": shared[:6],
                "rule": "主体词面共享 ≥2"}
    if not shared and subj_grams and goal_grams:
        return {"verdict": "mismatch", "material_subject": subject,
                "goal_subjects": [], "shared": [],
                "rule": "主体词面无共享（目标未引用主体时以词面重叠判定）"}
    return {"verdict": "undetermined", "material_subject": subject,
            "goal_subjects": [], "shared": shared[:6],
            "rule": "主体词面证据不足"}


# ---- 四层组装 ----

def build_core_entries() -> list:
    """内核层：与领域无关的固定标准（ADR-008 第 2 条）。忠实于意图由任务实例条目落实。"""
    core = [
        PlanEntry(
            entry_id="K1", name="约束、保留项与委托边界不被破坏",
            meaning="brief 约束与保留项在产物与修改过程中不被破坏；改动不越出授权范围（不越界）",
            category="hard_requirement", source_layer="core",
            source_refs=["core:adr008-constraints-reserved-scope"],
            observation="产物、修订前快照、工作区文件清单与约束/保留项清单",
            check_method="deterministic",
            pass_line="既有约束与保留项全部保持；无越出工作区/授权范围的改动",
            blocking=True, priority=1,
            note="结构阶段由用户/系统约束条目核对（外链等）；修订轮由确定性复检逐项核对保留项",
        ),
        PlanEntry(
            entry_id="K2", name="无回归",
            meaning="已满足的要求不因修改而失效",
            category="hard_requirement", source_layer="core",
            source_refs=["core:no-regression"],
            observation="修订前快照、修订后观察与渲染测量",
            check_method="deterministic",
            pass_line="修订轮逐项核对保留项与表单必填，无回归记录；首稿无前版，标注不适用",
            blocking=True, priority=1,
            note="由专业评审段确定性复检执行（修订轮适用）",
        ),
        PlanEntry(
            entry_id="K3", name="未验证不宣称已验证",
            meaning="凡未取得对应观察的维度，报告与判定必须如实标注未验证",
            category="hard_requirement", source_layer="core",
            source_refs=["core:no-unverified-claims"],
            observation="本轮报告、判定与观察记录",
            check_method="deterministic",
            pass_line="报告中不存在无观察依据的“已验证”表述",
            blocking=False, priority=1,
            disposition="发现违反：先更正报告与判定再交付；不冒充已验证",
        ),
    ]
    return core


def build_user_entries(brief) -> list:
    """用户层：brief 约束与偏好，沿用其 Provenance/source_ref；约束可映射的给确定性检查。"""
    entries = []
    for index, item in enumerate(brief.constraints or [], start=1):
        check_method, spec = "unverifiable", {}
        if any(word in item.text for word in ("无网络", "外链", "不发送")):
            check_method, spec = "deterministic", {"kind": SPEC_NO_EXTERNAL_REFS}
        name_text = item.text if len(item.text) <= 24 else item.text[:24] + "…"
        entries.append(PlanEntry(
            entry_id=f"U{index}", name=f"约束：{name_text}".replace("；", "、"),
            meaning=f"brief 约束不被破坏：{item.text}",
            category="hard_requirement", source_layer="user",
            source_refs=[f"brief:v{brief.version}:{item.source_ref or item.provenance.value}",
                         f"provenance:{item.provenance.value}"],
            observation="工作区产物与文件清单", check_method=check_method,
            pass_line=item.text[:80] + ("…" if len(item.text) > 80 else ""),
            blocking=blocking_rule("hard_requirement", check_method, False),
            priority=1, check_spec=spec,
            disposition="无法静态核验的部分如实标注未验证，不阻塞交付",
        ))
    preference_ids = []
    for index, item in enumerate(brief.preferences or [], start=1):
        preference_ids.append(f"brief:v{brief.version}:{item.source_ref or item.provenance.value}（{item.text[:30]}）")
    if preference_ids:
        entries.append(PlanEntry(
            entry_id="U-P", name="用户偏好与暂定方向",
            meaning="brief 偏好条目（含暂定默认）应被尊重，但审美与措辞无唯一正确答案",
            category="user_preference", source_layer="user",
            source_refs=preference_ids,
            observation="用户对实际产物的反馈（field-feedback / collab-resume）",
            check_method="user_judgment",
            pass_line="用户未表达不满前不据此修改；表达后按反馈修订",
            blocking=False, priority=3,
            disposition="等待用户反馈；不自动修改，不冒充唯一标准",
        ))
    return entries


def build_domain_entries(resources, realized_rule_ids=()) -> list:
    """领域层（确定性组装）：硬约束规则逐条入计划；guideline/preference/hypothesis 聚合。

    已被任务实例条目落实的规则（realized_rule_ids）不再单列——它们作为该条目的
    source_ref 出现，避免同一标准重复成维度、计划膨胀。当前观察工具（结构观察/
    渲染测量）只能覆盖部分规则；无法执行的检查一律 unverifiable 并如实处置。
    """
    entries = []
    hypotheses, advice = [], []
    hypothesis_texts, advice_texts = [], []
    index = 0
    for resource in resources or []:
        rid = f"{resource.meta.id}@{resource.meta.version}"
        for rule in resource.rules:
            ref = f"{rid} {rule.rule_id}"
            if rule.tag == "hypothesis":
                hypotheses.append(ref)
                hypothesis_texts.append(rule.text)
                continue
            if rule.tag in ("guideline", "preference"):
                advice.append(ref)
                advice_texts.append(rule.text)
                continue
            if rule.rule_id in realized_rule_ids:
                continue  # 已由任务实例条目落实（来源在其 source_refs 中）
            index += 1
            # 硬约束规则：可映射到现有工具的给确定性检查，否则如实标不可验证
            if "溢出" in rule.text:
                method, spec = "deterministic", {"kind": SPEC_RENDER_OVERFLOW}
            elif "表单" in rule.text and ("必填" in rule.text or "校验" in rule.text):
                method, spec = "deterministic", {"kind": SPEC_FORM_FIELDS}
            elif "完整、准确呈现" in rule.text:
                method, spec = "deterministic", {}  # 由任务实例事实条目落实（见 T1 来源）
            else:
                method, spec = "unverifiable", {}
            entries.append(PlanEntry(
                entry_id=f"D{index}", name=f"领域硬约束 {rule.rule_id}",
                meaning=rule.text,
                category="hard_requirement", source_layer="domain",
                source_refs=[ref, f"rule-nature:{rule.tag}"],
                observation=_rule_observation(rule.text), check_method=method,
                pass_line=rule.text.split("：", 1)[-1][:80],
                blocking=blocking_rule("hard_requirement", method, False),
                priority=1, check_spec=spec,
                disposition="当前无对应测量工具时如实标注未验证，不阻塞交付、不宣称通过",
                note=f"资源状态 pending_human（草案）；{rule.rule_id} 原文以资源文件为准",
            ))
    if advice:
        entries.append(PlanEntry(
            entry_id="D-A", name="领域专业建议与偏好（聚合）",
            meaning="已加载资源中的 guideline/preference 规则；提供理由，允许替代方案",
            category="professional_advice", source_layer="domain",
            source_refs=advice,
            observation="产物观察与用户反馈", check_method="user_judgment",
            pass_line="作为专业依据呈现，不阻塞交付；用户选定后转为保留项",
            blocking=False, priority=3,
            disposition="等待用户反馈或专业评审引用；不自动修改",
            search_text="；".join(advice_texts),
        ))
    if hypotheses:
        entries.append(PlanEntry(
            entry_id="D-H", name="待验证假设（聚合）",
            meaning="资源中标记 hypothesis 的规则：待验证假设，未验证前不作为修改依据",
            category="hypothesis", source_layer="domain",
            source_refs=hypotheses,
            observation="后续案例观察（FC 类反馈模式验证）", check_method="unverifiable",
            pass_line="不阻塞交付，也不作为修改依据（ADR-008 第 2 条）",
            blocking=False, priority=3,
            disposition="仅在获得案例证据后按计划修订程序转为可用标准",
            search_text="；".join(hypothesis_texts),
        ))
    return entries


def _rule_observation(text: str) -> str:
    if "溢出" in text:
        return "双视口渲染测量（溢出 px 与溢出元素）"
    if "表单" in text:
        return "结构观察表单字段与必填属性；提交/确认行为需真实交互，静态观察不可得"
    if "对比度" in text:
        return "配色对比度测量（当前无该测量工具）"
    return "产物观察（当前无自动化核对工具）"


def build_task_instance_entries(goal: str, material_text: str, briefs=(),
                                assessment: dict | None = None) -> list:
    """任务实例层：从任务素材机械推导具体验收项（活动事实、报名表单）。

    P3.4 审计 #1 修复：先做目标—素材主体一致性判定。mismatch 时素材事实
    不注入（T1/T2 不建——素材不是为本目标准备的，自决以目标为准）；另建
    T4 禁现条目：与目标主体不一致的素材内容不得出现在产物中。T3（目标
    主体一致）始终建立，可确定性核对时为 deterministic（诊断候选中的
    目标一致性问题由此路由到自主修订，不再落入 unverifiable）。
    """
    assessment = assessment or assess_material_goal_consistency(goal, material_text)
    verdict = assessment.get("verdict")
    derived = derive_task_facts(material_text, briefs)
    needles, form_fields = derived["needles"], derived["form_fields"]
    entries = []
    if verdict == "mismatch":
        needles, form_fields = {}, []
    if needles:
        missing = [label for label in ("活动名称", "日期时间", "地点", "价格") if label not in needles]
        entries.append(PlanEntry(
            entry_id="T1", name="任务关键事实完整呈现",
            meaning="素材中的关键活动事实在产物中可见且与素材一致；不虚构素材外的事实（忠实于意图的产物面）",
            category="hard_requirement", source_layer="task_instance",
            source_refs=["task-material:input/copy.md（活动素材）"]
            + [f"web-information-hierarchy@0.1-draft IH-1（硬约束：用户明确要求的要素完整、准确呈现）"],
            observation="产物结构观察与正文文本",
            check_method="deterministic",
            pass_line="；".join(f"{k}「{v}」可见" for k, v in needles.items()),
            blocking=True, priority=1,
            check_spec={"kind": SPEC_TEXT_CONTAINS, "needles": dict(needles)},
            note=("素材中未找到的标签：" + "、".join(missing)) if missing else "素材五类事实齐全",
        ))
    if verdict != "mismatch" and form_fields:  # 表单要求跟素材：只有素材报名方式给出具体字段才要求表单
        entries.append(PlanEntry(
            entry_id="T2", name="报名表单可用",
            meaning="素材报名方式要求的表单存在（必填字段齐全）；主要操作在双视口可发现可操作",
            category="hard_requirement", source_layer="task_instance",
            source_refs=["task-material:input/copy.md（报名方式）",
                         "web-information-hierarchy@0.1-draft IH-2",
                         "web-basic-interaction@0.1-draft BI-1"],
            observation="结构观察表单字段；视口可用性由渲染测量在专业评审段核对",
            check_method="deterministic",
            pass_line=("表单字段存在" + (f"（必填：{'、'.join(form_fields)}）" if form_fields else "")
                       + "；双视口无溢出（渲染测量）"),
            blocking=True, priority=1,
            check_spec={"kind": SPEC_FORM_FIELDS, "min_fields": 1,
                        "required_named": list(form_fields)},
            note="必填校验生效、提交确认与不发送真实数据属运行时行为，静态观察不可得→如实标注未验证",
        ))
    goal_subjects = [q for q in assessment.get("goal_subjects", []) if q]
    subject_needles = {f"目标主体「{q}」": q for q in goal_subjects}
    if not subject_needles and verdict == "consistent" \
            and assessment.get("material_subject") \
            and assessment["material_subject"] in (goal or ""):
        subject_needles = {"目标主体": assessment["material_subject"]}
    if subject_needles:
        entries.append(PlanEntry(
            entry_id="T3", name="目标主体一致",
            meaning="任务目标中的主体内容（产品/活动名）在产物中呈现；产物主题与目标一致（审计 20261010 #1）",
            category="hard_requirement", source_layer="task_instance",
            source_refs=["goal:评估目标原文（引用主体）"],
            observation="产物结构观察与正文文本",
            check_method="deterministic",
            pass_line="；".join(f"{k}可见" for k in subject_needles),
            blocking=True, priority=1,
            check_spec={"kind": SPEC_TEXT_CONTAINS, "needles": dict(subject_needles)},
            note="目标主体词面核对；深层语义一致性由专业评审与用户判断",
        ))
    if verdict == "mismatch":
        forbidden = {"素材主体": assessment.get("material_subject", "")}
        forbidden.update({f"素材{k}": v for k, v in needles.items()} if needles else {})
        # mismatch 时 needles 已清空；用原始推导的素材事实作为禁现内容
        raw = derive_task_facts(material_text, briefs)["needles"]
        forbidden.update({f"素材{k}": v for k, v in raw.items()})
        forbidden = {k: v for k, v in forbidden.items() if v}
        entries.append(PlanEntry(
            entry_id="T4", name="不一致素材内容不出现",
            meaning="与目标主体不一致的素材内容（其主体、日期、价格等事实）不出现在产物中（审计 20261010 #1）",
            category="hard_requirement", source_layer="task_instance",
            source_refs=["material-consistency:" + assessment.get("rule", "")],
            observation="产物结构观察与正文文本",
            check_method="deterministic",
            pass_line="；".join(f"{k}「{v}」不出现" for k, v in forbidden.items()),
            blocking=True, priority=1,
            check_spec={"kind": SPEC_TEXT_ABSENT, "needles": dict(forbidden)},
            note=("素材与目标主体不一致（" + assessment.get("rule", "")
                  + "）：素材事实不注入委托，且其内容不得进入产物"),
        ))
    return entries


# ---- 模型实例化（领域规则 → 本任务可检查条目） ----

_INSTANTIATE_SYSTEM = (
    "你是网页任务的评估计划实例化助手。只组装、实例化已有标准，不发明新标准。"
    "只能从给定的领域规则（引用规则编号）与用户/brief/素材原话（引用原文摘录）中产出"
    "本任务可检查的评估条目；引用了给定材料之外来源的条目会被拒收。"
    "标准写结果，不写实现：写“价格在移动端首屏可见”，不写字号或位置。"
    "最多 %d 条。输出 JSON：{\"entries\":[{\"name\",\"meaning\",\"category\","
    "\"source_refs\":[…],\"observation\",\"check_method\",\"pass_line\",\"disposition\"}]}。"
    "category ∈ hard_requirement/professional_advice/user_preference/hypothesis；"
    "check_method ∈ deterministic/pairwise_blind/user_judgment/unverifiable。"
    "当前可用的自动观察只有：页面结构文本、双视口渲染测量（溢出/裁剪）、双视口截图"
    "（截图只能配合模型判断，不能算确定性检查）；没有对比度测量、点击与提交测试。"
    "你给出的条目一律是暂定的（provisional），不会阻塞交付；请如实给 check_method，"
    "不要为了显得可查而写 deterministic。" % MODEL_ENTRY_LIMIT
)


def _instantiate_user_prompt(goal, brief, material_text, resources) -> str:
    rules = []
    for resource in resources or []:
        rid = f"{resource.meta.id}@{resource.meta.version}"
        for rule in resource.rules:
            rules.append(f"- {rid} {rule.rule_id} [{rule.tag}] {rule.text[:100]}")
    brief_items = [f"- [{i.provenance.value}:{i.source_ref}] {i.text[:80]}"
                   for i in (brief.constraints or []) + (brief.preferences or [])]
    material = (material_text or "")[:1200]
    return (
        f"[目标] {goal}\n\n[已加载领域规则（唯一可引用的规则来源）]\n"
        + ("\n".join(rules) or "（无）")
        + "\n\n[brief 条目（可引用其原话）]\n" + ("\n".join(brief_items) or "（无）")
        + f"\n\n[任务素材摘录（可引用其原话）]\n{material}\n\n"
        + "请产出本任务的评估条目（JSON）。source_refs 每项只能是规则编号（如 IH-4）"
        "或上述原话的连续摘录（≤40 字）。"
    )


def _resolve_source_ref(ref: str, rule_ids: set, quotable_text: str) -> bool:
    ref = (ref or "").strip()
    if not ref:
        return False
    if ref in rule_ids:
        return True
    if ref in quotable_text and len(ref) >= 4:
        return True
    # 允许“IH-4”出现在更长引用里（如“web-information-hierarchy@0.1-draft IH-4”）
    for rid in rule_ids:
        if rid and rid in ref:
            return True
    return False


def _model_disposition(value) -> str:
    """模型条目的处置文本；模型误填标签（provisional/暂定）时回退为默认处置。"""
    text = str(value or "").strip()
    if not text or text.casefold() in ("provisional", "暂定", "temporary"):
        return "暂定条目：不阻塞交付，不单独触发修订"
    return text


def instantiate_domain_rules_with_model(gateway, goal, brief, material_text, resources,
                                        notify=None):
    """调用模型把领域规则实例化为本任务条目；虚构来源拒收并留痕。

    返回 (accepted_entries, meta)。任何失败都退回空条目并如实记录，不抛异常、
    不阻塞任务（降级由调用方标注）。
    """
    meta = {"status": "skipped", "model": "", "usage": {}, "accepted": 0,
            "rejected": [], "note": ""}
    if gateway is None:
        meta["note"] = "未提供模型网关：纯确定性组装"
        return [], meta
    if not gateway.has_credentials():
        meta["status"] = "degraded"
        meta["note"] = "无凭据：退回纯确定性组装"
        return [], meta
    meta["status"] = "degraded"
    meta["model"] = getattr(getattr(gateway, "config", None), "model", "")
    rule_ids = {rule.rule_id for resource in resources or [] for rule in resource.rules}
    quotable = goal + "\n" + "\n".join(
        i.text for i in (brief.constraints or []) + (brief.preferences or [])) \
        + "\n" + (material_text or "")
    prompt = _instantiate_user_prompt(goal, brief, material_text, resources)
    record = gateway.complete(_INSTANTIATE_SYSTEM, prompt, json_mode=True)
    if not record.ok:
        meta["note"] = f"模型调用失败（{record.error[:80]}）：退回纯确定性组装"
        return [], meta
    meta["usage"] = dict(record.usage)
    meta["mock"] = bool(record.mock)
    try:
        payload = json.loads(record.response_text)
    except (json.JSONDecodeError, TypeError) as exc:
        meta["note"] = f"模型响应不是合法 JSON（{type(exc).__name__}）：拒收全部条目"
        return [], meta
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        meta["note"] = "模型响应缺少 entries 数组：拒收全部条目"
        return [], meta
    accepted = []
    for raw in raw_entries[: MODEL_ENTRY_LIMIT * 2]:
        if not isinstance(raw, dict):
            meta["rejected"].append({"entry": str(raw)[:60], "reason": "条目不是对象"})
            continue
        refs = raw.get("source_refs")
        if not isinstance(refs, list) or not refs:
            meta["rejected"].append({"entry": str(raw.get("name", ""))[:60],
                                     "reason": "缺少来源引用"})
            continue
        bad = [r for r in refs if not _resolve_source_ref(str(r), rule_ids, quotable)]
        if bad:
            meta["rejected"].append({"entry": str(raw.get("name", ""))[:60],
                                     "reason": "虚构来源：" + "；".join(map(str, bad))[:80]})
            continue
        category = raw.get("category")
        method = raw.get("check_method")
        if category not in CATEGORIES or method not in CHECK_METHODS:
            meta["rejected"].append({"entry": str(raw.get("name", ""))[:60],
                                     "reason": f"类别或检查方式不合法（{category}/{method}）"})
            continue
        name, meaning, pass_line = (str(raw.get("name") or "").strip(),
                                    str(raw.get("meaning") or "").strip(),
                                    str(raw.get("pass_line") or "").strip())
        if not (name and meaning and pass_line):
            meta["rejected"].append({"entry": name[:40], "reason": "含义/通过线缺失"})
            continue
        if method == "deterministic":
            # 防止模型虚构自动检查能力：确定性建议降级为不可验证并留痕
            method = "unverifiable"
            raw["note"] = "模型建议 deterministic，已降级为 unverifiable（当前无对应自动检查实现）"
        accepted.append(PlanEntry(
            entry_id="", name=name, meaning=meaning, category=category,
            source_layer="domain", source_refs=[str(r) for r in refs],
            observation=str(raw.get("observation") or "产物观察"),
            check_method=method, pass_line=pass_line,
            blocking=False,  # 模型条目一律 provisional，不阻塞交付
            disposition=_model_disposition(raw.get("disposition")),
            priority=2, agent_selected=True, provisional=True,
            derived_by=f"model:{meta['model']}",
            note=str(raw.get("note") or "") or "模型实例化；暂定（provisional）",
        ))
        if len(accepted) >= MODEL_ENTRY_LIMIT:
            break
    meta["accepted"] = len(accepted)
    meta["status"] = "ok" if accepted else "rejected_all"
    if len(accepted) < min(len(raw_entries), MODEL_ENTRY_LIMIT):
        meta["note"] = (meta["note"] + "；" if meta["note"] else "") + \
            f"接受 {len(accepted)}/{min(len(raw_entries), MODEL_ENTRY_LIMIT)} 条（其余拒收留痕）"
    if notify is not None:
        notify(f"模型实例化（{'Mock' if meta.get('mock') else meta['model']}）："
               f"接受 {meta['accepted']} 条，拒收 {len(meta['rejected'])} 条"
               + (f"（{meta['note'][:60]}）" if meta["note"] else ""))
    return accepted, meta


# ---- 计划组装与改版 ----

_TRADEOFFS = [
    "优先级：硬要求（priority 1）阻塞交付并触发预算内修复；专业建议与用户偏好（priority 3）"
    "不阻塞、不自动修改；模型实例化条目（provisional）不阻塞交付。",
    "观察工具缺失的维度如实标 unverifiable，不阻塞交付也不宣称通过；hypothesis 不作为修改依据。",
    "标准写结果不写实现；不为让产物通过而在看到结果后改标准（PROJECT_PLAN §16.4）。",
]
_STOP_CONDITIONS = [
    "硬要求（blocking 条目）全部通过且专业评审无成立问题 → 交付（DELIVERED=初稿可查看）。",
    "修订造成回归 → 不交付该修订版（修订前快照保留）。",
    "自主修订预算耗尽仍有成立问题 → 如实交付并留档遗留，不冒充已解决。",
    "执行器状态 unknown → 停止等待人工核对，不自动重发。",
]
_OUT_OF_SCOPE = [
    "真实点击/表单提交与网络行为（静态观察不可得，运行时行为如实标注未验证）。",
    "部署表现、SEO、无障碍全面审计、跨浏览器兼容。",
    "用户满意度（由用户逐字反馈另行记录，不写入本计划）。",
    "协商式 sprint contract（执行器先提案）；盲比使用同源模型，不建立独立性"
    "（V2 起候选路由与盲比复检已实现，见 eval_review.py）。",
]


def _next_entry_id(layer: str, existing: list) -> str:
    prefix = {"core": "K", "user": "U", "domain": "D", "task_instance": "T"}[layer]
    used = [e.entry_id for e in existing if e.entry_id.startswith(prefix)]
    numbers = []
    for uid in used:
        match = re.fullmatch(prefix + r"(\d+)", uid)
        if match:
            numbers.append(int(match.group(1)))
    return f"{prefix}{(max(numbers) if numbers else 0) + 1}"


def build_evaluation_plan(goal, brief, material_text, resources=(), gateway=None,
                          use_model=False, notify=None, version=1) -> EvaluationPlan:
    """组装评估计划（四层来源）；use_model 且有凭据时叠加模型实例化条目。"""
    entries = []
    entries.extend(build_core_entries())
    entries.extend(build_user_entries(brief))
    assessment = assess_material_goal_consistency(goal, material_text)
    entries.extend(build_task_instance_entries(goal, material_text, [brief],
                                               assessment=assessment))
    # 领域硬约束中已被任务实例条目引用落实的规则不再单列（保持计划短）
    realized = set()
    for e in entries:
        for ref in e.source_refs:
            realized.update(re.findall(r"\b[A-Z]{2}-\d+\b", ref))
    entries.extend(build_domain_entries(resources, realized_rule_ids=realized))
    for e in entries:
        if e.category == "hypothesis":
            e.blocking = False
    model_entries, model_meta = [], {"status": "skipped", "model": "", "usage": {},
                                     "rejected": [], "note": "未启用模型或无凭据：纯确定性组装"}
    if use_model and gateway is not None:
        model_entries, model_meta = instantiate_domain_rules_with_model(
            gateway, goal, brief, material_text, resources, notify=notify)
    for e in model_entries:
        e.entry_id = _next_entry_id("domain", entries)
        entries.append(e)
    entries.sort(key=lambda e: (e.priority, e.entry_id))
    return EvaluationPlan(
        material_assessment=assessment,
        schema_version=PLAN_SCHEMA, plan_id="plan-" + uuid.uuid4().hex[:12],
        version=version, brief_version=brief.version, goal=goal,
        created_at=datetime.now().isoformat(timespec="seconds"),
        entries=entries,
        priorities_note=f"试点目标量级 5—10 维；本计划 {len(entries)} 条（内核/用户/领域/任务实例"
                        "四层来源，逐条可追溯）。",
        tradeoffs=list(_TRADEOFFS), stop_conditions=list(_STOP_CONDITIONS),
        out_of_scope=list(_OUT_OF_SCOPE),
        model_instantiation=model_meta,
    )


def revise_plan(previous: EvaluationPlan, brief, material_text, reason_kind: str,
                detail: str, resources=(), gateway=None, use_model=False,
                notify=None) -> EvaluationPlan:
    """有原因地改版：目标变化/brief 改版、标准本身有错、发现新盲区（ADR-008 第 5 条）。

    旧版本文件不改写；受影响条目在新版本更新并在 revision.affected 中列明。
    """
    if reason_kind not in REVISION_REASONS:
        raise ValueError(f"计划修订原因不合法：{reason_kind}（允许 {REVISION_REASONS}）")
    new_plan = build_evaluation_plan(
        goal=brief.objective if reason_kind == "goal_change" else previous.goal,
        brief=brief, material_text=material_text, resources=resources,
        gateway=gateway, use_model=use_model, notify=notify,
        version=previous.version + 1)
    old_needles = previous.fact_needles()
    new_needles = new_plan.fact_needles()
    affected = [f"{k}: {old_needles.get(k, '（新增）')}→{v}" for k, v in new_needles.items()
                if old_needles.get(k) != v]
    new_plan.revision = {
        "reason_kind": reason_kind, "detail": detail,
        "supersedes_version": previous.version,
        "affected_entries": affected,
    }
    new_plan.supersedes_sha256 = previous.sha256
    return new_plan


# ---- 落盘与恢复 ----

def load_latest_plan(run_dir) -> "EvaluationPlan | None":
    """从运行目录恢复最新版本化计划；哈希按当前文件字节重算，不改写任何版本。"""
    plan_dir = Path(run_dir) / "evaluation-plan"
    plans = []
    if plan_dir.is_dir():
        for path in sorted(plan_dir.glob("plan-v*.json")):
            try:
                plan = EvaluationPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
                plan.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                plans.append(plan)
            except (OSError, ValueError, TypeError, KeyError):
                continue  # 损坏的计划文件跳过，不留痕改写
    return max(plans, key=lambda p: p.version) if plans else None


# ---- 视图 ----

def acceptance_block(plan: EvaluationPlan, skip_entry_ids: tuple = ()) -> str:
    """从计划派生的本轮验收项（写入执行器指令；执行器可见）。

    skip_entry_ids：不适用条目（如素材无报名要求时的表单类条目）不列入——
    列入会驱动执行器实现素材未要求的表单（P3.4 审计 #1 的真实观察）。
    """
    lines = [f"[本轮验收项（来自评估计划 v{plan.version}，"
             f"SHA-256 前 12 位 {(plan.sha256 or '—')[:12]}）]"]
    for e in plan.blocking_entries():
        if e.entry_id in skip_entry_ids:
            lines.append(f"- {e.entry_id} {e.name}：本轮不适用（素材未要求），不需要实现")
            continue
        lines.append(f"- {e.entry_id} {e.name}：{e.pass_line}（检查：{e.observation[:40]}）")
    lines.append("（以上为交付前将逐项核对的标准；无法静态核验的行为类要求仍须实现，"
                 "如必填校验与本地确认提示。素材中没有的事实不要新增。）")
    return "\n".join(lines) + "\n"


def render_plan_markdown(plan: EvaluationPlan) -> str:
    lines = [f"# 评估计划 v{plan.version}（{plan.plan_id}）", ""]
    lines.append(f"- 目标：{plan.goal}（brief v{plan.brief_version}）")
    lines.append(f"- schema：{plan.schema_version}；创建于 {plan.created_at}")
    if plan.sha256:
        lines.append(f"- SHA-256：`{plan.sha256}`")
    if plan.revision:
        lines.append(f"- 改版原因：{plan.revision.get('reason_kind')}"
                     f"（替代 v{plan.revision.get('supersedes_version')}）："
                     f"{plan.revision.get('detail')}")
        if plan.revision.get("affected_entries"):
            lines.append(f"- 受影响条目：{'；'.join(plan.revision['affected_entries'])}")
    lines.append("")
    lines.append("## 维度")
    for e in plan.entries:
        lines.append(
            f"- **{e.entry_id} {e.name}**（{CATEGORY_LABEL[e.category]}｜"
            f"{CHECK_METHOD_LABEL[e.check_method]}｜{'阻塞' if e.blocking else '不阻塞'}｜"
            f"优先级 {e.priority}）")
        lines.append(f"  - 含义：{e.meaning}")
        lines.append(f"  - 来源（{e.source_layer}）：{'；'.join(e.source_refs)}")
        lines.append(f"  - 观察：{e.observation}")
        lines.append(f"  - 通过线：{e.pass_line}")
        lines.append(f"  - 无法检查时：{e.disposition or '（不适用，当前可查）'}")
        if e.note:
            lines.append(f"  - 注：{e.note}")
        tags = []
        if e.agent_selected:
            tags.append("agent_selected")
        if e.provisional:
            tags.append("provisional（不阻塞）")
        if tags:
            lines.append(f"  - 标注：{'、'.join(tags)}")
    lines.append("")
    lines.append("## 优先级与取舍")
    lines.append(f"- {plan.priorities_note}")
    lines.extend(f"- {t}" for t in plan.tradeoffs)
    lines.append("")
    lines.append("## 停止条件")
    lines.extend(f"- {s}" for s in plan.stop_conditions)
    lines.append("")
    lines.append("## 不评估的范围")
    lines.extend(f"- {o}" for o in plan.out_of_scope)
    lines.append("")
    mi = plan.model_instantiation
    lines.append("## 模型实例化")
    lines.append(f"- 状态：{mi.get('status')}；模型：{mi.get('model') or '（未用）'}；"
                 f"用量：{mi.get('usage') or '（无）'}")
    if mi.get("mock"):
        lines.append("- **Mock 调用（测试/演示），不是真实模型响应**")
    if mi.get("note"):
        lines.append(f"- 注：{mi['note']}")
    for rejected in mi.get("rejected", []):
        lines.append(f"- 拒收：{rejected.get('entry')}——{rejected.get('reason')}")
    return "\n".join(lines) + "\n"


# ---- 确定性检查执行（collab.hard_requirement_review 调用） ----

_EXTERNAL_REF_RE = re.compile(
    r"""(?:src|href|action)\s*=\s*["'](?:https?:)?//[^"']*["']|https?://[^\s"'<>]+""",
    re.IGNORECASE)


def find_external_refs(html_text: str) -> list:
    """静态找出产物中的外部引用（http/https/协议相对地址），供无网络约束核对。"""
    if not html_text:
        return []
    hits = []
    for match in _EXTERNAL_REF_RE.finditer(html_text):
        fragment = match.group(0)
        if "例" in fragment or fragment.startswith("https://docs.") or "w3.org" in fragment:
            continue  # doctype/命名空间等非网络引用不计
        hits.append(fragment[:80])
    return hits


def execute_structural_check(entry: PlanEntry, text_blob: str, structure_items,
                             html_text: str) -> tuple:
    """执行结构阶段可执行的确定性条目。返回 (findings, blocking_labels, deferred)。"""
    findings, blocking, deferred = [], [], []
    spec = entry.check_spec or {}
    kind = spec.get("kind")
    if kind == SPEC_TEXT_CONTAINS:
        for label, needle in (spec.get("needles") or {}).items():
            if needle and needle in text_blob:
                findings.append(f"{label}：可见（{entry.entry_id}）")
            else:
                findings.append(f"{label}：未在结构/正文文本中发现（{entry.entry_id}）")
                blocking.append(label)
    elif kind == SPEC_TEXT_ABSENT:
        for label, needle in (spec.get("needles") or {}).items():
            if needle and needle in text_blob:
                findings.append(f"{label}「{needle}」出现（与目标主体不一致的素材内容，{entry.entry_id}）")
                blocking.append(label)
            else:
                findings.append(f"{label}「{needle}」未出现（{entry.entry_id}）")
    elif kind == SPEC_FORM_FIELDS:
        inputs = [i for i in structure_items if i.kind in ("input", "textarea")]
        min_fields = int(spec.get("min_fields", 1))
        if len(inputs) >= min_fields:
            required = [i.text for i in inputs if i.extra.get("required") == "yes"]
            findings.append(f"表单字段 {len(inputs)} 个（{entry.entry_id}"
                            + (f"；必填属性 {len(required)} 个：{'、'.join(required[:4])}"
                               if required else "") + "）")
        else:
            findings.append(f"未发现报名表单字段（{entry.entry_id}）")
            blocking.append("报名表单")
    elif kind == SPEC_NO_EXTERNAL_REFS:
        externals = find_external_refs(html_text)
        if externals:
            findings.append(f"发现外部引用：{'；'.join(externals[:3])}（{entry.entry_id}）")
            blocking.append(entry.name[:12])
        else:
            findings.append(f"外部引用：无（{entry.entry_id}）")
    return findings, blocking, deferred
