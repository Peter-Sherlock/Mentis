"""E1 冻结核对与运行输入白名单导出；不运行模型、不证明运行端隔离。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any


_TREATMENTS = {
    "feedback": {"B-1", "B-2", "B-3", "M"},
    "end_to_end": {"B-0", "B-1", "B-2", "M"},
    "decision": {"B-1", "B-2", "B-3", "M"},
}
_RESERVED = {"input.json", "prompt.md", "prepare-report.json", "suite.json", "reviewer.json"}
_ANNOTATION_KEYS = {
    "expected", "expected_behavior", "expected_result", "expected_answer", "gold",
    "gold_answer", "gold_operations", "answer", "answers", "score", "scores",
    "scoring", "scoring_criteria", "rubric", "diagnosis", "root_cause", "label",
    "labels", "author", "author_notes", "reviewer", "reviewer_notes", "case_purpose",
}


@dataclass(frozen=True)
class _File:
    target: str
    content: bytes
    sha256: str


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _fields(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: 必须是 object")
    missing, extra = fields - value.keys(), value.keys() - fields
    if missing or extra:
        raise ValueError(f"{label}: 字段白名单不符，缺少={sorted(missing)}，额外={sorted(extra)}")


def _strings(value: Any, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label}: 必须是字符串数组")
    if nonempty and not value:
        raise ValueError(f"{label}: 不得为空")


def _text(value: Any, label: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{label}: 必须是{'非空' if nonempty else ''}字符串")


def _enum(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{label}: 不支持的取值")


def _path(value: Any, label: str) -> str:
    """限制为无歧义的可移植相对路径，包含 Windows 驱动器和 ADS 检查。"""
    _text(value, label)
    windows = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    if windows.drive or windows.root or PurePosixPath(normalized).is_absolute():
        raise ValueError(f"{label}: 不允许绝对路径或驱动器路径")
    parts = normalized.split("/")
    for part in parts:
        if part in {"", ".", ".."} or part.endswith((".", " ")):
            raise ValueError(f"{label}: 路径逃逸或歧义组件")
        if re.search(r'[<>:"|?*\x00-\x1f]', part):
            raise ValueError(f"{label}: 不允许 Windows 特殊字符或数据流")
        stem = part.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError(f"{label}: 不允许设备名")
    return "/".join(parts)


def _reject_annotations(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold().replace("-", "_")
            if (normalized in _ANNOTATION_KEYS
                    or normalized.startswith(("expected_", "gold_", "author_", "reviewer_", "scoring_"))):
                raise ValueError(f"{label}: 公共策略不得包含作者答案/评分字段 {key}")
            _reject_annotations(child, f"{label}.{key}")
    elif isinstance(value, list):
        for child in value:
            _reject_annotations(child, label)


def _source(base: Path, value: Any, expected_sha: Any, label: str) -> _File:
    relative = _path(value, f"{label}.source")
    if any(part.casefold() in {"reviewer.json", "suite.json", "prepare-report.json"}
           for part in relative.split("/")):
        raise ValueError(f"{label}: 作者/评价端文件不能导出")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
        raise ValueError(f"{label}.sha256: 必须是64位十六进制哈希")
    try:
        candidate = (base / relative).resolve(strict=True)
        candidate.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label}: 源路径不存在或符号链接逃逸") from exc
    if not candidate.is_file():
        raise ValueError(f"{label}: 源必须是普通文件")
    content = candidate.read_bytes()
    actual = _sha(content)
    if actual != expected_sha.lower():
        raise ValueError(f"{label}: sha256 不匹配，冻结源已改变")
    return _File("", content, actual)


def _files(base: Path, entries: Any, label: str) -> list[_File]:
    if not isinstance(entries, list):
        raise ValueError(f"{label}: 必须是数组")
    result = []
    for index, entry in enumerate(entries):
        name = f"{label}[{index}]"
        _fields(entry, {"source", "target", "sha256"}, name)
        target = _path(entry["target"], f"{name}.target")
        if any(part.casefold() in _RESERVED for part in target.split("/")):
            raise ValueError(f"{name}: target 与保留文件碰撞")
        source = _source(base, entry["source"], entry["sha256"], name)
        result.append(_File(target, source.content, source.sha256))
    _unique_targets(result, label)
    return result


def _unique_targets(files: list[_File], label: str) -> None:
    targets = []
    for file in files:
        target = file.target.casefold()
        if any(target == old or target.startswith(old + "/") or old.startswith(target + "/")
               for old in targets):
            raise ValueError(f"{label}: target 重复或文件/目录碰撞: {file.target}")
        targets.append(target)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 数值: {value}")


def _validate_suite(suite_path: Path) -> tuple[dict, str, list[_File], dict[str, _File], list[dict]]:
    suite_path = Path(suite_path)
    if suite_path.is_symlink():
        raise ValueError("suite.json 不得是符号链接")
    suite_path = suite_path.resolve(strict=True)
    raw = suite_path.read_bytes()
    try:
        suite = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys,
                           parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"suite.json 无法解析: {exc}") from exc
    _fields(suite, {"schema_version", "suite_id", "status", "common", "samples", "treatments", "prompts", "isolation"}, "suite")
    if type(suite["schema_version"]) is not int or suite["schema_version"] != 1:
        raise ValueError("suite.schema_version: 仅支持 1")
    _text(suite["suite_id"], "suite.suite_id")
    if suite["status"] != "exploratory_frozen":
        raise ValueError("suite.status: 本工具仅导出 exploratory_frozen 开发集")
    _fields(suite["common"], {"files", "policy"}, "suite.common")
    if not isinstance(suite["common"]["policy"], dict):
        raise ValueError("suite.common.policy: 必须是显式公共 object")
    _reject_annotations(suite["common"]["policy"], "suite.common.policy")
    common = _files(suite_path.parent, suite["common"]["files"], "suite.common.files")
    _fields(suite["treatments"], set(_TREATMENTS), "suite.treatments")
    for kind, expected in _TREATMENTS.items():
        actual = suite["treatments"][kind]
        _strings(actual, f"suite.treatments.{kind}", nonempty=True)
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError(f"suite.treatments.{kind}: 对照组不符合冻结配置")
    arms = set().union(*_TREATMENTS.values())
    _fields(suite["prompts"], arms, "suite.prompts")
    prompts = {}
    for arm, prompt in suite["prompts"].items():
        _fields(prompt, {"source", "sha256"}, f"suite.prompts.{arm}")
        prompts[arm] = _source(suite_path.parent, prompt["source"], prompt["sha256"], f"suite.prompts.{arm}")
    _fields(suite["isolation"], {"status", "note"}, "suite.isolation")
    _text(suite["isolation"]["status"], "suite.isolation.status")
    _text(suite["isolation"]["note"], "suite.isolation.note")
    if not isinstance(suite["samples"], list) or not suite["samples"]:
        raise ValueError("suite.samples: 必须是非空数组")
    samples, seen = [], set()
    for index, sample in enumerate(suite["samples"]):
        label = f"suite.samples[{index}]"
        _fields(sample, {"sample_id", "kind", "review_status", "input", "files", "observations_missing"}, label)
        _text(sample["sample_id"], f"{label}.sample_id")
        identity = sample["sample_id"].casefold()
        if identity in seen:
            raise ValueError(f"{label}: sample_id 重复")
        seen.add(identity)
        _enum(sample["kind"], set(_TREATMENTS), f"{label}.kind")
        _enum(sample["review_status"], {"pending_human", "owner_reviewed", "independent_reviewed"}, f"{label}.review_status")
        _fields(sample["input"], {"user_goal", "user_feedback", "feedback_source", "conversation_context"}, f"{label}.input")
        public = sample["input"]
        _text(public["user_goal"], f"{label}.input.user_goal")
        _text(public["user_feedback"], f"{label}.input.user_feedback", nonempty=False)
        _strings(public["conversation_context"], f"{label}.input.conversation_context")
        _enum(public["feedback_source"], {"none", "simulated", "human_quote"}, f"{label}.input.feedback_source")
        if (public["feedback_source"] == "none") != (public["user_feedback"] == ""):
            raise ValueError(f"{label}.input: 反馈原文与来源不一致")
        _strings(sample["observations_missing"], f"{label}.observations_missing")
        own_files = _files(suite_path.parent, sample["files"], f"{label}.files")
        combined = common + own_files
        _unique_targets(combined, f"{label}.files+common")
        if any(identity in file.target.casefold() for file in combined):
            raise ValueError(f"{label}: target 不得携带 sample_id")
        samples.append({"sample": sample, "files": combined})
    return suite, _sha(raw), common, prompts, samples


def _audit(suite: dict, suite_sha: str, common: list[_File], prompts: dict[str, _File], samples: list[dict]) -> dict:
    pending = [row["sample"]["sample_id"] for row in samples
               if row["sample"]["review_status"] != "independent_reviewed"]
    exploratory_blocking = ["运行中的 Mentis 与执行器读不到作者答案尚未实测；同用户目录分离、提示词禁读和清洁导出均不构成隔离证明。"]
    formal_limits = ["正式主指标与数值阈值尚未冻结；本清单只冻结开发输入及组别配置。"]
    if pending:
        formal_limits.append("样本尚未独立人工复核，只可用于探索，不支持专业效果结论: " + ", ".join(pending))
    missing = [row["sample"]["sample_id"] for row in samples
               if any(reason != "initial_artifact_not_applicable"
                      or row["sample"]["kind"] not in {"end_to_end", "decision"}
                      for reason in row["sample"]["observations_missing"])]
    if missing:
        formal_limits.append("观察存在显式缺测，不能据此宣称完整质量已核验: " + ", ".join(missing))
    return {
        "status": "prepared_not_ready", "suite_id": suite["suite_id"], "suite_sha256": suite_sha,
        "sample_count": len(samples),
        "checks": [
            {"name": "schema_and_input_whitelist", "passed": True},
            {"name": "source_paths_and_targets", "passed": True},
            {"name": "frozen_source_hashes", "passed": True,
             "file_count": len(common) + len(prompts) + sum(len(row["sample"]["files"]) for row in samples)},
            {"name": "runtime_answer_isolation", "passed": False,
             "declared_status": suite["isolation"]["status"], "note": suite["isolation"]["note"]},
            {"name": "declared_independent_human_review", "passed": not pending},
        ],
        "exploratory_blocking_reasons": exploratory_blocking,
        "formal_claim_limits": formal_limits,
        "blocking_reasons": exploratory_blocking + formal_limits,
        "scope": "只核对清单、路径与冻结字节；不实现真实运行 runner，不调用模型，不证明运行端隔离。未人工复核草案允许用于已授权探索；正式效果声明受 formal_claim_limits 限制。",
    }


def audit_suite(suite_path: Path) -> dict:
    """只读核对冻结源；无有效隔离实测时绝不返回 ready。"""
    try:
        return _audit(*_validate_suite(suite_path))
    except (ValueError, OSError, RuntimeError) as exc:
        return {"status": "invalid", "checks": [{"name": "frozen_suite_validation", "passed": False}],
                "blocking_reasons": [str(exc)]}


def prepare_suite(suite_path: Path, out_dir: Path) -> dict:
    """从明确白名单的已校验字节制作每个组的独立输入包，拒绝覆盖。"""
    suite, suite_sha, common, prompts, samples = _validate_suite(suite_path)
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or getattr(out_dir, "is_junction", lambda: False)():
        raise ValueError("out_dir 不得是符号链接或 junction")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise FileExistsError(f"out_dir 已有内容，拒绝覆盖: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = _audit(suite, suite_sha, common, prompts, samples)
    report["packages"] = []
    report["report_is_evaluation_only"] = "prepare-report.json 包含样本身份，只能由评价端持有；运行端只挂载单个组目录。"
    for index, row in enumerate(samples, start=1):
        sample, files = row["sample"], row["files"]
        neutral = f"input-{index:03d}"
        input_bytes = _json_bytes({"input": sample["input"], "policy": suite["common"]["policy"],
                                   "observations_missing": sample["observations_missing"]})
        shared = [_File("input.json", input_bytes, _sha(input_bytes))] + files
        inventory = sorted(({"target": file.target, "sha256": file.sha256} for file in shared),
                           key=lambda item: item["target"])
        fingerprint = _sha(_json_bytes(inventory))
        arm_records = []
        for arm in suite["treatments"][sample["kind"]]:
            package = out_dir / neutral / arm
            for file in shared + [_File("prompt.md", prompts[arm].content, prompts[arm].sha256)]:
                target = package / file.target
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(file.content)
            arm_records.append({"treatment": arm, "path": f"{neutral}/{arm}",
                                "shared_fingerprint_sha256": fingerprint,
                                "prompt_sha256": prompts[arm].sha256})
        report["packages"].append({"sample_id": sample["sample_id"], "kind": sample["kind"],
                                   "neutral_id": neutral, "shared_files": inventory, "arms": arm_records})
    with (out_dir / "prepare-report.json").open("xb") as stream:
        stream.write(_json_bytes(report))
    return report
