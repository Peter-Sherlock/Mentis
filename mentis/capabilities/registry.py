"""资源注册表与 Markdown 资源加载。

文件格式：HTML 注释内的 JSON 元数据 + 固定小节（判断依据/正例/反例/反馈模式）。
规则行格式：- [性质] 编号：文本，性质 ∈ hard_constraint / guideline / preference / hypothesis。
解析失败的行为：跳过无法解析的行，不中断加载；注册表元数据本身解析失败则报错。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RULE_RE = re.compile(r"^\[(\w+)\]\s+([A-Z]{2}-\d+)：(.+)$")
FEEDBACK_RE = re.compile(r"^现象:\s*(.+?)\s*\|\s*检查:\s*(.+?)\s*\|\s*候选:\s*(.+)$")

RULE_TAGS = ("hard_constraint", "guideline", "preference", "hypothesis")

RULE_TAG_LABEL = {
    "hard_constraint": "硬要求",
    "guideline": "建议",
    "preference": "偏好",
    "hypothesis": "待验证假设",
}

META_RE = re.compile(r"<!--\s*mentis-resource\s*(\{.*?\})\s*-->", re.S)


def default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "registry.json"


@dataclass
class Rule:
    rule_id: str
    tag: str  # hard_constraint / guideline / preference / hypothesis
    text: str
    resource_id: str


@dataclass
class FeedbackPattern:
    phenomenon: str
    check: str
    candidates: str


@dataclass
class ResourceMeta:
    id: str
    version: str
    provenance: str
    domain: str
    applies_to: list
    excludes: list
    decision_questions: list
    observation_requirements: list
    file: str


@dataclass
class Resource:
    meta: ResourceMeta
    rules: list = field(default_factory=list)          # list[Rule]
    positive_examples: list = field(default_factory=list)
    negative_examples: list = field(default_factory=list)
    feedback_patterns: list = field(default_factory=list)  # list[FeedbackPattern]
    parse_warnings: list = field(default_factory=list)

    def rules_by_tag(self, tag: str) -> list:
        return [r for r in self.rules if r.tag == tag]


@dataclass
class Registry:
    registry_version: str
    provenance: str
    evidence_note: str
    resources: list              # list[ResourceMeta]
    declared_conflicts: list     # list[{between:[..], note:..}]

    def resource(self, resource_id: str) -> ResourceMeta:
        for r in self.resources:
            if r.id == resource_id:
                return r
        raise KeyError(f"注册表中没有资源 {resource_id}")


def load_registry(path: str | Path | None = None) -> Registry:
    path = Path(path) if path else default_registry_path()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    metas = [
        ResourceMeta(
            id=m["id"],
            version=m["version"],
            provenance=m["provenance"],
            domain=m["domain"],
            applies_to=m.get("applies_to", []),
            excludes=m.get("excludes", []),
            decision_questions=m.get("decision_questions", []),
            observation_requirements=m.get("observation_requirements", []),
            file=m["file"],
        )
        for m in data["resources"]
    ]
    return Registry(
        registry_version=data["registry_version"],
        provenance=data["provenance"],
        evidence_note=data.get("evidence_note", ""),
        resources=metas,
        declared_conflicts=data.get("declared_conflicts", []),
    )


def load_resource(meta: ResourceMeta, registry_path: str | Path | None = None) -> Resource:
    base = Path(registry_path).parent if registry_path else default_registry_path().parent
    text = (base / meta.file).read_text(encoding="utf-8")
    resource = Resource(meta=meta)

    meta_match = META_RE.search(text)
    if meta_match is None:
        resource.parse_warnings.append("缺少 mentis-resource 元数据块")
    elif json.loads(meta_match.group(1)).get("status") != "pending_human":
        resource.parse_warnings.append("资源状态不是 pending_human（解析器按草案处理）")

    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            title = line[3:].strip()
            section = (
                "rules" if title == "判断依据"
                else "positive" if title == "正例"
                else "negative" if title == "反例"
                else "feedback" if title == "反馈模式"
                else None
            )
            continue
        if not line.startswith("- ") or section is None:
            continue
        body = line[2:].strip()
        if section == "rules":
            m = RULE_RE.match(body)
            if m and m.group(1) in RULE_TAGS:
                resource.rules.append(Rule(m.group(2), m.group(1), m.group(3), meta.id))
            elif body:
                resource.parse_warnings.append(f"未解析的规则行: {body[:40]}")
        elif section == "positive":
            resource.positive_examples.append(body)
        elif section == "negative":
            resource.negative_examples.append(body)
        elif section == "feedback":
            m = FEEDBACK_RE.match(body)
            if m:
                resource.feedback_patterns.append(FeedbackPattern(m.group(1), m.group(2), m.group(3)))
            else:
                resource.parse_warnings.append(f"未解析的反馈模式行: {body[:40]}")
    return resource
