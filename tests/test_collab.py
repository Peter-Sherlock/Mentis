"""P2.3 回归例：协作循环的状态、权限与隔离边界（假执行器，全部离线）。"""

import os
import tempfile
import unittest
from pathlib import Path

from mentis.collab import Collaboration, collaboration_from_state
from mentis.contracts import AttemptStatus, StaleVersion, UnauthorizedAction
from mentis.executors.codex_cli import CodexTaskResult

GOOD_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>拾光咖啡 · 周六手冲入门工作坊</title></head><body>
<h1>周六手冲入门工作坊</h1>
<p>时间：2026-11-14（周六）14:00—16:00</p>
<p>地点：拾光咖啡 · 梧桐街店</p>
<p>价格：¥99/人，限 8 人</p>
<form id="signup"><input name="name" required><input name="contact" required>
<button type="submit">报名</button></form>
</body></html>
"""

BAD_PAGE = GOOD_PAGE.replace("<p>价格：¥99/人，限 8 人</p>\n", "")


class FakeAdapter:
    """可编程假执行器：first/second 决定好页/坏页/留反问。"""

    cli_version = "fake-0"

    def __init__(self, behavior=None, thread="t-fake-0000-0000-0000-000000"):
        self.behavior = behavior or {}
        self.thread = thread
        self.calls: list[str] = []

    def _emit(self, spec, status, artifacts):
        return CodexTaskResult(
            task_id="task-fake", attempt_id=f"attempt-{len(self.calls):04d}",
            brief_version=spec.brief_version, workspace=str(spec.workspace),
            status=status, native_thread_id=self.thread if status is not AttemptStatus.UNKNOWN else "",
            usage={"input_tokens": 100, "output_tokens": 10},
            artifact_paths=artifacts, cli_version="fake",
        )

    def _run(self, spec: CodexTaskSpec, status_override=None):
        root = Path(spec.workspace)
        behavior = self.behavior.get("first") if len(self.calls) == 0 else self.behavior.get("second", "good")
        self.calls.append(spec.prompt[:30])
        if behavior == "questions":
            root.joinpath("QUESTIONS.md").write_text(
                "Q: 报名费能不能改成 ¥199？\nQ: 素材里有讲师简介吗？\n", encoding="utf-8")
            return self._emit(spec, AttemptStatus.COMPLETED, ["QUESTIONS.md"])
        if behavior == "soft-question":
            root.joinpath("QUESTIONS.md").write_text("Q: 素材里有讲师简介吗？\n", encoding="utf-8")
            return self._emit(spec, AttemptStatus.COMPLETED, ["QUESTIONS.md"])
        page = GOOD_PAGE if behavior in ("good", "questions", "soft-question") else BAD_PAGE
        root.joinpath("index.html").write_text(page, encoding="utf-8")
        return self._emit(spec, status_override or AttemptStatus.COMPLETED, ["index.html"])

    def start_task(self, spec):
        return self._run(spec)

    def continue_task(self, thread, task_id, spec):
        assert thread == self.thread
        return self._run(spec)


def stub_capture(index, out_dir):
    from mentis.browser_observation import BrowserObservation, RenderedView

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    views = []
    for name in ("desktop", "mobile"):
        p = out / f"{name}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
        views.append(RenderedView(name=name, requested_width=1280, requested_height=800,
                                  screenshot_path=str(p), actual_width=1280, actual_height=800,
                                  sha256="f" * 64))
    return BrowserObservation(source_path=str(index), source_sha256="a" * 64,
                              browser="fake", browser_version="0", views=views,
                              limitations=["测试替身"])


def make_collab(**kw):
    try:
        from tests.test_guidance import file_aware_measure
    except ImportError:
        from test_guidance import file_aware_measure
    defaults = dict(
        goal="为拾光咖啡工作坊制作活动介绍页",
        adapter=FakeAdapter(), gateway=None, browser_capture=stub_capture,
        measure=file_aware_measure, use_model=False, interactive=False,
    )
    defaults.update(kw)
    return Collaboration(**defaults)


class CollabFlowTests(unittest.TestCase):
    def setUp(self):
        # 运行产物隔离到临时目录，不污染项目 runtime/collab
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def make(self, **kw):
        kw.setdefault("base_dir", self.base_dir)
        return make_collab(**kw)

    def test_autonomous_run_delivers_without_user(self):
        """退出条件：存在用户不再回复仍能完成的有界任务。"""
        collab = self.make()
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        self.assertTrue(collab.session.attempts[-1].accepted)
        self.assertNotIn("request_decision",
                         [ix.kind.value for ix in collab.session.interactions])
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        for section in ("看结果", "看变化", "看依据"):
            self.assertIn(section, report)
        self.assertTrue((collab.run_dir / "state.json").is_file())

    def test_workspace_isolation_no_secrets(self):
        collab = self.make()
        collab.run()
        ws = Path(collab.workspace)
        names = {p.name for p in ws.rglob("*") if p.is_file()}
        self.assertNotIn(".env", names)
        for name in names:
            for secret in ("api_key", "token", "secret", "password"):
                self.assertNotIn(secret, name.lower(), name)
        self.assertIn("brief.md", names)
        self.assertIn("copy.md", names)

    def test_design_pending_no_overreach(self):
        """退出条件：需要用户决定时不越权继续。"""
        collab = self.make(mode="design")
        status = collab.run()
        self.assertEqual(status, "PENDING")
        self.assertEqual(collab.session.attempts, [])  # 未委托、未执行、未猜测
        pending = [ix for ix in collab.session.interactions if ix.kind.value == "request_decision"]
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].answer_source)  # 沉默不构成批准
        self.assertEqual(pending[0].blocking_action, "build-page")

    def test_resume_answer_completes(self):
        collab = self.make(mode="design")
        self.assertEqual(collab.run(), "PENDING")
        pending = next(ix for ix in collab.session.interactions if ix.kind.value == "request_decision")
        status = collab.resume(answers={pending.interaction_id: "暖棕纸质感大图"})
        self.assertEqual(status, "DELIVERED")
        selected = [i for b in collab.session.briefs for i in b.preferences
                    if i.provenance.value == "user_selected"]
        self.assertTrue(any("暖棕" in i.text for i in selected))

    def test_executor_questions_routed_and_user_blocked(self):
        """退出条件：执行器反问先由 Mentis 分流；保留决定转用户且阻塞。"""
        collab = self.make(adapter=FakeAdapter(behavior={"first": "questions"}))
        status = collab.run()
        self.assertEqual(status, "PENDING")
        routes = {r["question"].split("？")[0]: r["route"] for r in collab.question_routes}
        self.assertEqual(routes["报名费能不能改成 ¥199"], "request_user")  # 用户保留决定
        self.assertEqual(routes["素材里有讲师简介吗"], "tool_query")  # 素材可答，不问用户
        self.assertTrue((collab.workspace / "ANSWERS.md").is_file())  # 可答部分已就绪
        self.assertFalse(collab.applied_answers)  # 但继续动作保持暂停

    def test_answerable_questions_auto_applied(self):
        collab = self.make(adapter=FakeAdapter(behavior={"first": "soft-question"}))
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        self.assertTrue(collab.applied_answers)
        soft = [r for r in collab.question_routes if r["route"] == "tool_query"]
        self.assertEqual(len(soft), 1)

    def test_feedback_creates_new_brief_and_revision(self):
        collab = self.make()
        self.assertEqual(collab.run(), "DELIVERED")
        v1 = collab.session.current_brief.version
        status = collab.resume(feedback="首图换成咖啡杯特写，保留暖色底")
        self.assertEqual(status, "DELIVERED")
        self.assertEqual(collab.session.current_brief.version, v1 + 1)
        self.assertTrue(any("咖啡杯" in i.text for i in collab.session.current_brief.preferences))
        self.assertTrue(any(r.action_key.startswith("revision-b") for r in collab.codex_runs))

    def test_goal_change_invalidates_old_accept(self):
        collab = self.make()
        self.assertEqual(collab.run(), "DELIVERED")
        old_attempt = collab.session.attempts[-1]
        self.assertTrue(old_attempt.accepted)
        status = collab.resume(goal_new="改做个人作品展示页（同样是本地单页）")
        self.assertEqual(status, "DELIVERED")
        self.assertGreater(collab.session.current_brief.version, 1)
        with self.assertRaises(StaleVersion):
            collab.session.accept(old_attempt.attempt_id)  # 旧版本不能再接受

    def test_revoke_blocks_all_dispatch(self):
        collab = self.make()
        collab.prepare_workspace()
        status = collab.resume(revoke=True)
        self.assertEqual(status, "WITHDRAWN")
        with self.assertRaises(UnauthorizedAction):
            collab.session.dispatch("build-page", "exec-workspace")

    def test_repair_bounded_then_not_delivered(self):
        """预算内修不好 → 不交付、不接受，不无限循环。"""
        collab = self.make(adapter=FakeAdapter(behavior={"first": "bad", "second": "bad"}),
                             max_repairs=1)
        status = collab.run()
        self.assertEqual(status, "NOT_DELIVERED")
        self.assertEqual(collab.repairs_used, 1)
        self.assertFalse(collab.session.attempts[-1].accepted)

    def test_repair_success_delivers(self):
        collab = self.make(adapter=FakeAdapter(behavior={"first": "bad", "second": "good"}),
                             max_repairs=1)
        status = collab.run()
        self.assertEqual(status, "DELIVERED")
        self.assertEqual(collab.repairs_used, 1)

    def test_unknown_timeout_stops_without_retry(self):
        adapter = FakeAdapter()

        def unknown(spec, status_override=None):
            return CodexTaskResult(
                task_id="t", attempt_id="a", brief_version=spec.brief_version,
                workspace=str(spec.workspace), status=AttemptStatus.UNKNOWN,
                native_thread_id="", usage={}, artifact_paths=[], cli_version="fake")

        adapter._run = unknown  # type: ignore[method-assign]
        collab = self.make(adapter=adapter)
        status = collab.run()
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(len(collab.session.attempts), 1)  # 不自动重发

    def test_state_roundtrip_resume_ready(self):
        collab = self.make()
        collab.run()
        restored = collaboration_from_state(collab.run_dir / "state.json")
        self.assertEqual(restored.session.goal, collab.session.goal)
        self.assertEqual(restored.run_id, collab.run_id)
        self.assertEqual(str(restored.workspace), str(collab.workspace))
        self.assertEqual(len(restored.codex_runs), len(collab.codex_runs))

    def test_revision_flow_preserves_goal_constraints_artifact_and_basis(self):
        """T4 核验：v1 初稿 → 用户反馈 → 新版委托 → v2 产物与复检 的已有路径。

        检查原目标、硬约束（含来源分列）、旧版产物快照、修改依据是否保留。
        反馈文本为开发者编写的**样例**，标注「样例反馈」，不冒充项目负责人原话。
        """
        import hashlib
        import json

        from mentis.trace import load_events, render_timeline

        goal = "为拾光咖啡工作坊制作活动介绍页"
        feedback = "（样例反馈）首图放大，并在价格下方加一行退款说明"
        v2_marker = "<!-- v2-revision-marker -->"

        class TwoPageAdapter(FakeAdapter):
            """第二次委托写不同内容（v1/v2 哈希可区分），并发出脚本化子项事件供回放小结。"""

            @staticmethod
            def _emit_probe_events(spec, pattern):
                if not spec.on_event:
                    return
                for i, st in enumerate(pattern):
                    spec.on_event(
                        {"type": "item.completed",
                         "item": {"type": "command_execution", "status": st,
                                  "command": f"probe-{st}"}},
                        {"first": i == 0, "first_event_latency_s": 0.1})

            def _run(self, spec, status_override=None):
                if len(self.calls) >= 1:  # 第二次（修订）委托
                    self._emit_probe_events(
                        spec, ("in_progress", "failed", "in_progress", "completed"))
                    result = super()._run(spec, status_override)
                    (Path(spec.workspace) / "index.html").write_text(
                        GOOD_PAGE + v2_marker, encoding="utf-8")
                    result.artifact_paths = ["index.html"]
                    return result
                self._emit_probe_events(spec, ("in_progress", "completed"))
                return super()._run(spec, status_override)

        collab = self.make(adapter=TwoPageAdapter(behavior={"first": "good"}), goal=goal)
        self.assertEqual(collab.run(), "DELIVERED")
        v1_artifact = next(a for a in collab.session.artifacts
                           if a.attempt_id == collab.session.attempts[0].attempt_id)
        v1_sha = v1_artifact.sha256

        self.assertEqual(collab.resume(feedback=feedback), "DELIVERED")

        # 1) 原目标保留：v1 brief 仍在（带替代标记），v2 沿用同一目标
        briefs = collab.session.briefs
        self.assertEqual(briefs[0].objective, goal)
        self.assertIsNotNone(briefs[0].superseded_by)
        self.assertEqual(briefs[-1].objective, goal)
        self.assertEqual(briefs[-1].version, briefs[0].version + 1)
        # 2) 硬约束保留且来源分列：程序预置约束标 system，不冒充用户原话
        preset = [i for b in briefs for i in b.constraints
                  if "无网络" in i.text]
        self.assertTrue(preset)
        self.assertEqual(preset[0].provenance.value, "system")
        self.assertTrue(all(i.provenance.value == "system" for i in preset))
        self.assertIn("无网络", briefs[-1].constraints[0].text)  # v2 仍携带
        # 3) 旧版产物保留：修订前快照文件本体 + 哈希与 v1 产物记录一致；v2 内容不同
        snapshot = collab.run_dir / "versions" / f"index-{collab.session.attempts[0].attempt_id}.html"
        self.assertTrue(snapshot.is_file())
        snap_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        self.assertEqual(snap_sha, v1_sha)
        v2_sha = hashlib.sha256((Path(collab.workspace) / "index.html").read_bytes()).hexdigest()
        self.assertNotEqual(v1_sha, v2_sha)
        self.assertIn(v2_marker, (Path(collab.workspace) / "index.html").read_text(encoding="utf-8"))
        v2_artifact = next(a for a in collab.session.artifacts
                           if a.attempt_id == collab.session.attempts[-1].attempt_id)
        self.assertEqual(v2_artifact.sha256, v2_sha)
        # 4) 修改依据保留：revision 记录 反馈交互→旧尝试→新尝试，指令=反馈原样
        revision = collab.session.revisions[-1]
        self.assertEqual(revision.instruction, feedback)
        self.assertEqual(revision.from_attempt_id, collab.session.attempts[0].attempt_id)
        self.assertEqual(revision.to_attempt_id, collab.session.attempts[-1].attempt_id)
        feedback_ix = next(ix for ix in collab.session.interactions
                           if ix.interaction_id == revision.feedback_interaction_id)
        self.assertIn("样例反馈", feedback_ix.message)
        # 5) v2 复检：新尝试有 review 且通过；trace 有对应事件与含反馈的确切指令
        review = next(r for r in collab.session.reviews
                      if r.attempt_id == collab.session.attempts[-1].attempt_id)
        self.assertTrue(review.passed)
        events = load_events(collab.run_dir)
        kinds = [e["kind"] for e in events]
        # 结构检查×2（v1/v2）+ 专业评审未执行（use_model=False）×2 次 finalize 各一条
        self.assertEqual(kinds.count("review.recorded"), 4)
        unavailable = [e for e in events if e["kind"] == "review.recorded"
                       and "专业评审未执行" in e["summary"]]
        self.assertEqual(len(unavailable), 2)  # 无模型时如实标注，不冒充评审通过
        self.assertIn("brief.updated", kinds)
        prompt_v2 = (collab.run_dir / "trace" / "prompts" /
                     f"{collab.session.attempts[-1].attempt_id}.md").read_text(encoding="utf-8")
        self.assertIn("样例反馈", prompt_v2)  # 确切指令携带反馈
        # 6) 面向用户记录：报告含阶段语义/快照；回放含子项成败小结
        report = (collab.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("阶段性交付语义", report)
        self.assertIn("修订前快照", report)
        self.assertIn("未反馈", report)  # 满意度如实=未反馈
        timeline = render_timeline(collab.run_dir)
        self.assertIn("执行器子项小结", timeline)
        self.assertIn("失败 1", timeline)  # 修订轮的脚本化失败子项如实统计
        self.assertIn("仅开始未到终态 0", timeline)
        state = json.loads((collab.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "DELIVERED")


if __name__ == "__main__":
    unittest.main()
