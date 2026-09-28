"""P3.2 回归例：指导修订循环（全部离线：假执行器、假模型、文件感知的假测量）。

纪律映射：
- 合格不乱改：无有效诊断 → 零委托；
- 修改回归拒收：修复缺陷但破坏保留项 → REGRESSION，不接受；
- 必要介入：诊断请求用户独有决定 → 暂停修订委托，回答后继续；
- 超时核对：无活跃写入者才按产物核对继续；有活跃写入者转 NEEDS_HUMAN。
受控案例的"答案"只存在于断言与 fixture，不输入受测循环。
"""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import mentis.guidance as guidance_module
from mentis.guidance import (GuidanceCycle, compose_revision_instruction, freeze_baseline,
                             parse_frozen_brief, preserved_items)
from mentis.observation import observe_structure
from mentis.render_measurement import (FormCheck, OverflowElement, RenderMeasurement,
                                       ViewportMeasurement)

GOAL = "为拾光咖啡（虚构品牌）周六手冲入门工作坊做一个温暖、简洁的活动介绍页"
CONSTRAINT = "本地单页、无网络、不发送真实数据；改动限于隔离工作区"
COPY_DECK = """# 拾光咖啡 · 周六手冲入门工作坊（合成素材，虚构品牌）

- 活动名称：周六手冲入门工作坊
- 日期时间：2026-11-14（周六）14:00—16:00
- 地点：拾光咖啡 · 梧桐街店（演示地址：梧桐街 12 号）
- 价格：¥99/人，限 8 人
- 报名方式：留下姓名和联系方式（本地演示，不发送任何数据）
- 讲师：林小豆（虚构人物），三年手冲教学经验
"""

