"""E1 公平运行器回归例（全部离线：Fake 执行器 + Mock 网关，已标注；准入检查为注入替身）。

覆盖（E1 收尾退出门槛的离线部分）：
- 双臂共享管线：包展开一致、确切指令先落盘（哈希=发送）、事件/用量入 trace；
- 基线臂（B-1）：提示词驱动循环，预算内委托，无外层模型调用；
- M 臂：计划先落盘 → 诊断 → 路由 → 修订（预算内）→ 盲比 → 分级；合格不改零委托；
- 预算逐项执行（attempts/revisions/model_calls），耗尽如实收尾不冒充完成；
- 准入 fail-closed：check_admission 未通过 → AdmissionError，执行器零调用。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mentis.eval_runner import AdmissionError, FairRun
from mentis.executors.codex_cli import CodexTaskResult
from mentis.contracts import AttemptStatus

try:
    from tests.test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from tests.test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                                     DIAGNOSIS_GENERIC, DIAGNOSIS_QUALIFIED,
                                     FakeGateway, file_aware_measure)
except ImportError:  # discover -s tests 以顶层模块导入
    from test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from test_guidance import (BLIND_AFTER_X, BLIND_AFTER_Y, _FixedCoin,
                               DIAGNOSIS_GENERIC, DIAGNOSIS_QUALIFIED,
                               FakeGateway, file_aware_measure)

COPIED_DECK = """# 拾光咖啡 · 周六手冲入门工作坊（合成素材，虚构品牌）

- 活动名称：周六手冲入门工作坊
- 日期时间：2026-11-14（周六）14:00—16:00
- 地点：拾光咖啡 · 梧桐街店（演示地址：梧桐街 12 号）
- 价格：¥99/人，限 8 人
- 报名方式：留下姓名和联系方式（本地演示，不发送任何数据）
"""

POLICY = {"max_executor_attempts": 3, "max_auto_revisions": 2,
          "max_guidance_model_calls": 4, "per_turn_timeout_s": 600,
          "execution_scope": "只在本地隔离工作区制作和检查，不发布，不发送数据"}


def make_prepared(root: Path, with_draft: bool = True) -> Path:
    """最小冻结输入包夹具（结构对齐 eval-prepare 输出；B-1 与 M 同内容合成素材）。"""
    for arm in ("input-002/B-1", "input-002/M"):
        arm_dir = root / "prepared" / arm
        (arm_dir / "workspace").mkdir(parents=True)
        if with_draft:
            (arm_dir / "workspace" / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
        (arm_dir / "input.json").write_text(json.dumps({
            "input": {"user_goal": "用附件里的完整活动资料做一个温暖、简洁的本地活动页",
                      "user_feedback": "页面好像少了点报名信息。（样例反馈）",
                      "feedback_source": "simulated", "conversation_context": []},
            "policy": POLICY, "observations_missing": []}, ensure_ascii=False),
            encoding="utf-8")
        (arm_dir / "prompt.md").write_text(
            "你是负责本地单页作品质量的专业协作代理（B-1 测试夹具提示词）。", encoding="utf-8")
        (arm_dir / "input").mkdir()
        (arm_dir / "input" / "copy.md").write_text(COPIED_DECK, encoding="utf-8")
    (root / "prepared" / "prepare-report.json").write_text("{}", encoding="utf-8")
    return root / "prepared"


class RecordingAdapter(FakeAdapter):
    """记录完整收到的 prompt（供哈希一致性断言）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.full_prompts: list = []

    def _run(self, spec, status_override=None):
        self.full_prompts.append(spec.prompt)
        return super()._run(spec, status_override)


PASSING_ISO = {"runtime_answer_isolation": {"passed": True, "window_id": "w-test",
                                            "checks": []}}
FAILING_ISO = {"runtime_answer_isolation": {"passed": False, "window_id": "", "checks": [
    {"name": "live_denial_current", "passed": False, "detail": "受保护路径可读"}]}}


