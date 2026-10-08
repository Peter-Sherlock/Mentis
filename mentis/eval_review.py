"""P2.4-V2 可验证性路由与盲比复检（ADR-008 第 6—8 条）。

- 诊断候选先对应到评估计划的维度，按该维度的检查方式路由：
  deterministic / pairwise_blind → 可自主修订；user_judgment → 非阻塞选项告知
  用户（不自动修改）；unverifiable → 不修订；对应不到任何维度 → 计划盲区，
  不自动采纳（可按计划修订程序提出，不自行动）。
- 模型复检改为盲比：前后两版观察随机标为 X/Y，只给计划中的标准，不给诊断
  理由与修改指令；正反两种顺序各判一次，两次不一致记 unknown。
- 判定分级六值：improved / changed_unverified / no_change / regression /
  unverified / unknown。improved 需确定性证据或盲比两序一致；同源盲比降低
  确认偏差，但不建立独立性（PROJECT_PLAN §16.5）。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

ROUTE_ADOPT = "adopt"                      # 确定性/盲比维度：可自主修订
ROUTE_USER_OPTION = "user_option"          # 用户判断维度：非阻塞选项告知用户
ROUTE_SKIP_UNVERIFIABLE = "skip_unverifiable"
ROUTE_BLIND_SPOT = "plan_blind_spot"

VERDICT_IMPROVED = "improved"
VERDICT_CHANGED_UNVERIFIED = "changed_unverified"
VERDICT_NO_CHANGE = "no_change"
VERDICT_REGRESSION = "regression"
VERDICT_UNVERIFIED = "unverified"
VERDICT_UNKNOWN = "unknown"

VERDICT_LABEL = {
    VERDICT_IMPROVED: "改善（有确定性证据或盲比两序一致）",
    VERDICT_CHANGED_UNVERIFIED: "已修改但效果未验证",
    VERDICT_NO_CHANGE: "无正向变化",
    VERDICT_REGRESSION: "回归",
    VERDICT_UNVERIFIED: "观察不可信",
    VERDICT_UNKNOWN: "无法判断",
}

_ENTRY_ID_RE = re.compile(r"\b([KTUD]-[AH]|\b[KTUD]\d{1,2})\b")
# 中文按二元组切分，ASCII 词按整词；用于候选↔维度词面重叠（确定性、可审计）
_ASCII_RE = re.compile(r"[A-Za-z0-9]+")


def _bigrams(text: str) -> set:
    text = re.sub(r"\s+", "", text)
    grams = set()
    ascii_words = _ASCII_RE.findall(text)
    for word in ascii_words:
        grams.add(word.lower())
    cleaned = _ASCII_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", "", cleaned)
    for i in range(len(cleaned) - 1):
        grams.add(cleaned[i:i + 2])
    return grams


def map_candidate_to_dimension(candidate_text: str, plan,
                               min_shared: int = 3, min_score: float = 0.12):
    """候选 → 计划维度的确定性映射；返回 (entry|None, evidence dict)。

    显式引用维度 id（如 "T1"、"D-A"）直接命中；否则按词面重叠
    （二元组/ASCII 词）取最高分维度，需同时满足共享数与覆盖率下限。
    映射是启发式——命中与得分都写入路由记录，供审计与误差观察。
    """
    text = candidate_text or ""
    explicit = sorted({m for m in _ENTRY_ID_RE.findall(text)}, key=len, reverse=True)
    for entry in plan.entries:
        eid = entry.entry_id
        if any(exp == eid for exp in explicit):
            return entry, {"matched_by": "explicit_id", "score": 1.0,
                           "shared": [eid], "explicit": explicit}
    best, best_score, best_shared = None, 0.0, []
    cand_grams = _bigrams(text)
    if not cand_grams:
        return None, {"matched_by": "none", "score": 0.0, "shared": []}
    for entry in plan.entries:
        grams = _bigrams(plan.search_blob(entry))
        shared = cand_grams & grams
        score = len(shared) / max(1, len(cand_grams))
        if score > best_score or (score == best_score and best is not None
                                  and len(shared) > len(best_shared)):
            best, best_score, best_shared = entry, score, sorted(shared)[:8]
    if best is not None and best_score >= min_score and len(best_shared) >= min_shared:
        return best, {"matched_by": "keyword_overlap", "score": round(best_score, 3),
                      "shared": best_shared}
    return None, {"matched_by": "none", "score": round(best_score, 3), "shared": best_shared}


def route_by_verifiability(entry) -> tuple:
    """按维度检查方式给出路由决定；返回 (decision, basis)。"""
    if entry is None:
        return ROUTE_BLIND_SPOT, "对应不到任何计划维度：计划盲区，不自动采纳（ADR-008 第 6 条）"
    if entry.check_method in ("deterministic", "pairwise_blind"):
        return ROUTE_ADOPT, (
            f"对应维度 {entry.entry_id}（检查方式 {entry.check_method}）：可自主修订；"
            "修订后按计划复检")
    if entry.check_method == "user_judgment":
        return ROUTE_USER_OPTION, (
            f"对应维度 {entry.entry_id}（检查方式 user_judgment）：审美/偏好类由用户判断——"
            "以非阻塞选项告知，不自动修改；触及保留决定仍走 request_decision")
    return ROUTE_SKIP_UNVERIFIABLE, (
        f"对应维度 {entry.entry_id}（检查方式 unverifiable）：当前无法验证——"
        "不触发修订，也不据此宣称改善")


# ---- 盲比复检 ----

_BLIND_SYSTEM = (
    "你是修改效果的盲比复查者。给你同一个网页两个版本的观察记录与截图，随机标为"
    " 版本X / 版本Y；你不知道哪个是修改前、哪个是修改后，也不要猜测。"
    "只依据给定的评估标准逐条判断哪个版本更符合标准。"
    "只输出 JSON：{\"overall\": \"X_better|Y_better|equal\", "
    "\"dimensions\": [{\"entry_id\": …, \"verdict\": \"X_better|Y_better|equal|cannot_judge\", "
    "\"reason\": 一句话}]}。entry_id 只能使用给定标准中的编号；"
    "看不清或标准无法从给定材料判断时用 cannot_judge，不要臆断。"
)


def _render_version_text(pack, version_label: str, image_names: list) -> str:
    parts = [pack.structure.render_text(), "", pack.measurement.render_text()]
    if image_names:
        parts.append(f"[版本{version_label} 图像输入] 本次盲比同时收到该版本以下截图"
                     "（真实视口渲染）：" + "、".join(image_names))
    else:
        parts.append(f"[版本{version_label} 图像输入] 本版本无截图；视觉判断不可用，"
                     "不得声称看过画面")
    return "\n".join(parts)


def _version_images(images: list, version_label: str) -> list:
    return [(f"版本{version_label}-{name}", blob) for name, blob in (images or [])]


def _standards_block(plan) -> tuple:
    entries = plan.blind_review_standards()
    lines = [f"- {e.entry_id} {e.name}（{e.check_method}）：{e.pass_line}"
             for e in entries]
    return entries, "\n".join(lines)


def _parse_blind_response(text: str, valid_ids: set) -> tuple:
    """解析盲比响应；返回 (parsed, dropped_ids)——虚构维度编号剔除并留痕。"""
    data = json.loads(text)
    overall = data.get("overall")
    if overall not in ("X_better", "Y_better", "equal"):
        raise ValueError(f"overall 非法：{overall}")
    dims, dropped = {}, []
    for row in data.get("dimensions", []):
        if not isinstance(row, dict):
            continue
        eid = str(row.get("entry_id", ""))
        verdict = row.get("verdict")
        if eid not in valid_ids:
            if eid:
                dropped.append(eid)
            continue  # 虚构维度编号剔除并留痕
        if verdict not in ("X_better", "Y_better", "equal", "cannot_judge"):
            dropped.append(eid)
            continue
        dims[eid] = {"verdict": verdict, "reason": str(row.get("reason", ""))[:120]}
    return {"overall": overall, "dimensions": dims}, dropped


def run_blind_review(gateway, plan, before_pack, before_images, after_pack, after_images,
                     out_path, use_model: bool = True, notify=None, rng=None,
                     before_html_sha256: str = "", after_html_sha256: str = "",
                     professional: dict | None = None) -> dict:
    """前后两版盲比：随机 A/B、只给计划标准、正反两序各判一次。

    返回 dict（落盘 out_path）：assignment（随机指派，可审计）、两序原始判定、
    逐维度正反一致性（不一致→该维度 unknown）、overall 与 usage/mock 标注。
    模型调用失败或输出非法 → overall="unknown"（不判改善）。同源模型：盲比
    降低确认偏差，不建立独立性。
    C1：professional 只接收配置版本/哈希/本阶段职责的**关联信息**（写入结果
    JSON 供追溯）；盲比输入仍然只有计划标准与观察——不接收诊断结论、候选或
    配置内容（大纲 §3：保持盲评隔离）。
    """
    def _notify(message):
        if notify is not None:
            notify(message)

    standards, standards_block = _standards_block(plan)
    valid_ids = {e.entry_id for e in standards}
    result = {
        "kind": "blind-pairwise-review/1",
        "reviewed_before_sha256": before_html_sha256 or (before_pack.sha256 if before_pack else ""),
        "reviewed_after_sha256": after_html_sha256 or (after_pack.sha256 if after_pack else ""),
        "standards": [{"entry_id": e.entry_id, "name": e.name, "pass_line": e.pass_line,
                       "check_method": e.check_method} for e in standards],
        "orders": {}, "per_dimension": {}, "consistent": False,
        "overall": VERDICT_UNKNOWN, "note": "", "mock": False,
        "usage_total": {}, "dropped_refs": [],
    }
    if isinstance(professional, dict) and professional.get("version"):
        # 仅关联信息；不含配置内容（隔离边界见 docstring）
        result["professional_config"] = {
            "version": professional.get("version"),
            "sha256": professional.get("sha256", ""),
            "stage_responsibility": professional.get("stage_responsibility", ""),
        }
    if before_pack is None or after_pack is None:
        result["note"] = "观察记录缺失（修改前/后至少一版无观察包）：无法盲比，记 unknown"
        _write_blind(out_path, result)
        _notify(result["note"])
        return result
    if not use_model or gateway is None or not getattr(gateway, "has_credentials", lambda: True)():
        result["note"] = "模型不可用：盲比未执行，按未验证处理（不判改善）"
        result["overall"] = VERDICT_UNKNOWN
        _write_blind(out_path, result)
        _notify(result["note"])
        return result
    rng = rng or random.Random()
    before_first = rng.random() < 0.5  # 随机决定第一序中 before 扮演 X 还是 Y
    order_specs = []
    if before_first:
        order_specs.append(("order1", "before", "after"))
        order_specs.append(("order2", "after", "before"))
    else:
        order_specs.append(("order1", "after", "before"))
        order_specs.append(("order2", "before", "after"))
    usages, mock_flags, dropped = [], [], []
    for order_name, x_source, y_source in order_specs:
        x_pack, x_images = ((before_pack, before_images) if x_source == "before"
                            else (after_pack, after_images))
        y_pack, y_images = ((before_pack, before_images) if y_source == "before"
                            else (after_pack, after_images))
        x_img_names = [n for n, _ in _version_images(x_images, "X")]
        y_img_names = [n for n, _ in _version_images(y_images, "Y")]
        user_prompt = (
            f"[评估标准（来自评估计划 v{plan.version}，逐条判断）]\n{standards_block}\n\n"
            f"[版本X 观察记录]\n{_render_version_text(x_pack, 'X', x_img_names)}\n\n"
            f"[版本Y 观察记录]\n{_render_version_text(y_pack, 'Y', y_img_names)}\n\n"
            "[输出要求] 只输出 JSON 对象；entry_id 只能取标准清单中的编号。"
        )
        images = _version_images(x_images, "X") + _version_images(y_images, "Y")
        record = (gateway.complete(_BLIND_SYSTEM, user_prompt, images=images)
                  if images else gateway.complete(_BLIND_SYSTEM, user_prompt))
        usages.append(dict(record.usage or {}))
        mock_flags.append(bool(record.mock))
        order_result = {"ok": record.ok, "error": record.error[:120],
                        "x_is": x_source, "y_is": y_source,
                        "image_refs": record.image_refs, "raw": ""}
        if not record.ok:
            order_result["error_note"] = "调用失败：本序判定缺失"
        else:
            order_result["raw"] = record.response_text[:4000]
            try:
                parsed, dropped_in_order = _parse_blind_response(record.response_text,
                                                                 valid_ids)
                order_result["overall"] = parsed["overall"]
                order_result["dimensions"] = parsed["dimensions"]
                if dropped_in_order:
                    dropped.extend(dropped_in_order)
                    order_result["dropped_refs"] = dropped_in_order
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                order_result["error_note"] = f"输出非法（{type(exc).__name__}）：本序判定缺失"
        result["orders"][order_name] = order_result
    result["dropped_refs"] = sorted(set(dropped))
    # 用量与 Mock 汇总
    result["mock"] = all(mock_flags) if mock_flags else False
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        result["usage_total"][key] = sum(u.get(key, 0) for u in usages if isinstance(u, dict))
    # 逐维度正反一致化
    dim_verdicts = {}
    orders = result["orders"]
    def _canonical(order_name, value):
        if value == "cannot_judge" or value is None:
            return "unknown"
        if value == "equal":
            return "equal"
        x_is = orders[order_name]["x_is"]
        better_x = value == "X_better"
        better_before = better_x if x_is == "before" else not better_x
        return "before_better" if better_before else "after_better"
    all_ids = set(valid_ids)
    for eid in sorted(all_ids):
        v1 = orders.get("order1", {}).get("dimensions", {}).get(eid, {}).get("verdict")
        v2 = orders.get("order2", {}).get("dimensions", {}).get(eid, {}).get("verdict")
        c1 = _canonical("order1", v1) if orders.get("order1") else "unknown"
        c2 = _canonical("order2", v2) if orders.get("order2") else "unknown"
        if "unknown" in (c1, c2):
            final = "unknown"
        elif c1 == c2:
            final = c1
        else:
            final = "unknown"  # 两序不一致：不判
        dim_verdicts[eid] = final
    result["per_dimension"] = dim_verdicts
    # 总体判定：两序 overall 正反一致化后比较
    o1 = orders.get("order1", {}).get("overall")
    o2 = orders.get("order2", {}).get("overall")
    if o1 and o2:
        c1 = _canonical("order1", o1)
        c2 = _canonical("order2", o2)
        if c1 == c2:
            result["consistent"] = True
            result["overall"] = {"after_better": "after_better", "before_better": "before_better",
                                 "equal": "equal"}[c1]
        else:
            result["consistent"] = False
            result["overall"] = "inconsistent"
            result["note"] = "两序总体结论不一致：记 unknown（不判改善，ADR-008 第 7 条）"
    else:
        result["consistent"] = False
        result["overall"] = "unknown"
        result["note"] = (result["note"] or "") + "至少一序判定缺失（调用失败/输出非法）"
    _write_blind(out_path, result)
    _notify(f"盲比复检（{'Mock' if result['mock'] else '真实模型'}）："
            f"两序{'一致' if result['consistent'] else '不一致'}，"
            f"总体 {result['overall']}；用量 {result['usage_total']}")
    return result


def _write_blind(out_path, result: dict) -> None:
    try:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except OSError:
        pass  # 落盘失败不改变判定；原始判定仍在返回值与事件摘要中


# ---- 判定分级（ADR-008 第 8 条） ----

def grade_revision(regression: list, observation_unknown: list, artifact_changed: bool,
                   blind: dict | None, targeted_dimensions: list | None = None) -> tuple:
    """修订轮判定 → (六值判定, 依据说明)。

    优先级：回归 > 观察不可信 > 未修改 > 盲比/确定性证据 > changed_unverified / unknown。
    improved 仅当无回归、观察可信且盲比两序一致判 after_better（确定性证据由
    deterministic_checks 单独承载，作为 improved 的必要非充分条件之一）。
    """
    if regression:
        return VERDICT_REGRESSION, "复检发现保留项/必填回归：" + "；".join(regression)[:120]
    if observation_unknown:
        return VERDICT_UNVERIFIED, "观察不可信（测量报错/缺截图/缺记录）：" + \
            "；".join(observation_unknown)[:120]
    if not artifact_changed:
        return VERDICT_NO_CHANGE, "产物未发生变化"
    overall = (blind or {}).get("overall")
    consistent = (blind or {}).get("consistent", False)
    dims = (blind or {}).get("per_dimension", {})
    targeted = targeted_dimensions or []
    if overall == "after_better" and consistent:
        return VERDICT_IMPROVED, "盲比两序一致：修改后版本更符合计划标准"
    if overall == "before_better" and consistent:
        return VERDICT_NO_CHANGE, "盲比两序一致：修改未带来正向变化（保留项完好，未判回归）"
    if overall == "equal" and consistent:
        return VERDICT_NO_CHANGE, "盲比两序一致：两版相当（无正向变化）"
    if overall == "inconsistent":
        return VERDICT_CHANGED_UNVERIFIED, "盲比两序结论不一致：改了，但效果未验证（不判改善）"
    if any(dims.get(t) == "after_better" for t in targeted):
        return VERDICT_IMPROVED, "目标维度盲比两序一致改善"
    if any(dims.get(t) == "unknown" for t in targeted):
        return VERDICT_CHANGED_UNVERIFIED, "目标维度盲比无法判定：效果未验证"
    return VERDICT_UNKNOWN, "盲比结果缺失或无法解释（调用失败/输出非法）"