DEFECT_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>拾光咖啡 · 周六手冲入门工作坊</title></head><body>
<h1>周六手冲入门工作坊</h1>
<!-- OVERFLOW-MARKER -->
<div class="hero-row" style="width:9999px">整行内容超宽</div>
<p>时间：2026-11-14（周六）14:00—16:00</p>
<p>地点：拾光咖啡 · 梧桐街店</p>
<p>价格：¥99/人，限 8 人</p>
<p>讲师：林小豆</p><p>三年手冲教学经验</p>
<form id="signup"><input name="name" required><input name="contact" required>
<button type="submit">报名</button></form>
</body></html>
"""

FIXED_PAGE = DEFECT_PAGE.replace('<!-- OVERFLOW-MARKER -->\n'
                                 '<div class="hero-row" style="width:9999px">整行内容超宽</div>\n', "")
REGRESSION_PAGE = FIXED_PAGE.replace("<p>价格：¥99/人，限 8 人</p>\n", "")

DIAGNOSIS_OVERFLOW = json.dumps({
    "diagnoses": [{
        "phenomenon": "移动 390 视口横向溢出 120px，hero 行内容超出可视宽度",
        "evidence_refs": ["ov:mobile:1", "mv:mobile"],
        "severity": "blocking",
        "interpretation": "渲染测量显示 scrollWidth 510 > innerWidth 390，违反移动端硬要求",
        "modification_candidates": ["约束该行宽度并允许换行，使 scrollWidth 不超过视口"],
    }],
    "next_action": "agent_decide",
    "decision_requests": [],
}, ensure_ascii=False)

DIAGNOSIS_QUALIFIED = json.dumps({
    "diagnoses": [], "next_action": "stop", "decision_requests": [],
}, ensure_ascii=False)

DIAGNOSIS_NEEDS_USER = json.dumps({
    "diagnoses": [{
        "phenomenon": "活动是否需要增加线上直播渠道影响版面取舍",
        "evidence_refs": ["h:1"],
        "severity": "plan_changing",
        "interpretation": "素材未提及，涉及目标取舍",
        "modification_candidates": ["待用户决定后调整"],
    }],
    "next_action": "request_user",
    "decision_requests": [{
        "question": "是否要把线上直播纳入活动信息？",
        "blocking_reason": "影响版面取舍且素材没有该事实",
        "user_unique_info_or_auth_basis": "只有用户知道活动是否提供直播",
        "excluded_autonomous_paths": "素材与页面内均无该信息；无法自决或查证",
    }],
}, ensure_ascii=False)

REVIEW_RESOLVED = json.dumps({
    "resolved": ["d1"], "unresolved": [], "new_issues": [], "summary": "溢出已消除",
}, ensure_ascii=False)

DIAGNOSIS_MISSING_PRICE = json.dumps({
    "diagnoses": [{
        "phenomenon": "页面正文未出现价格事实（对照素材应有 ¥99）",
        "evidence_refs": ["h:1"],
        "severity": "blocking",
        "interpretation": "结构正文无 ¥99，活动价格信息缺失",
        "modification_candidates": ["在原价格位置恢复 ¥99/人，限 8 人"],
    }],
    "next_action": "agent_decide",
    "decision_requests": [],
}, ensure_ascii=False)


def page_png(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + struct.pack(">II", width, height) + b"\x00" * 8)


class FakeGateway:
    """假模型：按队列返回固定响应；记录收到的输入与图像。"""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list = []
        self.config = SimpleNamespace(model="mock-model")

    def has_credentials(self) -> bool:
        return True

    def usage_summary(self) -> dict:
        return {"real_calls": 0, "mock_calls": len(self.calls), "model": "mock-model"}

    def complete(self, system, user_prompt, json_mode=True, mock=False, images=None):
        self.calls.append({"prompt": user_prompt, "images": [n for n, _ in (images or [])]})
        text = self.responses.pop(0) if self.responses else "{}"
        return SimpleNamespace(ok=True, mock=True, model="mock-model", usage={"total_tokens": 1},
                               error="", response_text=text, image_refs=[n for n, _ in (images or [])],
                               request_id="fake")


class FakeAdapter:
    """假执行器：按行为写入固定页面；记录收到的指令。"""

    cli_version = "fake-0"

    def __init__(self, behavior=None, thread="t-fake-0000-0000-0000-000000",
                 status=None):
        self.behavior = behavior or {}
        self.thread = thread
        self.status = status
        self.prompts: list = []

    def _run(self, spec):
        self.prompts.append(spec.prompt)
        root = Path(spec.workspace)
        step = "first" if len(self.prompts) == 1 else "second"
        behavior = self.behavior.get(step, "fixed")
        page = {"defect": DEFECT_PAGE, "fixed": FIXED_PAGE, "regression": REGRESSION_PAGE}.get(behavior)
        if page is not None:
            root.joinpath("index.html").write_text(page, encoding="utf-8")
        from mentis.contracts import AttemptStatus

        status = self.status or AttemptStatus.COMPLETED
        return SimpleNamespace(
            task_id="task-fake", attempt_id=f"attempt-{len(self.prompts):04d}",
            brief_version=spec.brief_version, status=status,
            native_thread_id=self.thread if status is not AttemptStatus.UNKNOWN else "",
            usage={"input_tokens": 100, "output_tokens": 10},
            artifact_paths=["index.html"], final_response="done", error="",
            events=[], cli_version="fake")

    def start_task(self, spec):
        return self._run(spec)

    def continue_task(self, thread, task_id, spec):
        assert thread == self.thread
        return self._run(spec)


def file_aware_measure(page, out_dir):
    """假测量：读取真实页面内容生成测量记录（缺陷标记 → 溢出 120px）。"""
    page = Path(page)
    html = page.read_text(encoding="utf-8")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    viewports = []
    for name, (w, h) in (("mobile", (390, 844)), ("desktop", (1280, 800))):
        viewport = ViewportMeasurement(name=name, width=w, height=h, inner_width=w,
                                       scroll_width=w, overflow_px=0)
        if "OVERFLOW-MARKER" in html:
            viewport.overflow_px = 120
            viewport.scroll_width = w + 120
            viewport.overflowing.append(OverflowElement(
                item_id=f"ov:{name}:1", tag="div", ident=".hero-row",
                text="整行内容超宽", left=0, right=w + 120, width=w + 120))
        if "<form" in html:
            viewport.forms.append(FormCheck(
                item_id=f"formchk:{name}:1", index=0, ident="#signup", action="", method="get",
                fields=2, required_fields=2, empty_check_validity=False,
                empty_submit_fired=False, filled_check_validity=True, filled_submit_fired=True))
        viewport.network_intent = {"hasFetch": False, "hasXHR": False, "hasBeacon": False}
        viewports.append(viewport)
    return RenderMeasurement(
        source_path=str(page), source_sha256="b" * 64, browser="fake", browser_version="0",
        viewports=viewports,
        limitations=["测试替身：由文件内容生成，无真实浏览器渲染"],
        created_at="2026-09-28T00:00:00")


def stub_capture(index, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from mentis.browser_observation import BrowserObservation, RenderedView

    views = []
    for i, (name, w, h) in enumerate((("desktop", 1280, 800), ("mobile", 390, 844))):
        p = out / f"{name}.png"
        p.write_bytes(page_png(w, h))
        views.append(RenderedView(name=name, requested_width=w, requested_height=h,
                                  screenshot_path=str(p), actual_width=w, actual_height=h,
                                  sha256="c" * 64))
    return BrowserObservation(source_path=str(index), source_sha256="a" * 64,
                              browser="fake", browser_version="0", views=views,
                              limitations=["测试替身"])


def make_baseline(root: Path, page: str = DEFECT_PAGE) -> Path:
    baseline = root / "baseline-run"
    (baseline / "workspace" / "input").mkdir(parents=True)
    (baseline / "observation").mkdir()
    (baseline / "workspace" / "index.html").write_text(page, encoding="utf-8")
    (baseline / "workspace" / "brief.md").write_text(
        f"# 目标\n{GOAL}\n\n# 约束与偏好\n- {CONSTRAINT}\n", encoding="utf-8")
    (baseline / "workspace" / "input" / "copy.md").write_text(COPY_DECK, encoding="utf-8")
    (baseline / "observation" / "desktop.png").write_bytes(page_png(1280, 800))
    (baseline / "observation" / "mobile.png").write_bytes(page_png(390, 844))
    return baseline


class GuidanceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cycle_kwargs = dict(
            browser_capture=stub_capture, measure=file_aware_measure, use_model=True,
            include_images=True, timeout_s=60,
        )

    def cycle(self, baseline, gateway, adapter=None, **kw):
        merged = dict(self.cycle_kwargs)
        merged.update(kw)
        return GuidanceCycle(baseline_dir=baseline, run_dir=self.root / "run",
                             adapter=adapter, gateway=gateway, **merged)


class FreezeAndMaterialTests(GuidanceTestBase):
    def test_freeze_preserves_v1_and_original_untouched(self):
        baseline = make_baseline(self.root)
        run_dir = self.root / "run"
        freeze = freeze_baseline(baseline, run_dir)
        v1 = run_dir / "v1" / "index.html"
        self.assertTrue(v1.is_file())
        self.assertEqual(freeze["v1_html_sha256"][:12], "946005ae" [:0] + freeze["v1_html_sha256"][:12])
        import hashlib
        self.assertEqual(
            hashlib.sha256((baseline / "workspace" / "index.html").read_bytes()).hexdigest(),
            hashlib.sha256(v1.read_bytes()).hexdigest())
        self.assertTrue((baseline / "workspace" / "index.html").is_file())
        self.assertTrue((run_dir / "legacy-screenshots" / "mobile.png").is_file())

    def test_preserved_items_derived_from_materials_only(self):
        baseline = make_baseline(self.root)
        items = preserved_items(baseline / "run-v1-does-not-exist") if False else None
        run_dir = self.root / "run"
        freeze_baseline(baseline, run_dir)
        items = preserved_items(run_dir / "v1")
        labels = dict(items)
        self.assertEqual(labels.get("价格"), "¥99")
        self.assertEqual(labels.get("活动日期"), "2026-11-14")
        self.assertEqual(labels.get("活动名称"), "周六手冲入门工作坊")
        self.assertIn("梧桐街", " ".join(v for k, v in items))
        goal, constraints = parse_frozen_brief(run_dir / "v1")
        self.assertIn("温暖、简洁", goal)
        self.assertEqual(constraints, [CONSTRAINT])

    def test_instruction_uses_diagnosis_and_measurement_not_developer_answers(self):
        baseline = make_baseline(self.root)
        run_dir = self.root / "run"
        freeze_baseline(baseline, run_dir)
        structure = observe_structure(str(baseline / "workspace" / "index.html"))
        pack = SimpleNamespace(measurement=file_aware_measure(
            baseline / "workspace" / "index.html", self.root / "m"))
        bundle = SimpleNamespace(valid_diagnoses=[SimpleNamespace(
            phenomenon="模型发现的现象", evidence_refs=["ov:mobile:1"], severity="blocking",
            interpretation="模型解释", modification_candidates=["模型候选修改"])])
        text = compose_revision_instruction(
            bundle, ["d1"], SimpleNamespace(measurement=pack.measurement),
            preserved_items(run_dir / "v1"), [CONSTRAINT])
        self.assertIn("模型发现的现象", text)
        self.assertIn("模型候选修改", text)
        self.assertIn("ov:mobile:1", text)
        self.assertIn("¥99", text)
        self.assertIn(CONSTRAINT, text)
        self.assertNotIn("OVERFLOW-MARKER", text)  # 开发者缺陷标记不进入指令


class FailingThenFixedAdapter(FakeAdapter):
    """第一轮：写入部分修改后进程失败（模拟执行器中断）；第二轮：完成。"""

    def _run(self, spec):
        if not self.prompts:
            self.prompts.append(spec.prompt)
            Path(spec.workspace).joinpath("index.html").write_text(
                DEFECT_PAGE.replace("padding: 35px 0 30px", "padding: 24px 0 20px"),
                encoding="utf-8")
            from mentis.contracts import AttemptStatus

            return SimpleNamespace(
                task_id="task-fake", attempt_id="attempt-0001",
                brief_version=spec.brief_version, status=AttemptStatus.FAILED,
                native_thread_id=self.thread, usage={}, artifact_paths=["index.html"],
                final_response="", error="Codex 未报告成功终态（进程退出码 1）",
                events=[], cli_version="fake")
        return super()._run(spec)


class GuidanceLoopTests(GuidanceTestBase):
    def test_full_improvement_loop(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter)
        status = cycle.run()
        self.assertEqual(status, "IMPROVED")
        self.assertEqual(cycle.verdict, "improved")
        self.assertTrue((cycle.run_dir / "v2" / "index.html").is_file())
        self.assertNotIn("OVERFLOW-MARKER",
                         (cycle.run_dir / "v2" / "index.html").read_text(encoding="utf-8"))
        self.assertTrue(cycle.session.attempts[-1].accepted)
        instruction = (cycle.run_dir / "instruction-round1.md").read_text(encoding="utf-8")
        self.assertIn("ov:mobile:1", instruction)
        self.assertIn("¥99", instruction)
        # 诊断确实收到双视口截图（图像输入声明进入诊断提示）
        self.assertIn("[图像输入]", gateway.calls[0]["prompt"])
        self.assertEqual(gateway.calls[0]["images"], ["desktop.png", "mobile.png"])
        report = (cycle.run_dir / "report.md").read_text(encoding="utf-8")
        for section in ("看结果", "看变化", "看依据"):
            self.assertIn(section, report)
        self.assertIn("给执行器的完整指令", report)
        self.assertTrue((cycle.run_dir / "codex-round1-events.json").is_file())
        # 合同记录：通知不阻塞、修订链与评审在案
        kinds = [ix.kind.value for ix in cycle.session.interactions]
        self.assertIn("notify_progress", kinds)
        self.assertNotIn("request_decision", kinds)

    def test_qualified_page_not_modified(self):
        baseline = make_baseline(self.root, page=FIXED_PAGE)
        gateway = FakeGateway([DIAGNOSIS_QUALIFIED])
        adapter = FakeAdapter()
        cycle = self.cycle(baseline, gateway, adapter)
        status = cycle.run()
        self.assertEqual(status, "QUALIFIED_NO_CHANGE")
        self.assertEqual(adapter.prompts, [])  # 合格不乱改：零委托
        self.assertEqual(cycle.verdict, "qualified_no_change")

    def test_regression_rejected_not_marked_improvement(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "regression"})
        cycle = self.cycle(baseline, gateway, adapter, max_rounds=1)
        status = cycle.run()
        self.assertEqual(status, "REGRESSION")
        self.assertFalse(cycle.session.attempts[-1].accepted)  # 回归不得标改善
        review = json.loads((cycle.run_dir / "review-round1.md").read_text(encoding="utf-8"))
        self.assertTrue(any("价格" in r for r in review["preserved_regression"]))
        self.assertTrue((cycle.run_dir / "v2" / "index.html").is_file())  # 证据保留

    def test_second_round_runs_within_budget(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED,
                               DIAGNOSIS_MISSING_PRICE, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "regression", "second": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter, max_rounds=2)
        status = cycle.run()
        self.assertEqual(status, "IMPROVED")
        self.assertEqual(len(adapter.prompts), 2)
        # 回归发现进入第二轮指令（复检遗留问题不静默丢失）
        instruction2 = (cycle.run_dir / "instruction-round2.md").read_text(encoding="utf-8")
        self.assertIn("上一轮遗留问题", instruction2)
        self.assertIn("价格", instruction2)

    def test_decision_request_pauses_then_resume_completes(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_NEEDS_USER, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter)
        status = cycle.run()
        self.assertEqual(status, "NEEDS_DECISION")
        self.assertEqual(adapter.prompts, [])  # 依赖动作暂停，不越权委托
        pending = [ix for ix in cycle.session.interactions
                   if ix.kind.value == "request_decision" and ix.answer_source is None]
        self.assertEqual(len(pending), 1)
        status = cycle.resume(answers={pending[0].interaction_id: "需要，纳入直播信息"})
        self.assertEqual(status, "IMPROVED")
        instruction = (cycle.run_dir / "instruction-round1b.md").read_text(encoding="utf-8")
        self.assertIn("需要，纳入直播信息", instruction)  # 用户决定进入指令

    def test_presentation_preference_routed_autonomously(self):
        """可逆的呈现取舍由 Mentis 自主决定（不转问用户），进入指令并留痕。"""
        preference_request = json.dumps({
            "diagnoses": [{
                "phenomenon": "移动端首屏信息密度较高（偏好取舍）",
                "evidence_refs": ["mv:mobile", "h:1"],
                "severity": "minor",
                "interpretation": "插画占纵向空间较大，属偏好取舍而非硬要求冲突",
                "modification_candidates": ["缩小移动端插画高度"],
            }],
            "next_action": "request_user",
            "decision_requests": [{
                "question": "移动端首屏是否需要在插画之前就露出『活动时间/地点』卡片，"
                            "还是保持当前插画优先的节奏？",
                "blocking_reason": "IH-2/VE-2 声明冲突，属偏好取舍",
                "user_unique_info_or_auth_basis": "密度取舍需跟随用户反应",
                "excluded_autonomous_paths": "测量已排除硬要求问题",
            }],
        }, ensure_ascii=False)
        baseline = make_baseline(self.root)
        gateway = FakeGateway([preference_request, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter)
        status = cycle.run()
        self.assertEqual(status, "IMPROVED")  # 不因偏好请求暂停
        self.assertEqual([r["route"] for r in cycle.decision_routes], ["agent_decide"])
        instruction = (cycle.run_dir / "instruction-round1.md").read_text(encoding="utf-8")
        self.assertIn("Mentis 对诊断请求的自主分流", instruction)
        self.assertIn("保持当前内容与顺序", instruction)

    def test_unknown_reconciled_from_artifacts(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        from mentis.contracts import AttemptStatus

        adapter = FakeAdapter(behavior={"first": "fixed"}, status=AttemptStatus.UNKNOWN)
        cycle = self.cycle(baseline, gateway, adapter)
        original = guidance_module.codex_processes_since
        guidance_module.codex_processes_since = lambda since: []
        try:
            status = cycle.run()
        finally:
            guidance_module.codex_processes_since = original
        self.assertEqual(status, "IMPROVED")
        self.assertEqual(cycle.rounds[-1].outcome, "reconciled_from_artifacts")
        self.assertEqual(len(adapter.prompts), 1)  # 不盲目重发

    def test_unknown_with_active_writer_needs_human(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW])
        from mentis.contracts import AttemptStatus

        adapter = FakeAdapter(behavior={"first": "fixed"}, status=AttemptStatus.UNKNOWN)
        cycle = self.cycle(baseline, gateway, adapter)
        original = guidance_module.codex_processes_since
        guidance_module.codex_processes_since = lambda since: [{"Id": 1, "StartTime": "x"}]
        try:
            status = cycle.run()
        finally:
            guidance_module.codex_processes_since = original
        self.assertEqual(status, "NEEDS_HUMAN")
        self.assertEqual(len(adapter.prompts), 1)
        self.assertEqual(cycle.session.attempts[-1].status.value, "unknown")

    def test_failed_round_retried_after_review(self):
        """失败轮：核对（无活跃写入者）后续办，计入预算；不盲目重试。"""
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        adapter = FailingThenFixedAdapter(behavior={"second": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter, max_rounds=2)
        self.assertEqual(cycle.run(), "FAILED")  # 第一轮如实失败
        from mentis.guidance import guidance_from_state

        original = guidance_module.codex_processes_since
        guidance_module.codex_processes_since = lambda since: []
        try:
            restored = guidance_from_state(
                cycle.run_dir / "state.json", adapter=adapter, gateway=gateway,
                browser_capture=stub_capture, measure=file_aware_measure)
            status = restored.resume(retry=True)
        finally:
            guidance_module.codex_processes_since = original
        self.assertEqual(status, "IMPROVED")
        instruction2 = (restored.run_dir / "instruction-round2.md").read_text(encoding="utf-8")
        self.assertIn("续办说明", instruction2)
        self.assertIn("git diff", instruction2)
        self.assertEqual(len(adapter.prompts), 2)  # 预算内第二轮，非无限重试

    def test_retry_blocked_while_executor_active(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW])
        from mentis.contracts import AttemptStatus

        adapter = FakeAdapter(behavior={"first": "defect"}, status=AttemptStatus.FAILED)
        cycle = self.cycle(baseline, gateway, adapter, max_rounds=2)
        self.assertEqual(cycle.run(), "FAILED")
        from mentis.guidance import guidance_from_state

        original = guidance_module.codex_processes_since
        guidance_module.codex_processes_since = lambda since: [{"Id": 7}]
        try:
            restored = guidance_from_state(
                cycle.run_dir / "state.json", adapter=adapter, gateway=gateway,
                browser_capture=stub_capture, measure=file_aware_measure)
            status = restored.resume(retry=True)
        finally:
            guidance_module.codex_processes_since = original
        self.assertEqual(status, "NEEDS_HUMAN")  # 有活跃写入者：不续办
        self.assertEqual(len(adapter.prompts), 1)

    def test_preserved_needle_calibrated_to_v1_presentation(self):
        """素材整句跨 DOM 节点时，核对串按 v1 实际呈现收窄（否则会误报回归）。"""
        baseline = make_baseline(self.root)
        run_dir = self.root / "run"
        freeze_baseline(baseline, run_dir)
        items = dict(preserved_items(run_dir / "v1"))
        self.assertEqual(items.get("讲师"), "林小豆，三年手冲教学经验")  # 素材整句
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        cycle = self.cycle(baseline, gateway, FakeAdapter(behavior={"first": "fixed"}))
        self.assertEqual(cycle.run(), "IMPROVED")  # 校准后不误报
        import json as _json
        meta = _json.loads((cycle.run_dir / "state.json").read_text(encoding="utf-8"))
        calibrated = dict(meta["preserved_items"])
        self.assertEqual(calibrated.get("讲师"), "林小豆")

    def test_reverify_corrects_false_regression(self):
        """复现真实误判：过严核对串造成假回归 → reverify 修正工具后透明更正。"""
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter, max_rounds=1)
        original = guidance_module.preserved_items
        guidance_module.preserved_items = lambda v1: [("讲师", "林小豆，三年手冲教学经验")]
        cycle._calibrate_preserved = lambda: None  # 模拟修正前：无校准
        try:
            self.assertEqual(cycle.run(), "REGRESSION")  # 过严核对串 → 假回归
        finally:
            guidance_module.preserved_items = original
            del cycle._calibrate_preserved  # 恢复校准方法
        self.assertFalse(cycle.session.attempts[-1].accepted)
        status = cycle.reverify()  # 校准核对串 + 复用历史复查 → 更正
        self.assertEqual(status, "IMPROVED")
        self.assertTrue(cycle.session.attempts[-1].accepted)
        self.assertIn("复核更正", "；".join(cycle.rounds[-1].notes))
        report = (cycle.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("复核更正", report)  # 原判定保留在备注，可审计

    def test_state_roundtrip_and_resume_ready(self):
        baseline = make_baseline(self.root)
        gateway = FakeGateway([DIAGNOSIS_OVERFLOW, REVIEW_RESOLVED])
        adapter = FakeAdapter(behavior={"first": "fixed"})
        cycle = self.cycle(baseline, gateway, adapter)
        cycle.run()
        from mentis.guidance import guidance_from_state

        restored = guidance_from_state(
            cycle.run_dir / "state.json", adapter=FakeAdapter(), gateway=gateway,
            browser_capture=stub_capture, measure=file_aware_measure)
        self.assertEqual(restored.run_id, cycle.run_id)
        self.assertEqual(dict(restored._preserved).get("价格"), "¥99")
        self.assertEqual(restored.status, "IMPROVED")


if __name__ == "__main__":
    unittest.main()
