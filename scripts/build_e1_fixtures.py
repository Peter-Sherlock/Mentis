"""Build the small E1 development suite from a frozen real artifact.

The source is committed with provenance in README. Controlled variants are
author-created fixtures, not Mentis results. This script makes no model calls.
Run from the repository: py -3.14 scripts/build_e1_fixtures.py [--capture]
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SUITE = ROOT / "docs/evaluation/e1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_entry(path, target):
    return {"source": path.relative_to(SUITE).as_posix(), "target": target, "sha256": sha(path)}


def observe(page, capture):
    from mentis.observation import observe_structure
    out = page.parent / "observation"
    out.mkdir(exist_ok=True)
    structure = asdict(observe_structure(str(page)))
    structure["source_path"] = "workspace/index.html"
    write_json(out / "structure.json", structure)
    files = [file_entry(out / "structure.json", "observation/structure.json")]
    missing = ["contrast_sampling", "dead_link_execution", "real_network_capture",
               "full_click_and_async_interaction", "independent_quality_review"]
    if capture:
        from mentis.browser_observation import capture_local_html
        from mentis.render_measurement import measure_page
        scratch = ROOT / "runtime/evaluation/e1-observation" / page.parent.name
        scratch.mkdir(parents=True, exist_ok=True)
        browser = capture_local_html(page, scratch)
        for view in browser.views:
            if view.error or not view.screenshot_path:
                raise ValueError(f"Screenshot failed: {view.name}: {view.error}")
            dest = out / f"{view.name}.png"
            shutil.copyfile(view.screenshot_path, dest)
            files.append(file_entry(dest, f"observation/{view.name}.png"))
        measurement = asdict(measure_page(page, scratch))
        measurement["source_path"] = "workspace/index.html"
        measurement["browser"] = Path(measurement["browser"]).name
        for view in measurement["viewports"]:
            if view["errors"] or view["inner_width"] != view["width"]:
                raise ValueError(f"Measurement unverified: {view['name']}: {view['errors']}")
            view["measurement_copy"] = "observer_injected_copy"
        write_json(out / "measurement.json", measurement)
        files.append(file_entry(out / "measurement.json", "observation/measurement.json"))
    else:
        # Never silently regenerate old screenshots for changed page bytes.
        missing.extend(["screenshots", "render_measurement"])
    return files, missing


def build(capture):
    base = (SUITE / "source/index.html").read_text(encoding="utf-8")
    facts = (SUITE / "source/copy.md").read_text(encoding="utf-8")
    shared = SUITE / "shared"
    shared.mkdir(exist_ok=True)
    shared_files = []
    for src in sorted((ROOT / "resources/web_pilot").glob("*.md")):
        # Remove author case labels from shared professional material. Preserve
        # criteria and their draft/hypothesis status; expose equally to all arms.
        text = src.read_text(encoding="utf-8")
        text = re.sub(r"FC-\d+(?:a|b)?", "领域示例", text)
        text = text.replace("预埋成因", "示例现象")
        dest = shared / src.name
        dest.write_text(text, encoding="utf-8")
        shared_files.append(file_entry(dest, f"resources/{dest.name}"))
    requirements = shared / "requirements.md"
    requirements.write_text(
        "# 本地活动页公开要求\n\n"
        "活动事实以提供的资料及用户明确决定为准，不编造事实。\n"
        "单文件中文页面；桌面1280×800与移动390×844可用，无横向溢出。\n"
        "报名姓名、联系方式必填；空提交有提示，填写后有确认；不得发送或保存真实数据。\n"
        "站内跳转目标存在；有意义的图片有替代文本。正文对比度抽检至少4.5:1。\n"
        "这些要求不能证明完整无障碍合规或审美满意。未执行的检查记未验证。\n"
        "视觉与布局可在授权内自主选择。重要事实冲突需查证，资料不能解决时再请求用户。\n",
        encoding="utf-8")
    shared_files.append(file_entry(requirements, "input/requirements.md"))
    policy = {
        "execution_scope": "只在本地隔离工作区制作和检查，不发布，不发送真实数据",
        "delegated_decisions": ["字体", "配色", "布局", "授权内普通问题修复"],
        "reserved_decisions": ["资料无法解决的重要活动事实", "新的外部写入许可"],
        "max_auto_revisions": 2, "max_executor_attempts": 3,
        "max_guidance_model_calls": 4, "per_turn_timeout_s": 600,
        "available_tools": ["local_files", "shell", "browser_screenshots", "render_measurement"],
        "user_availability": "常规设计不回复；必要事实请求由受控模拟协议提供相同回答",
        "network": "模型和执行器认证调用除外；作品不得访问网络或发送数据",
        "token_and_currency_limit": "unsupported；记录用量，不宣称硬封顶",
        "prompt_rules": "保留正常工具、自检、预算内追问与后续修订；不得只限制某个基线为单次调用"
    }
    samples = []

    def add(sid, kind, goal, feedback="", context=None, html=None, attachments=None):
        folder = SUITE / "samples" / sid
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        missing = []
        if html is not None:
            page = folder / "index.html"
            if html == base:
                page.write_bytes((SUITE / "source/index.html").read_bytes())
            else:
                page.write_text(html, encoding="utf-8")
            paths.append(file_entry(page, "workspace/index.html"))
            obs_files, missing = observe(page, capture)
            paths.extend(obs_files)
        else:
            missing = ["initial_artifact_not_applicable"]
        for name, content in (attachments or {"copy.md": facts}).items():
            dest = folder / name
            dest.write_text(content, encoding="utf-8")
            paths.append(file_entry(dest, f"input/{name}"))
        samples.append({
            "sample_id": sid, "kind": kind, "review_status": "pending_human",
            "input": {"user_goal": goal, "user_feedback": feedback,
                      "feedback_source": "simulated" if feedback else "none",
                      "conversation_context": context or []},
            "files": paths, "observations_missing": missing})

    goal = ("用附件里的完整活动资料做一个温暖、简洁的本地活动页，方便附近居民了解和报名。"
            "视觉和布局你来定，按提供的双视口和表单要求完成。首稿后最多自动修订两轮，"
            "不发布、不发送数据。一般设计细节不用问我，完成后给我预览和检查结果。")
    add("E1-01", "end_to_end", goal)
    assert "¥99" in base
    price_variant = base.replace("¥99", "费用待补充")
    add("E1-02", "feedback", goal,
        "页面好像少了点报名信息。整体布局和配色别变，帮我检查一下。",
        html=price_variant)
    add("E1-03", "feedback", goal,
        "请再检查一下这版，已有活动事实和现在的视觉风格都保留。",
        html=base)
    # Facts conflict is in raw supplied materials, never a defect annotation.
    copy_b = facts.replace("2026-11-14（周六）", "2026-11-21（周六）")
    decision_goal = ("用附件活动资料制作本地活动页，日期和地点必须准确。"
                     "视觉和布局你来定，最多自动修订两轮，不发布、不发送数据。")
    add("E1-04", "decision", decision_goal,
        attachments={"copy-a.md": facts, "copy-b.md": copy_b})
    add("E1-05", "decision", decision_goal,
        context=["活动日期以我最后确认的2026-11-21 14:00—16:00为准，其他事实保持原资料。"],
        attachments={"copy-a.md": facts, "copy-b.md": copy_b})
    prompts = {}
    for label in ["B-0", "B-1", "B-2", "B-3", "M"]:
        file = SUITE / "prompts" / f"{label}.md"
        prompts[label] = {"source": file.relative_to(SUITE).as_posix(), "sha256": sha(file)}
    write_json(SUITE / "suite.json", {
        "schema_version": 1, "suite_id": "e1-dev-v1", "status": "exploratory_frozen",
        "common": {"files": shared_files, "policy": policy}, "samples": samples,
        "treatments": {"feedback": ["B-1", "B-2", "B-3", "M"],
                       "end_to_end": ["B-0", "B-1", "B-2", "M"],
                       "decision": ["B-1", "B-2", "B-3", "M"]},
        "prompts": prompts,
        "isolation": {"status": "unverified", "note": "白名单导出不等于操作系统拒读；E2需独立身份或隔离挂载及真实拒读探针"}})
    print(json.dumps({"samples": len(samples), "arms": 20, "model_calls": 0,
                      "captured_pages": 2 if capture else 0,
                      "suite": str(SUITE / "suite.json")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true", help="真实Chrome采集两份固定首稿；不调用模型")
    build(parser.parse_args().capture)
