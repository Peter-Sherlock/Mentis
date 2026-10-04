"""E2 运行契约和评价端导出。此模块不读取 reviewer.json，不提供评分答案。"""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC_SCHEMA = "mentis-eval-spec/1"
PROTOCOL = "docs/evaluation/e2/PROTOCOL.md"


class ReadinessError(ValueError):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    """原子替换检查点；历史版本由调用方另存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def source_hashes(root: Path = ROOT) -> dict:
    files = list((root / "mentis").rglob("*.py"))
    files += [p for p in (root / "resources").rglob("*") if p.is_file()]
    # 领域注册表/资料也属于方法；不含测试、答案和运行产物。
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(files)}


def freeze_spec(out: Path, suite: Path, root: Path = ROOT) -> dict:
    """必须在开隔离窗口前调用；运行期不读取被拒读的 Git 元数据。"""
    if out.exists():
        raise ReadinessError(f"拒绝覆盖已有运行契约：{out}")
    def git(*args):
        result = subprocess.run(["git", "-c", "core.excludesfile=", *args], cwd=root,
                                capture_output=True, text=True, check=True, timeout=20)
        return result.stdout.strip()
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ReadinessError("工作树不干净；先提交代码/协议，再冻结运行契约")
    commit = git("rev-parse", "HEAD")
    protocol = root / PROTOCOL
    text = protocol.read_text(encoding="utf-8")
    version = re.search(r"v\d+\.\d+-frozen", text)
    if not version:
        raise ReadinessError("协议缺少冻结版本")
    spec = {"schema": SPEC_SCHEMA, "code_commit": commit,
            "source_hashes": source_hashes(root), "suite_sha256": sha(suite),
            "protocol_path": PROTOCOL, "protocol_version": version.group(),
            "protocol_sha256": sha(protocol),
            "created_at": datetime.now(timezone.utc).isoformat()}
    write_json(out, spec)
    return spec


def validate_spec(path: Path, suite_sha: str = "", root: Path = ROOT) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReadinessError(f"运行契约不可读：{type(exc).__name__}") from exc
    if (spec.get("schema") != SPEC_SCHEMA
            or not re.fullmatch(r"[0-9a-f]{40}", spec.get("code_commit", ""))):
        raise ReadinessError("运行契约 schema/commit 无效")
    if spec.get("source_hashes") != source_hashes(root):
        raise ReadinessError("当前方法代码/领域资源与冻结 commit 的字节清单不一致")
    if spec.get("protocol_path") != PROTOCOL or spec.get("protocol_sha256") != sha(root / PROTOCOL):
        raise ReadinessError("协议与冻结运行契约不一致")
    if suite_sha and spec.get("suite_sha256") != suite_sha:
        raise ReadinessError("套件哈希与运行契约不一致")
    return spec


def binding(path: Path, suite_sha: str = "") -> dict:
    spec = validate_spec(path, suite_sha)
    return {"spec_sha256": sha(path), "code_commit": spec["code_commit"],
            "protocol_version": spec["protocol_version"],
            "protocol_sha256": spec["protocol_sha256"]}


_FACT = re.compile(r"^-\s*([^：:]+)[：:]\s*(.+)$", re.MULTILINE)
_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def material_state(workspace: Path, context: list[str], answers: list[dict], write_effective=True) -> dict:
    """仅从运行组公开素材/用户上下文解析事实；保留原文件和来源。

    当前决定案例契约只处理标注化日期冲突。其它事实冲突保持待决。
    """
    sources = sorted((workspace / "input").glob("copy*.md"))
    sources = [p for p in sources if p.name != "copy-effective.md"]
    texts = {p.relative_to(workspace).as_posix(): p.read_text(encoding="utf-8") for p in sources}
    facts = {}
    for path, text in texts.items():
        for key, value in _FACT.findall(text):
            facts.setdefault(key.strip(), []).append({"path": path, "value": value.strip()})
    conflicts = {k: v for k, v in facts.items() if len({x["value"] for x in v}) > 1}
    effective = next(iter(texts.values()), "")
    decisions, pending = [], []
    supplied = [("conversation_context", s) for s in context]
    supplied += [(a.get("source", "controlled_simulation"), a["answer"]) for a in answers]
    for key, values in conflicts.items():
        resolved = None
        if "日期" in key:
            for source, statement in supplied:
                dates = _DATE.findall(statement)
                if len(set(dates)) == 1 and ("确认" in statement or "为准" in statement):
                    selected = [v for v in values if dates[0] in v["value"]]
                    if len(selected) == 1:
                        resolved = (source, statement, selected[0]["value"])
        if resolved:
            source, statement, value = resolved
            effective = re.sub(r"(^-\s*" + re.escape(key) + r"[：:]\s*).+$",
                               lambda m: m[1] + value, effective, flags=re.MULTILINE)
            decisions.append({"field": key, "source": source, "statement": statement, "value": value})
        else:
            pending.append({"field": key, "alternatives": values})
    path = ""
    if not pending and effective:
        target = workspace / "input" / "copy-effective.md"
        if write_effective:
            target.write_text(effective, encoding="utf-8")
        path = target.relative_to(workspace).as_posix()
    return {"material": effective, "effective_path": path, "sources": list(texts),
            "source_hashes": {p: sha(workspace / p) for p in texts},
            "conflicts": conflicts, "decisions": decisions, "pending": pending}


def workspace_hashes(workspace: Path) -> dict:
    """核对前后完整作品快照，排除 Git 元数据；拒绝链接越出作品目录。"""
    result = {}
    for p in sorted(workspace.rglob("*")):
        relative = p.relative_to(workspace)
        if ".git" in relative.parts:
            continue
        if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
            raise ReadinessError(f"工作区含链接，不能核对续办：{relative}")
        if p.is_file():
            result[relative.as_posix()] = sha(p)
    return result


def summarize_runs(runs: Path, suite: Path, out: Path) -> dict:
    """评价侧：全部20臂、缺失项、执行与作品分列；盲评只导出作品。"""
    if out.exists():
        raise ReadinessError(f"拒绝覆盖已有评价导出：{out}")
    suite_doc = json.loads(suite.read_text(encoding="utf-8"))
    suite_hash = sha(suite)
    manifests = []
    for p in sorted(runs.glob("*/run-manifest.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("suite_sha256") != suite_hash:
            raise ReadinessError(f"汇总发现不同套件：{p}")
        manifests.append((p, doc))
    rows, mapping, guards = [], [], []
    out.mkdir(parents=True)
    (out / "blind-artifacts").mkdir()
    for i, sample in enumerate(suite_doc["samples"], 1):
        for treatment in suite_doc["treatments"][sample["kind"]]:
            arm = f"input-{i:03d}/{treatment}"
            matches = [(p, m) for p, m in manifests if m["arm"] == arm]
            for p, m in matches:
                for c in m.get("summary", {}).get("candidates", []):
                    guards.append({"run_id": m["run_id"], "arm": arm, **c,
                                   "human_should_adopt": None, "reviewer": "", "basis": ""})
            # 重跑记录都保留；不能静默挑选最好的一次。
            for p, m in matches or [(None, None)]:
                summary = m.get("summary", {}) if m else {}
                row = {"sample": sample["sample_id"], "kind": sample["kind"], "arm": arm,
                       "run_id": m["run_id"] if m else "", "status": "recorded" if m else "missing",
                       "execution_status": m.get("execution_status", "unknown") if m else "",
                       "data_role": ("e2_candidate" if m and m.get("execution_binding")
                                     and m.get("evidence_mode") == "live_local"
                                     else "infrastructure_only" if m else "missing"),
                       "method_verdict": summary.get("verdict") or "" if m else "",
                       "artifact_exists": bool(m and m.get("artifact", {}).get("exists")),
                       "blind_id": "", "human_quality": "", "human_improvement": "pending_human",
                       "attempts": m["budget"]["used"]["attempts"] if m else "",
                       "model_units": m["budget"]["used"]["model_calls"] if m else "",
                       "model_requests": m.get("model_requests", {}).get("total", "") if m else "",
                       "http_attempts": m.get("model_requests", {}).get("http_attempts", "") if m else "",
                       "code_commit": m.get("execution_binding", {}).get("code_commit", "") if m else "",
                       "manifest": str(p.resolve()) if p else ""}
                if row["artifact_exists"]:
                    artifact = Path(m["artifact"]["path"])
                    if sha(artifact) != m["artifact"]["sha256"]:
                        raise ReadinessError(f"作品与 manifest 哈希不一致：{artifact}")
                    blind_id = "work-" + secrets.token_hex(6)
                    shutil.copyfile(artifact, out / "blind-artifacts" / f"{blind_id}.html")
                    row["blind_id"] = blind_id
                    material_files = [f for f in sample["files"] if f["target"].startswith("input/")]
                    task_card = {"goal": sample["input"]["user_goal"],
                                 "public_materials": [
                                     (suite.parent / f["source"]).read_text(encoding="utf-8") for f in material_files],
                                 "public_context": sample["input"].get("conversation_context", [])}
                    # 卡片只有共同的公开目标/素材；不给方法、预算、问题数、内部版本链。
                    write_json(out / "blind-artifacts" / f"{blind_id}-task.json", task_card)
                    mapping.append({"blind_id": blind_id, "run_id": row["run_id"],
                                    "arm": arm, "artifact_sha256": sha(artifact),
                                    "source_label_warning": any(label in artifact.read_text(encoding="utf-8")
                                                                 for label in (m["run_id"], "Mentis", "B-1", "B-2", "B-3"))})
                rows.append(row)
    secrets.SystemRandom().shuffle(mapping)
    write_json(out / "private-map.json", {"evaluation_only": True, "items": mapping})
    write_json(out / "guard-review.json", {"review_status": "pending_human", "candidates": guards,
               "rate": None, "note": "人工补 human_should_adopt 后重算；缺标签不当作零误伤。"})
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (out / "comparison.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    # 复核者仅获得这个目录；名称/顺序与私有映射、trace及预算分开。
    (out / "blind-artifacts" / "order.txt").write_text(
        "\n".join(x["blind_id"] + ".html" for x in mapping) + "\n", encoding="utf-8")
    report = {"expected_arms": sum(len(suite_doc["treatments"][s["kind"]]) for s in suite_doc["samples"]),
              "recorded_arms": len({r["arm"] for r in rows if r["status"] == "recorded"}),
              "missing_arms": [r["arm"] for r in rows if r["status"] == "missing"],
              "runs": len(manifests), "review_status": "pending_human", "quality_conclusion": None}
    report["e2_candidate_arms"] = len({r["arm"] for r in rows if r["data_role"] == "e2_candidate"})
    report["method_bindings"] = sorted({r["code_commit"] for r in rows if r["code_commit"]})
    report["mixed_method_versions"] = len(report["method_bindings"]) > 1
    write_json(out / "summary.json", report)
    return report


def guard_rates(review: Path) -> dict:
    doc = json.loads(review.read_text(encoding="utf-8"))
    candidates = doc["candidates"]
    eligible = [c for c in candidates if c.get("decision") in
                {"reject", "pending", "plan_blind_spot", "user_option", "skip_unverifiable"}]
    labeled = [c for c in eligible if type(c.get("human_should_adopt")) is bool
               and c.get("reviewer") and c.get("basis")]
    overflow = [c for c in labeled if c.get("guard") == "overflow"]
    mapping = [c for c in labeled if c.get("decision") in
               {"plan_blind_spot", "user_option", "skip_unverifiable"}]
    return {"eligible": len(eligible), "labeled": len(labeled),
            "missing_labels": len(eligible) - len(labeled),
            "false_suppression_rate": sum(c["human_should_adopt"] for c in labeled) / len(labeled) if labeled else None,
            "overflow_labeled": len(overflow),
            "overflow_false_suppression_rate": sum(c["human_should_adopt"] for c in overflow) / len(overflow) if overflow else None,
            "mapping_labeled": len(mapping),
            "mapping_false_suppression_rate": sum(c["human_should_adopt"] for c in mapping) / len(mapping) if mapping else None,
            "labels_complete": len(labeled) == len(eligible)}
