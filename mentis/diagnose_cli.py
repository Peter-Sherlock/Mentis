"""P2.2 CLI：对真实页面文件做结构观察 + 模型诊断（有凭据→真实调用；无→明确指引）。

记录保存到 runtime/diagnosis/；调用日志脱敏（不含密钥，只含主机与用量）。
"""

import json
import os
import sys
from datetime import datetime

from mentis.capabilities.registry import load_registry
from mentis.capabilities.selection import select_for_task
from mentis.diagnosis import render_bundle_markdown, run_diagnosis
from mentis.models.gateway import API_KEY_ENV, ModelConfig, ModelGateway
from mentis.observation import observe_structure

SAVE_DIR = os.path.join("runtime", "diagnosis")


def _append_call_log(gateway) -> None:
    os.makedirs(SAVE_DIR, exist_ok=True)
    path = os.path.join(SAVE_DIR, "call_log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for record in gateway.calls:
            f.write(
                json.dumps(
                    {
                        "request_id": record.request_id,
                        "model": record.model,
                        "host": record.host,
                        "ok": record.ok,
                        "mock": record.mock,
                        "latency_ms": record.latency_ms,
                        "usage": record.usage,
                        "prompt_sha256": record.prompt_sha256,
                        "error": record.error,
                        "created_at": record.created_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def run_diagnose(args) -> int:
    page = args.page
    if not os.path.exists(page):
        print(f"页面文件不存在：{page}")
        return 2

    observation = observe_structure(page)
    registry = load_registry()
    context, _ = select_for_task(
        registry,
        task_label=f"页面诊断：{page}",
        task_keywords=["单页活动介绍页"],
        decision_needs=["信息密度", "主操作层级", "表单校验行为", "配色气质"],
        observation_capabilities={"structure_only": True},
        can_execute=False,
    )

    gateway = ModelGateway(config=ModelConfig())
    if args.fixture:
        with open(args.fixture, "r", encoding="utf-8") as f:
            fixture_text = f.read()
        gateway._transport = lambda system, prompt, json_mode: fixture_text
        gateway.force_mock = True
    elif not gateway.has_credentials():
        print("【无法执行真实调用】")
        print(f"缺少凭据：请设置环境变量 {API_KEY_ENV}，或在项目根目录 .env 中设置同名键。")
        print("环境变量优先；.env 已被 Git 忽略。凭据不写日志。")
        print("真实调用还需显式加 --real；单次命令最多两次请求，每次输出上限 2000 token。")
        print("发送内容包含选定页面的结构文本、目标、反馈和领域规则，请先核对数据范围。")
        print("离线验证可使用：python -m mentis diagnose --page <页面> --fixture tests/fixtures/mock_diagnosis_response.json")
        return 2
    elif not args.real:
        print("【未发送数据】真实调用需要显式加 --real。")
        print("将发送页面结构、目标、反馈和领域规则至 api.deepseek.com；单次最多两次请求。")
        return 2

    bundle = run_diagnosis(
        gateway,
        goal=args.goal or f"诊断页面 {page}",
        user_feedback=args.feedback or "",
        observation=observation,
        context=context,
    )
    _append_call_log(gateway)

    os.makedirs(SAVE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(SAVE_DIR, f"diagnosis_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_bundle_markdown(bundle))
    json_path = os.path.join(SAVE_DIR, f"diagnosis_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "goal": bundle.goal,
                "mock": bundle.mock,
                "model": bundle.model,
                "observation_source": bundle.observation_source,
                "valid_diagnoses": [d.__dict__ for d in bundle.valid_diagnoses],
                "invalid_diagnoses": bundle.invalid_diagnoses,
                "valid_decision_requests": [d.__dict__ for d in bundle.valid_decision_requests],
                "invalid_decision_requests": bundle.invalid_decision_requests,
                "next_action": bundle.next_action,
                "next_action_valid": bundle.next_action_valid,
                "errors": bundle.errors,
                "call_ids": bundle.call_ids,
                "usage": bundle.usage,
                "created_at": bundle.created_at,
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=list,
        )

    print(render_bundle_markdown(bundle))
    print(f"[已保存] {md_path}")
    print(f"[已保存] {json_path}")
    print(f"[调用日志] {os.path.join(SAVE_DIR, 'call_log.jsonl')}（脱敏：不含密钥）")
    if bundle.errors:
        return 1
    return 0