def make_run(root: Path, prepared: Path, arm: str, adapter, gateway):
    run = FairRun(prepared, arm, root / "runs", suite_sha256="a" * 64,
                  evidence_path=root / "evidence.json", adapter=adapter,
                  gateway=gateway, browser_capture=stub_capture,
                  measure=file_aware_measure, base_dir=root / "runs",
                  run_id="test-run-" + arm.replace("/", "-"))
    return run


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def patch_admission(self, result):
        import unittest.mock as mock

        return mock.patch("mentis.eval_runner.check_admission", return_value=result)

    def test_baseline_arm_runs_prompt_loop_within_budget(self):
        """B-1 夹具：提示词驱动循环，1 次委托完成；确切指令落盘=发送；无外层模型调用。"""
        prepared = make_prepared(self.root)
        adapter = RecordingAdapter(behavior={"first": "good"})
        gateway = FakeGateway([])
        run = make_run(self.root, prepared, "input-002/B-1", adapter, gateway)
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        self.assertEqual(manifest["budget"]["used"]["attempts"], 1)
        self.assertEqual(manifest["budget"]["used"]["model_calls"], 0)
        self.assertTrue(manifest["artifact"]["exists"])
        self.assertEqual(manifest["isolation"]["admission"], "passed")
        # 展开一致性：工作区含 index.html + input.json + prompt.md + input/copy.md
        ws = run.workspace
        self.assertTrue((ws / "index.html").is_file())
        self.assertTrue((ws / "input.json").is_file())
        self.assertTrue((ws / "prompt.md").is_file())
        self.assertTrue((ws / "input" / "copy.md").is_file())
        # 确切指令先落盘且哈希=发送文本
        sent = adapter.full_prompts[0]
        prompt_file = run.run_dir / "trace" / "prompts" / "attempt-001.md"
        import hashlib

        self.assertEqual(hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
                         hashlib.sha256(sent.encode("utf-8")).hexdigest())
        self.assertIn("B-1 测试夹具提示词", sent)
        self.assertTrue((run.run_dir / "run-manifest.json").is_file())

    def test_baseline_failed_attempts_stop_at_budget(self):
        """执行器失败 → 预算内续办；3 次委托后停止，不冒充完成。"""
        prepared = make_prepared(self.root)

        class FailingAdapter(FakeAdapter):
            def _run(self, spec, status_override=None):
                return self._emit(spec, AttemptStatus.FAILED, [])

        run = make_run(self.root, prepared, "input-002/B-1", FailingAdapter(),
                       FakeGateway([]))
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        self.assertEqual(manifest["budget"]["used"]["attempts"], 3)
        self.assertEqual(manifest["summary"]["status"], "failed")

    def test_baseline_unknown_stops_without_retry(self):
        """unknown：锁语义由 Adapter 持有，运行器不自动重发，如实收尾。"""
        prepared = make_prepared(self.root)

        def unknown(spec, status_override=None):
            return CodexTaskResult(
                task_id="t", attempt_id="a", brief_version=0,
                workspace=str(spec.workspace), status=AttemptStatus.UNKNOWN,
                native_thread_id="t-unknown", usage={}, artifact_paths=[],
                cli_version="fake")

        adapter = FakeAdapter()
        adapter._run = unknown  # type: ignore[method-assign]
        run = make_run(self.root, prepared, "input-002/B-1", adapter, FakeGateway([]))
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        self.assertEqual(manifest["budget"]["used"]["attempts"], 1)
        self.assertEqual(manifest["summary"]["status"], "unknown")

    def test_mentis_arm_full_flow_with_blind_verdict(self):
        """M 臂：计划先落盘 → 诊断（Mock）→ 路由采纳 → 修订 → 盲比（Mock 两序一致）→ improved。"""
        prepared = make_prepared(self.root)

        class ChangingAdapter(FakeAdapter):
            """修订委托（本流程唯一委托）写不同页面（artifact_changed=True）。"""

            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                if len(self.calls) >= 1:
                    Path(spec.workspace).joinpath("index.html").write_text(
                        GOOD_PAGE + "<!-- e1-m-v2 -->", encoding="utf-8")
                return result

        adapter = ChangingAdapter(behavior={"first": "good"})
        gateway = FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y])
        run = make_run(self.root, prepared, "input-002/M", adapter, gateway)
        run._blind_rng = _FixedCoin()
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        summary = manifest["summary"]
        self.assertEqual(summary["verdict"], "improved")
        # 计划实例化 + 诊断 + 盲比复查各1单位；两序盲比分开记录实际请求。
        self.assertEqual(manifest["budget"]["used"],
                         {"attempts": 1, "revisions": 1, "model_calls": 3})
        self.assertTrue(summary["blind"]["consistent"])
        # 计划先于委托（事件序）
        from mentis.trace import load_events

        events = load_events(run.run_dir)
        seqs = {e["kind"]: e["seq"] for e in events}
        self.assertLess(seqs["evaluation_plan.recorded"], seqs["delegation.prepared"])
        self.assertTrue((run.run_dir / "evaluation-plan" / "plan-v1.json").is_file())
        self.assertTrue((run.run_dir / "blind-review.json").is_file())

    def test_mentis_arm_qualified_no_change_zero_dispatch(self):
        """M 臂合格不改：零委托、零修订；计划与诊断各1单位。"""
        prepared = make_prepared(self.root)
        adapter = FakeAdapter(behavior={"first": "good"})
        run = make_run(self.root, prepared, "input-002/M", adapter,
                       FakeGateway([DIAGNOSIS_QUALIFIED]))
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        self.assertEqual(manifest["budget"]["used"]["attempts"], 0)
        self.assertEqual(manifest["budget"]["used"]["model_calls"], 2)
        self.assertEqual(manifest["summary"]["verdict"], "no_change")

    def test_mentis_arm_budget_exhausted_honest_stop(self):
        """模型预算1单位：计划后诊断被拦截，如实收尾 unverified。"""
        prepared = make_prepared(self.root)
        policy = dict(POLICY, max_guidance_model_calls=1)
        arm_dir = prepared / "input-002/M"
        meta = json.loads((arm_dir / "input.json").read_text(encoding="utf-8"))
        meta["policy"] = policy
        (arm_dir / "input.json").write_text(json.dumps(meta, ensure_ascii=False),
                                            encoding="utf-8")
        adapter = FakeAdapter(behavior={"first": "good"})
        gateway = FakeGateway([DIAGNOSIS_GENERIC, BLIND_AFTER_X, BLIND_AFTER_Y])
        run = make_run(self.root, prepared, "input-002/M", adapter, gateway)
        run._blind_rng = _FixedCoin()
        with self.patch_admission(PASSING_ISO):
            manifest = run.run()
        self.assertEqual(manifest["summary"]["verdict"], "unverified")
        self.assertIn("预算耗尽", manifest["summary"]["verdict_basis"])
        self.assertEqual(manifest["budget"]["used"]["model_calls"], 1)  # 盲比被预算拦截

    def test_admission_failure_blocks_all_dispatch(self):
        """准入 fail-closed：未通过 → AdmissionError，执行器零调用、无 manifest。"""
        prepared = make_prepared(self.root)
        adapter = FakeAdapter(behavior={"first": "good"})
        run = make_run(self.root, prepared, "input-002/B-1", adapter, FakeGateway([]))
        with self.patch_admission(FAILING_ISO):
            with self.assertRaises(AdmissionError):
                run.run()
        self.assertEqual(adapter.calls, [])
        self.assertFalse((run.run_dir / "run-manifest.json").exists())

    def test_arms_get_identical_flattened_package(self):
        """公平条件：两臂工作区展开逐字节一致（除方法提示词语义由各自臂处理）。"""
        prepared = make_prepared(self.root)
        # M 包与 B 包同内容（夹具只建一份；展开规则相同即逐字节一致）
        ws_a = self.root / "flat-a"
        ws_b = self.root / "flat-b"
        from mentis.eval_runner import _flatten_package

        _flatten_package(prepared / "input-002/B-1", ws_a)
        _flatten_package(prepared / "input-002/M", ws_b)
        def digest(ws: Path) -> str:
            import hashlib

            parts = []
            for p in sorted(ws.rglob("*")):
                if p.is_file():
                    parts.append(f"{p.relative_to(ws)}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
            return "|".join(parts)
        self.assertEqual(digest(ws_a), digest(ws_b))


if __name__ == "__main__":
    unittest.main()
