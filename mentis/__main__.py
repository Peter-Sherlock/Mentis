"""Mentis 开发入口。

P2.2 起含真实模型诊断命令（需 DEEPSEEK_API_KEY，可由环境或项目 .env 提供）；其余为离线演示与验证。
"""

import argparse
import json
import platform
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mentis import STAGE, __version__

BANNER = """\
Mentis v{version} — 开发入口（当前：{stage}）
真实模型命令需要 DEEPSEEK_API_KEY（环境变量或项目 .env）；演示与离线验证无需凭据。
命令：
  python -m mentis                本说明
  python -m mentis --version      版本号
  python -m mentis demo-contract  最小任务与反馈契约演示（脚本化数据，含拒绝路径）
  python -m mentis demo-routing   首领域资源按需加载与缺口分流演示（确定性规则）
  python -m mentis diagnose       真实模型页面诊断（结构观察→候选→校验；--fixture 为 Mock 模式）
  python -m mentis observe-page   本地 HTML 双视口截图（P3.1，静态初始画面）
  python -m mentis collab         默认自主委托的协作运行（真实执行器+截图；--design 为共同设计）
  python -m mentis collab-resume  恢复运行：回答请求 / 反馈修订 / 改目标 / 撤回
  python -m mentis guide-revise   P3.2 指导修订循环：冻结首稿→观察测量→诊断→指令→修订→复检
  python -m mentis guide-resume   回答诊断提出的用户决定后继续修订
  python -m mentis field-start    P5.2a 真实使用会话：自然语言目标→委托→三入口→使用记录
  python -m mentis field-feedback 查看作品后补记逐字反馈（绑定该运行的会话记录）
  python -m mentis trace           P2.4 事后回放 / --follow 实时旁观（只读）/ --export 导出白名单时间线
  python -m mentis conversation    本地交流视图：确切委托、Codex 公开回复与动作摘要（只读）
  python -m mentis replay          P2.4-O1 只读运行投影：决策/模型调用/版本证据关联（CLI 与后续页面共用）
  python -m mentis status          P2.4-O2 最后可靠状态：阶段、等待、生产者活动、预算（只读）
  python -m mentis view            本地只读流程页（时间线、决策、交流、作品证据；--run 选择运行）
  python -m mentis workbench      P3.4 本地工作台：浏览器内发起与跟进任务（默认端口 8874）
  python -m mentis eval-check      E1 冻结清单核对（离线；--isolation-evidence 接入真实隔离准入）
  python -m mentis eval-prepare    E1 按白名单制作各组相同输入包（离线；拒绝覆盖）
  python -m mentis eval-isolate    E1 答案读隔离：probe 开窗双端探测 / release 解除 / check 准入
  python -m mentis eval-plan       P2.4-V1 评估计划先行：组装并打印某目标的评估计划（--real 调模型）
  python -m mentis eval-run        E1 公平运行器：隔离窗口内运行单个臂（M 或基线组；需先 eval-isolate probe）
路线与状态见 docs/DEVELOPMENT_STATUS.md。
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mentis", description="Mentis 开发入口")
    parser.add_argument("--version", action="version", version=f"mentis {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo-contract", help="P1.2 契约演示（脚本化数据）")
    sub.add_parser("demo-routing", help="P2.1 资源选择与缺口分流演示（确定性规则）")
    diag = sub.add_parser("diagnose", help="P2.2 模型诊断（真实调用需凭据；--fixture 为 Mock）")
    diag.add_argument("--page", required=True, help="要诊断的 HTML 页面文件路径")
    diag.add_argument("--goal", default="", help="本轮目标（可留空）")
    diag.add_argument("--feedback", default="", help="用户反馈原话（可留空）")
    diag.add_argument("--fixture", default="", help="Mock 固定响应文件（离线验证模式）")
    diag.add_argument("--real", action="store_true", help="明确允许本次调用将页面结构、目标和反馈发送给 DeepSeek；最多两次请求")
    observe = sub.add_parser("observe-page", help="本地 HTML 双视口截图；不执行点击或提交")
    observe.add_argument("--page", required=True, help="本地 HTML 文件路径")
    observe.add_argument("--out", default="", help="输出目录；默认 runtime/observations/<运行编号>")
    col = sub.add_parser("collab", help="P2.3 协作运行：目标→自主委托→产物→观察（需 Codex 登录态）")
    col.add_argument("--goal", required=True, help="本轮网页目标")
    col.add_argument("--design", action="store_true", help="共同设计模式（先确认风格方向；默认自主）")
    col.add_argument("--direction", default="", help="直接给出风格方向（跳过方向提问）")
    col.add_argument("--non-interactive", action="store_true", help="不等待输入：待决事项保持未决")
    col.add_argument("--max-repairs", type=int, default=1, help="自动修复轮数上限（默认 1）")
    col.add_argument("--timeout", type=int, default=420, help="单次执行器超时秒数（1—600）")
    res = sub.add_parser("collab-resume", help="恢复协作运行：回答/反馈/改目标/撤回")
    res.add_argument("--state", required=True, help="runtime/collab/<run>/state.json")
    res.add_argument("--answer", action="append", default=[], help="<请求ID>:<回答>，可多次")
    res.add_argument("--feedback", default="", help="用户修正（进入新 brief 版本并续接修订）")
    res.add_argument("--goal-new", default="", help="改变目标（旧版本结果不再适用）")
    res.add_argument("--revoke", action="store_true", help="撤回授权（后续委托全部拒绝）")
    res.add_argument("--confirm-outcome", default="", choices=["", "completed", "failed"],
                     help="unknown 的人工核对确认（须先自行核对执行器与产物）")
    res.add_argument("--non-interactive", action="store_true", help="不等待输入")
    guide = sub.add_parser("guide-revise", help="P3.2 指导修订循环（真实运行需 --real）")
    guide.add_argument("--baseline", required=True, help="P2.3 运行目录（含 workspace/index.html）")
    guide.add_argument("--real", action="store_true",
                       help="明确允许：发送结构/测量/截图至 DeepSeek，并调用本机 Codex 修订")
    guide.add_argument("--max-rounds", type=int, default=2, help="自动修改轮数上限（默认 2）")
    guide.add_argument("--timeout", type=int, default=600, help="单次执行器超时秒数（1—600）")
    guide.add_argument("--no-images", action="store_true", help="诊断不发送截图（仅结构与测量）")
    guide.add_argument("--run-id", default="", help="指定运行目录 ID（默认时间戳）")
    gres = sub.add_parser("guide-resume", help="回答诊断请求的用户决定后续办，或核对后续办失败轮")
    gres.add_argument("--state", required=True, help="runtime/guidance/<run>/state.json")
    gres.add_argument("--answer", action="append", default=[], help="<请求ID>:<回答>，可多次")
    gres.add_argument("--retry", action="store_true",
                      help="核对后续办失败轮（确认无活跃写入者；计入修订预算）")
    gres.add_argument("--reverify", action="store_true",
                      help="用修正后的确定性工具对已有 v2 重新复检（透明更正，不重新委托）")
    fstart = sub.add_parser("field-start", help="P5.2a 真实使用会话（自然语言目标→委托→三入口→使用记录）")
    fstart.add_argument("--goal", required=True, help="用你自己的话描述目标（自然语言，不做问卷）")
    fstart.add_argument("--design", action="store_true", help="共同设计模式（先确认风格方向；默认自主）")
    fstart.add_argument("--direction", default="", help="直接给出风格方向（跳过方向提问）")
    fstart.add_argument("--non-interactive", action="store_true", help="不等待输入：待决保持未决")
    fstart.add_argument("--timeout", type=int, default=420, help="单次执行器超时秒数（1—600）")
    fstart.add_argument("--max-repairs", type=int, default=1, help="自动修复轮数上限（默认 1）")
    fstart.add_argument("--label", default="", help="会话标签（便于你识别）")
    ffb = sub.add_parser("field-feedback", help="查看作品后补记逐字反馈（P5.2a）")
    ffb.add_argument("--run", required=True, help="运行目录（runtime/collab/<运行ID> 或 runtime/guidance/<运行ID>）")
    ffb.add_argument("--satisfied", required=True, choices=["yes", "no"], help="整体满意：yes / no")
    ffb.add_argument("--quote", required=True, help="你的原话（逐字记录，一句话即可）")
    ffb.add_argument("--block", default="", help="被卡住/打断/不明白的地方（原话，可选）")
    ffb.add_argument("--operation", default="", help="你做了什么（可选，默认'查看作品与报告后提交'）")
    ffb.add_argument("--label", default="", help="会话标签（可选）")
    tr = sub.add_parser("trace", help="事后回放（默认）/ --follow 实时旁观 / --export 导出白名单时间线")
    tr.add_argument("--run", required=True,
                    help="运行目录（runtime/collab/<ID> 或 runtime/guidance/<ID>）")
    tr.add_argument("--follow", action="store_true",
                    help="T2 实时旁观：重放已有事件后跟随新增；运行收尾后自动结束")
    tr.add_argument("--export", nargs="?", const="trace-export.json", default="", metavar="路径",
                    help="T3 导出白名单时间线 JSON（默认 trace-export.json；不含 prompt 原文/"
                         "用户逐字反馈/密钥字段；历史运行导出材料清单+缺口，不回填）")
    conversation = sub.add_parser("conversation", help="本地交流视图（确切指令、公开回复与动作摘要；只读）")
    conversation.add_argument("--run", required=True, help="运行目录（runtime/collab/<ID> 或 runtime/guidance/<ID>）")
    view = sub.add_parser("view", help="本地只读运行页面：时间线、决策、公开交流与作品证据")
    view.add_argument("--run", required=True, help="选定运行目录")
    view.add_argument("--port", type=int, default=8873, help="本机端口，0 表示自动选择")
    workbench = sub.add_parser("workbench",
                               help="P3.4 本地工作台：浏览器内发起任务/跟进度/答复待决/"
                                    "反馈/改目标/核对/撤回与作品对比（动作转发真实 CLI）")
    workbench.add_argument("--port", type=int, default=8874, help="本机端口，0 表示自动选择")
    workbench.add_argument("--runs-dir", default="runtime/collab",
                           help="任务运行目录（默认 runtime/collab）")
    replay = sub.add_parser("replay",
                            help="P2.4-O1 只读运行投影：决策记录、模型调用、版本与证据关联"
                                 "（CLI 与后续页面共用；不重算业务判定、不改写运行目录）")
    replay.add_argument("--run", required=True, help="运行目录（eval-run / collab / guidance）")
    replay.add_argument("--out", default="", help="同时把投影 Markdown 写入该路径（默认只打印；"
                                                  "不写入运行目录）")
    status = sub.add_parser("status",
                            help="P2.4-O2 最后可靠状态：当前阶段、等待原因、生产者活动信号、"
                                 "预算与收尾（只读；活动过期只表示无法确认活跃，不代表失败）")
    status.add_argument("--run", required=True, help="运行目录（eval-run / collab / guidance）")
    echeck = sub.add_parser("eval-check", help="E1 只读核对冻结源与输入白名单，不运行实验")
    echeck.add_argument("--suite", default="docs/evaluation/e1/suite.json", help="探索性开发集清单")
    echeck.add_argument("--isolation-evidence", default="",
                        help="E1 隔离证据 JSON；提供时按真实探测证据+当前窗口做运行前准入")
    eprepare = sub.add_parser("eval-prepare", help="E1 离线输入导出；不调用模型，不证明答案读隔离")
    eprepare.add_argument("--suite", default="docs/evaluation/e1/suite.json", help="探索性开发集清单")
    eprepare.add_argument("--out", required=True, help="全新输出目录，已有内容时拒绝覆盖")
    eiso = sub.add_parser("eval-isolate", help="E1 运行端答案读隔离：开窗探测 / 解除 / 准入 / 遗留清扫")
    eiso.add_argument("action", choices=["probe", "release", "check", "sweep"])
    eiso.add_argument("--suite", default="docs/evaluation/e1/suite.json", help="探索性开发集清单")
    eiso.add_argument("--prepared", required=True, help="eval-prepare 输出的目录")
    eiso.add_argument("--arm", default="input-001/M",
                      help="探测目标组目录（相对 prepared，如 input-001/M）")
    eiso.add_argument("--no-codex", action="store_true",
                      help="只探测 Mentis 环境端；准入要求 Codex 端证据时不得使用")
    eiso.add_argument("--run-spec", default="", help="E2 开窗前冻结的代码/协议契约")
    eplan = sub.add_parser("eval-plan",
                           help="P2.4-V1 评估计划先行：为某目标组装并打印评估计划"
                                "（默认离线组装；--real 调模型实例化领域规则）")
    eplan.add_argument("--goal", required=True, help="本轮任务目标原话")
    eplan.add_argument("--material", default="",
                       help="任务素材文件路径（标注化“- 标签：值”格式）；任务实例层据此推导")
    eplan.add_argument("--pilot", action="store_true",
                       help="使用内置合成试点素材（拾光咖啡，虚构品牌）演示完整四层计划")
    eplan.add_argument("--real", action="store_true",
                       help="调用 DeepSeek 实例化领域规则（需凭据；发送目标/brief 条目/素材摘录/已加载规则；"
                            "虚构来源条目会被拒收并留痕）")
    eplan.add_argument("--out", default="", help="同时把计划 JSON 写入该路径（默认只打印）")
    erun = sub.add_parser("eval-run",
                          help="E1 公平运行器：在隔离窗口内运行单个臂（M=评估计划+盲比流程；"
                               "B-1 等基线=方法提示词驱动的执行器循环）")
    erun.add_argument("--prepared", required=True, help="eval-prepare 输出的目录")
    erun.add_argument("--arm", required=True, help="目标组（相对 prepared，如 input-002/B-1）")
    erun.add_argument("--evidence", required=True, help="当前窗口的隔离证据 JSON")
    erun.add_argument("--suite-sha", required=True, help="冻结 suite sha256（准入绑定）")
    erun.add_argument("--suite", default="docs/evaluation/e1/suite.json", help="冻结清单路径")
    erun.add_argument("--out", default="runtime/evaluation/e1-runs", help="运行记录根目录")
    erun.add_argument("--run-spec", required=True, help="eval-freeze 生成的代码/协议契约（E2 准入必需）")
    efreeze = sub.add_parser("eval-freeze", help="干净提交上冻结 E2 代码/协议契约；开窗前执行")
    efreeze.add_argument("--suite", default="docs/evaluation/e1/suite.json")
    efreeze.add_argument("--out", required=True)
    eanswer = sub.add_parser("eval-answer", help="待决后注入受控回答；答案不得提前送入运行组")
    eanswer.add_argument("--run", required=True)
    eanswer.add_argument("--answer", required=True)
    eanswer.add_argument("--answered-by", required=True)
    eanswer.add_argument("--source", choices=["controlled_simulation", "human_user"], default="controlled_simulation")
    econtinue = sub.add_parser("eval-continue", help="unknown 停写核对后至多续办一次")
    econtinue.add_argument("--run", required=True)
    econtinue.add_argument("--checked-by", required=True)
    econtinue.add_argument("--basis", required=True)
    econtinue.add_argument("--artifact-complete", action="store_true", help="只核对作品，保留 unknown，不再委托")
    esummary = sub.add_parser("eval-summary", help="评价侧20臂汇总与作品盲评导出")
    esummary.add_argument("--runs", required=True)
    esummary.add_argument("--suite", default="docs/evaluation/e1/suite.json")
    esummary.add_argument("--out", required=True)
    eguard = sub.add_parser("eval-guard-report", help="根据独立人工标签计算守卫误伤率；缺标签报 unknown")
    eguard.add_argument("--review", required=True)
    args = parser.parse_args(argv)

    if args.command in ("eval-freeze", "eval-summary", "eval-guard-report", "eval-answer", "eval-continue"):
        from mentis.eval_readiness import ReadinessError, freeze_spec, summarize_runs, guard_rates
        from mentis.eval_runner import AdmissionError, FairRun

        try:
            if args.command == "eval-freeze":
                result = freeze_spec(Path(args.out), Path(args.suite))
            elif args.command == "eval-summary":
                result = summarize_runs(Path(args.runs), Path(args.suite), Path(args.out))
            elif args.command == "eval-guard-report":
                result = guard_rates(Path(args.review))
            elif args.command == "eval-answer":
                result = FairRun.reopen(args.run).answer(args.answer, args.answered_by, args.source)
            else:
                result = FairRun.reopen(args.run).checked_continue(args.checked_by, args.basis, args.artifact_complete)
        except (AdmissionError, ReadinessError, OSError, ValueError) as exc:
            print(f"操作未执行：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "eval-run":
        from mentis.eval_runner import AdmissionError, FairRun
        from mentis.eval_isolation import IsolationError

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        try:
            manifest = FairRun(args.prepared, args.arm, args.out, args.suite_sha,
                               args.evidence, suite_path=args.suite, run_spec=args.run_spec).run()
        except (AdmissionError, IsolationError) as exc:
            print(f"运行未启动：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0 if manifest["runner_status"] == "finished" and manifest["artifact"]["exists"] else 1

    if args.command == "eval-plan":
        from mentis.capabilities.registry import load_registry
        from mentis.capabilities.selection import select_for_task
        from mentis.contracts import Brief
        from mentis.evaluation_plan import build_evaluation_plan, render_plan_markdown
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        material_text = ""
        if args.pilot:
            from mentis.collab import COPY_DECK as material_text  # 合成素材（虚构品牌）
        elif args.material:
            material_text = Path(args.material).read_text(encoding="utf-8")
        goal = args.goal
        brief = Brief(brief_id="brief-cli", version=1, objective=goal,
                      constraints=[], preferences=[],
                      created_at=datetime.now().isoformat(timespec="seconds"))
        resources = []
        try:
            _, resources = select_for_task(
                load_registry(), task_label="评估计划",
                task_keywords=["单页活动介绍页"],
                decision_needs=["信息密度", "主操作层级", "表单校验行为", "视觉层级"],
                observation_capabilities={"structure_only": True, "screenshots": True,
                                         "render_measurement": True},
                can_execute=True)
        except Exception:
            resources = []
        use_model = bool(args.real)
        gateway = None
        if use_model:
            from mentis.models.gateway import ModelConfig, ModelGateway
            gateway = ModelGateway(config=ModelConfig())
        plan = build_evaluation_plan(
            goal=goal, brief=brief, material_text=material_text,
            resources=resources, gateway=gateway, use_model=use_model)
        print(render_plan_markdown(plan))
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_bytes(plan.to_json_bytes())
            print(f"[已写入] {Path(args.out).resolve()}")
        mi = plan.model_instantiation
        print(f"[摘要] 维度 {len(plan.entries)} 条（阻塞 {len(plan.blocking_entries())}）；"
              f"模型实例化 {mi.get('status')}"
              + (f"，用量 {mi.get('usage')}" if mi.get("usage") else ""))
        return 0

    if args.command in ("eval-check", "eval-prepare", "eval-isolate"):
        from pathlib import Path as _Path

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
        if args.command == "eval-check":
            from mentis.evaluation import audit_suite
            if args.isolation_evidence:
                from mentis.eval_isolation import eval_check_with_isolation
                result = eval_check_with_isolation(_Path(args.suite),
                                                   _Path(args.isolation_evidence))
            else:
                result = audit_suite(_Path(args.suite))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # Zero means frozen input validation/preparation succeeded; the status
            # and runtime_answer_isolation check explicitly prohibit a ready claim.
            return 1 if result["status"] == "invalid" else 0
        if args.command == "eval-prepare":
            from mentis.evaluation import prepare_suite
            try:
                report = prepare_suite(_Path(args.suite), _Path(args.out))
            except (OSError, ValueError) as exc:
                print(f"评测输入未导出：{exc}", file=sys.stderr)
                return 2
            result = {key: report[key] for key in ("status", "suite_id", "suite_sha256", "sample_count", "checks", "blocking_reasons", "exploratory_blocking_reasons", "formal_claim_limits", "scope")}
            result["package_count"] = sum(len(row["arms"]) for row in report["packages"])
            result["out"] = str(_Path(args.out).resolve())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] != "invalid" else 1
        from mentis.eval_isolation import IsolationError, eval_isolate_cli
        try:
            result = eval_isolate_cli(args)
        except IsolationError as exc:
            print(f"隔离操作未完成：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == "check":
            return 0 if result.get("runtime_answer_isolation", {}).get("passed") else 1
        if args.action == "sweep":
            return 0 if all(r.get("released") for r in result.get("results", [])) else 1
        return 0 if result.get("ok") else 1

    if args.command == "demo-contract":
        from mentis.demo_contract import run_demo

        return run_demo()
    if args.command == "demo-routing":
        from mentis.capabilities.demo_routing import run_demo

        return run_demo()
    if args.command == "diagnose":
        from mentis.diagnose_cli import run_diagnose

        return run_diagnose(args)
    if args.command == "observe-page":
        from mentis.browser_observation import capture_local_html

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(args.out) if args.out else Path("runtime/observations") / f"{stamp}_{uuid4().hex[:6]}"
        try:
            observation = capture_local_html(args.page, out)
        except (ValueError, FileNotFoundError) as exc:
            print(f"观察失败：{exc}", file=sys.stderr)
            return 2
        manifest = Path(out) / "observation.json"
        manifest.write_text(json.dumps(asdict(observation), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[观察记录] {manifest}")
        for view in observation.views:
            print(f"[{view.name}] {view.actual_width}×{view.actual_height} "
                  f"{view.screenshot_path or view.error}")
        print("[边界] " + "；".join(observation.limitations))
        return 0 if all(view.screenshot_path and not view.error for view in observation.views) else 1
    if args.command == "collab":
        from mentis.collab_cli import run_collab

        return run_collab(args)
    if args.command == "collab-resume":
        from mentis.collab_cli import run_resume

        return run_resume(args)
    if args.command == "guide-revise":
        from mentis.guidance_cli import run_guide_revise

        return run_guide_revise(args)
    if args.command == "guide-resume":
        from mentis.guidance_cli import run_guide_resume

        return run_guide_resume(args)
    if args.command == "field-start":
        from mentis.field import run_field_start

        interactive = (not args.non_interactive) and sys.stdin.isatty()
        status, _session_dir, _run_dir = run_field_start(
            goal=args.goal, design=args.design, direction=args.direction,
            interactive=interactive, timeout_s=args.timeout,
            max_repairs=args.max_repairs, label=args.label)
        return 0 if status in ("DELIVERED", "PENDING") else 1
    if args.command == "field-feedback":
        from mentis.field import run_field_feedback

        try:
            run_field_feedback(Path(args.run), satisfied=args.satisfied == "yes",
                               quote=args.quote, blocks=args.block,
                               operation=args.operation, label=args.label)
        except ValueError as exc:
            print(f"反馈未记录：{exc}", file=sys.stderr)
            return 2
        return 0
    if args.command == "trace":
        from mentis.trace import follow_timeline, render_timeline

        run_dir = Path(args.run)
        if not run_dir.is_dir():
            print(f"运行目录不存在：{run_dir.resolve()}", file=sys.stderr)
            return 2
        if args.export:
            from mentis.trace_export import EXPORT_NOTE, export_timeline

            try:
                summary = export_timeline(run_dir, args.export)
            except (OSError, ValueError) as exc:
                print(f"导出失败：{type(exc).__name__}：{exc}", file=sys.stderr)
                return 2
            print(f"[导出完成] {summary['path'].resolve()}（模式 {summary['mode']}；"
                  f"事件 {summary['events']} 条）")
            print(f"[边界] {EXPORT_NOTE}")
            return 0
        if args.follow:
            follow_timeline(run_dir)
        else:
            print(render_timeline(run_dir))
        return 0
    if args.command == "conversation":
        from mentis.trace import render_conversation

        run_dir = Path(args.run)
        if not run_dir.is_dir():
            print(f"运行目录不存在：{run_dir.resolve()}", file=sys.stderr)
            return 2
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # 确切指令可能含 ¥ 等本机 GBK 不可编码字符
        print(render_conversation(run_dir))
        return 0
    if args.command == "view":
        from mentis.run_view import serve_run
        if not 0 <= args.port <= 65535:
            print("端口必须在 0—65535 之间", file=sys.stderr)
            return 2
        try:
            return serve_run(args.run, args.port)
        except (OSError, ValueError) as exc:
            print(f"查看服务无法启动：{exc}", file=sys.stderr)
            return 2

    if args.command == "workbench":
        from mentis.workbench import serve_workbench
        if not 0 <= args.port <= 65535:
            print("端口必须在 0—65535 之间", file=sys.stderr)
            return 2
        try:
            return serve_workbench(args.runs_dir, args.port)
        except (OSError, ValueError) as exc:
            print(f"工作台无法启动：{exc}", file=sys.stderr)
            return 2

    if args.command == "replay":
        from mentis.run_projection import load_projection, render_projection_markdown

        run_dir = Path(args.run)
        if not run_dir.is_dir():
            print(f"运行目录不存在：{run_dir.resolve()}", file=sys.stderr)
            return 2
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        projection = load_projection(run_dir)
        markdown = render_projection_markdown(projection)
        if args.out:
            out_path = Path(args.out)
            if out_path.resolve() in (run_dir.resolve(),) or run_dir.resolve() in out_path.resolve().parents:
                print("输出路径不能位于运行目录内（投影不改写运行材料）", file=sys.stderr)
                return 2
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(markdown, encoding="utf-8")
            print(f"[投影已写入] {out_path.resolve()}（运行目录字节未改动）")
        print(markdown)
        return 0

    if args.command == "status":
        from mentis.run_projection import load_run_state, render_state_markdown

        run_dir = Path(args.run)
        if not run_dir.is_dir():
            print(f"运行目录不存在：{run_dir.resolve()}", file=sys.stderr)
            return 2
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(render_state_markdown(load_run_state(run_dir)))
        return 0

    print(BANNER.format(version=__version__, stage=STAGE).rstrip())
    print(f"Python {platform.python_version()} on {platform.system()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
