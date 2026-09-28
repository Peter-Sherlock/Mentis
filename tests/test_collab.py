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
    defaults = dict(
        goal="为拾光咖啡工作坊制作活动介绍页",
        adapter=FakeAdapter(), gateway=None, browser_capture=stub_capture,
        use_model=False, interactive=False,
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


if __name__ == "__main__":
    unittest.main()
