"""E2 准备回归：离线模型/执行器/准入替身；Git 文件与检查点是真实文件操作。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mentis.contracts import AttemptStatus
from mentis.eval_readiness import (PROTOCOL, ReadinessError, freeze_spec, guard_rates,
                                   material_state, sha, summarize_runs, validate_spec, write_json)
from mentis.eval_runner import AdmissionError, FairRun
try:
    from tests.test_eval_runner import (COPIED_DECK, PASSING_ISO, POLICY, RecordingAdapter,
                                       make_prepared, make_run)
    from tests.test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from tests.test_guidance import (DIAGNOSIS_GENERIC, DIAGNOSIS_QUALIFIED,
                                    BLIND_AFTER_X, BLIND_AFTER_Y, FakeGateway,
                                    _FixedCoin, file_aware_measure)
except ImportError:
    from test_eval_runner import (COPIED_DECK, PASSING_ISO, POLICY, RecordingAdapter,
                                 make_prepared, make_run)
    from test_collab import GOOD_PAGE, FakeAdapter, stub_capture
    from test_guidance import (DIAGNOSIS_GENERIC, DIAGNOSIS_QUALIFIED,
                              BLIND_AFTER_X, BLIND_AFTER_Y, FakeGateway,
                              _FixedCoin, file_aware_measure)


class DatesAdapter(RecordingAdapter):
    def _run(self, spec, status_override=None):
        result = super()._run(spec, status_override)
        effective = Path(spec.workspace) / "input" / "copy-effective.md"
        if effective.is_file() and "2027-05-14" in effective.read_text(encoding="utf-8"):
            (Path(spec.workspace) / "index.html").write_text(
                GOOD_PAGE.replace("2026-11-14", "2027-05-14"), encoding="utf-8")
        return result


class UnknownAdapter(RecordingAdapter):
    def _run(self, spec, status_override=None):
        result = super()._run(spec, status_override)
        result.status = AttemptStatus.UNKNOWN
        return result  # Fake 保留会话 id；真实超时必须有 thread.started 才能续办。


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.admission = patch("mentis.eval_runner.check_admission", return_value=PASSING_ISO)
        self.admission.start()
        self.addCleanup(self.admission.stop)

    def decisions(self, context=()):
        prepared = make_prepared(self.root, with_draft=False)
        for arm in ("M", "B-1"):
            folder = prepared / "input-002" / arm
            copy = folder / "input" / "copy.md"
            copy.write_text(COPIED_DECK.replace("2026-11-14", "2027-05-07"), encoding="utf-8")
            copy.rename(copy.with_name("copy-a.md"))
            (folder / "input" / "copy-b.md").write_text(
                COPIED_DECK.replace("2026-11-14", "2027-05-14"), encoding="utf-8")
            meta_path = folder / "input.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["input"].update(user_feedback="", conversation_context=list(context))
            write_json(meta_path, meta)
        return prepared

    def reopen(self, run, adapter=None, responses=()):
        return FairRun.reopen(run.run_dir, adapter=adapter or DatesAdapter(),
                              gateway=FakeGateway(list(responses)), browser_capture=stub_capture,
                              measure=file_aware_measure)

    def test_missing_date_waits_before_first_write_and_answer_is_late(self):
        prepared = self.decisions()
        adapter = DatesAdapter()
        run = make_run(self.root, prepared, "input-002/M", adapter, FakeGateway([DIAGNOSIS_QUALIFIED]))
        first = run.run()
        self.assertEqual(first["runner_status"], "needs_user")
        self.assertEqual(first["budget"]["used"]["attempts"], 0)
        self.assertEqual(adapter.full_prompts, [])
        self.assertFalse(first["artifact"]["exists"])
        resumed_adapter = DatesAdapter()
        resumed = self.reopen(run, resumed_adapter, [DIAGNOSIS_QUALIFIED])
        result = resumed.answer("以2027-05-14 14:00—16:00为准", "测试控制器")
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["budget"]["used"]["attempts"], 1)
        self.assertEqual(result["controlled_answers"][0]["source"], "controlled_simulation")
        self.assertIn("2027-05-14", resumed_adapter.full_prompts[0])
        self.assertIn("2027-05-07", (resumed.workspace / "input/copy-a.md").read_text(encoding="utf-8"))
        history = json.loads((run.run_dir / "history/manifest-001.json").read_text(encoding="utf-8"))
        self.assertEqual(history["controlled_answers"], [])

    def test_given_decision_is_reused_without_new_question(self):
        prepared = self.decisions(["活动日期以我最后确认的2027-05-14 14:00—16:00为准，其他事实保持原资料。"])
        adapter = DatesAdapter()
        run = make_run(self.root, prepared, "input-002/M", adapter, FakeGateway([DIAGNOSIS_QUALIFIED]))
        result = run.run()
        self.assertEqual(result["runner_status"], "finished")
        self.assertEqual(result["controlled_answers"], [])
        self.assertEqual(result["decisions"][0]["source"], "conversation_context")
        self.assertIn("2027-05-14", Path(result["artifact"]["path"]).read_text(encoding="utf-8"))

    def test_answer_cannot_be_injected_before_request_or_with_ambiguous_date(self):
        prepared = self.decisions()
        run = make_run(self.root, prepared, "input-002/M", DatesAdapter(), FakeGateway([]))
        with self.assertRaises(AdmissionError):
            run.answer("以2027-05-14为准", "测试控制器")
        run.run()
        with self.assertRaises(AdmissionError):
            self.reopen(run).answer("2027-05-07 或2027-05-14都可以", "测试控制器")

    def test_baseline_gets_same_controlled_date_after_asking(self):
        prepared = self.decisions()
        class Asking(DatesAdapter):
            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                (Path(spec.workspace) / "QUESTIONS.md").write_text("Q: 两份资料日期冲突，以哪个为准？", encoding="utf-8")
                return result
        run = make_run(self.root, prepared, "input-002/B-1", Asking(), FakeGateway([]))
        first = run.run()
        self.assertEqual(first["runner_status"], "needs_user")
        adapter = DatesAdapter()
        # 已有会话id相同；假执行器在收到回答后删问题。
        original = adapter._run
        def clear(spec, status_override=None):
            result = original(spec, status_override)
            (Path(spec.workspace) / "QUESTIONS.md").unlink(missing_ok=True)
            return result
        adapter._run = clear
        result = self.reopen(run, adapter).answer("以2027-05-14 14:00—16:00为准", "测试控制器")
        self.assertEqual(result["budget"]["used"]["attempts"], 2)
        self.assertIn("2027-05-14", adapter.full_prompts[0])

    def test_baseline_existing_date_context_is_sent(self):
        prepared = self.decisions(["以我确认的2027-05-14 14:00—16:00为准"])
        adapter = DatesAdapter()
        run = make_run(self.root, prepared, "input-002/B-1", adapter, FakeGateway([]))
        run.run()
        self.assertIn("以我确认的2027-05-14", adapter.full_prompts[0])

    def test_design_question_does_not_receive_reserved_date_answer(self):
        prepared = self.decisions()
        class Questions(DatesAdapter):
            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                question = "Q: 页面选什么颜色？" if len(self.calls) == 1 else "Q: 两份日期冲突，以哪个为准？"
                (Path(spec.workspace) / "QUESTIONS.md").write_text(question, encoding="utf-8")
                return result
        adapter = Questions()
        run = make_run(self.root, prepared, "input-002/B-1", adapter, FakeGateway([]))
        result = run.run()
        self.assertEqual(result["budget"]["used"]["attempts"], 2)
        self.assertEqual(result["runner_status"], "needs_user")
        self.assertIn("日期", result["summary"]["pending"]["question"])
        self.assertEqual(result["controlled_answers"], [])
        self.assertIn("自主决定", adapter.full_prompts[1])
        self.assertNotIn("2027-05-14", adapter.full_prompts[1])

    def unknown_run(self, arm="input-002/B-1", responses=()):
        prepared = make_prepared(self.root)
        run = make_run(self.root, prepared, arm, UnknownAdapter(), FakeGateway(list(responses)))
        run._blind_rng = _FixedCoin()
        run.run()
        return run

    def test_checked_continue_preserves_original_unknown_and_budget(self):
        run = self.unknown_run()
        adapter = DatesAdapter()
        resumed = self.reopen(run, adapter)
        result = resumed.checked_continue("核对者", "查看进程和作品", process_check=lambda _: [])
        self.assertEqual(result["budget"]["used"]["attempts"], 2)
        self.assertEqual(result["codex_usage"][0]["status"], "unknown")
        self.assertEqual(result["codex_usage"][1]["status"], "completed")
        self.assertEqual(result["checked_continuations"], 1)
        self.assertTrue(result["reconciliation"][0]["workspace_hashes"])
        self.assertEqual(len(adapter.calls), 1)
        with self.assertRaises(AdmissionError):
            resumed.checked_continue("核对者", "第二次", process_check=lambda _: [])

    def test_active_or_unavailable_process_check_blocks_dispatch(self):
        run = self.unknown_run()
        adapter = DatesAdapter()
        resumed = self.reopen(run, adapter)
        for processes in (None, [{"pid": 123}]):
            with self.assertRaises(AdmissionError):
                resumed.checked_continue("核对者", "进程未停止", process_check=lambda _, p=processes: p)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(resumed.budget.used["attempts"], 1)

    def test_artifact_check_keeps_unknown_and_never_dispatches(self):
        run = self.unknown_run()
        adapter = DatesAdapter()
        result = self.reopen(run, adapter).checked_continue(
            "核对者", "已含预期改动", artifact_complete=True, process_check=lambda _: [])
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["summary"]["artifact_state"], "unknown_artifact_checked")
        self.assertEqual(result["budget"]["used"]["attempts"], 1)
        self.assertEqual(result["checked_continuations"], 0)
        self.assertEqual(adapter.calls, [])

    def test_second_unknown_terminates_and_rejects_another_continuation(self):
        run = self.unknown_run()
        resumed = self.reopen(run, UnknownAdapter())
        result = resumed.checked_continue("核对者", "继续剩余工作", process_check=lambda _: [])
        self.assertEqual(result["execution_status"], "unknown")
        with self.assertRaises(AdmissionError):
            self.reopen(resumed).checked_continue("核对者", "再试", process_check=lambda _: [])

    def test_question_pending_unknown_reconciles_to_needs_user_without_delegation(self):
        """真实运行发现的缺口（2026-10-04，E1-04/B-1）：决定样本中执行器构建前
        正确停止提问，但 turn 因失败子命令被保守判 unknown、工作区无产物——
        原续办机制要求 index.html 而拒绝。修复后：核对续办不重发委托、不计续办
        次数，按工作区 QUESTIONS.md 转 needs_user；回答仍只能经 eval-answer 注入。"""

        class AsksDateThenBuilds(RecordingAdapter):
            def _run(self, spec, status_override=None):
                result = super()._run(spec, status_override)
                ws = Path(spec.workspace)
                if len(self.calls) == 1:
                    result.status = AttemptStatus.UNKNOWN  # 失败子命令→保守 unknown 的模拟
                    (ws / "QUESTIONS.md").write_text(
                        "# 待确认：活动日期\n\ninput/copy-a.md 写 2027-05-07，"
                        "input/copy-b.md 写 2027-05-14，请确认以哪一天为准。",
                        encoding="utf-8")
                    (ws / "index.html").unlink()  # 构建前正确停止：无产物
                else:
                    (ws / "QUESTIONS.md").unlink()
                    (ws / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
                return result

        adapter = AsksDateThenBuilds()
        run = make_run(self.root, self.decisions(), "input-002/B-1", adapter, FakeGateway([]))
        run.run()
        self.assertEqual(run.state["status"], "unknown")
        resumed = self.reopen(run, adapter)
        result = resumed.checked_continue(
            "核对者", "执行器写 QUESTIONS.md 后 62s 停止；工作区无产物（问题待决）",
            process_check=lambda _: [])
        self.assertEqual(result["runner_status"], "needs_user")
        self.assertEqual(result["budget"]["used"]["attempts"], 1)  # 零新委托
        self.assertEqual(result["checked_continuations"], 0)       # 不计续办次数
        self.assertIn("日期", result["summary"]["pending"]["question"])
        self.assertEqual(len(adapter.calls), 1)
        answered = self.reopen(resumed, adapter).answer(
            "以2027-05-14 14:00—16:00为准", "控制器", "controlled_simulation")
        self.assertEqual(answered["budget"]["used"]["attempts"], 2)
        self.assertIn("2027-05-14", adapter.full_prompts[-1])
        self.assertEqual(answered["runner_status"], "finished")

    def test_m_revision_continuation_does_not_replan_or_rediagnose(self):
        run = self.unknown_run("input-002/M", [DIAGNOSIS_GENERIC])
        resumed = self.reopen(run, DatesAdapter(), [BLIND_AFTER_X, BLIND_AFTER_Y])
        resumed._blind_rng = _FixedCoin()
        result = resumed.checked_continue("核对者", "原修订尚需复检", process_check=lambda _: [])
        self.assertEqual(result["budget"]["used"], {"attempts": 2, "revisions": 1, "model_calls": 3})
        self.assertEqual(result["model_requests"]["total"], 4)  # 计划、诊断、两序
        self.assertNotIn("error_type", result["summary"])

    def test_tampered_checkpoint_or_plan_blocks_resumption(self):
        run = self.unknown_run("input-002/M", [DIAGNOSIS_GENERIC])
        state = run.run_dir / "runner-state.json"
        original = state.read_bytes()
        state.write_bytes(original + b" ")
        with self.assertRaises(AdmissionError):
            self.reopen(run)
        state.write_bytes(original)
        plan = run.run_dir / "evaluation-plan/plan-v1.json"
        plan.write_bytes(plan.read_bytes() + b" ")
        adapter = DatesAdapter()
        with self.assertRaises(AdmissionError):
            self.reopen(run, adapter).checked_continue("核对者", "检查", process_check=lambda _: [])
        self.assertEqual(adapter.calls, [])

    def test_observer_exception_has_manifest_and_trace(self):
        prepared = make_prepared(self.root)
        run = make_run(self.root, prepared, "input-002/M", DatesAdapter(), FakeGateway([]))
        def unavailable(*_):
            raise OSError("observer unavailable")
        run.measure = unavailable
        result = run.run()
        self.assertEqual(result["runner_status"], "error")
        self.assertEqual(result["summary"]["verdict"], "unverified")
        self.assertTrue((run.run_dir / "trace/manifest.json").is_file())

    def test_invalid_spec_blocks_before_executor_dispatch(self):
        prepared = make_prepared(self.root)
        adapter = DatesAdapter()
        run = make_run(self.root, prepared, "input-002/M", adapter, FakeGateway([]))
        run.run_spec = str(self.root / "missing-spec.json")
        with self.assertRaises(AdmissionError):
            run.run()
        self.assertEqual(adapter.calls, [])

    def test_commit_and_protocol_binding_are_sent_to_admission(self):
        prepared = make_prepared(self.root)
        run = make_run(self.root, prepared, "input-002/B-1", DatesAdapter(), FakeGateway([]))
        run.run_spec = str(self.root / "contract.json")
        expected = {"code_commit": "c" * 40, "protocol_version": "v1.1-frozen", "spec_sha256": "a" * 64}
        with patch("mentis.eval_runner.binding", return_value=expected), \
             patch("mentis.eval_runner.check_admission", return_value=PASSING_ISO) as admission:
            result = run.run()
        self.assertEqual(admission.call_args.kwargs["expected"]["execution_binding"], expected)
        self.assertEqual(result["execution_binding"], expected)

    def test_out_directory_is_used_and_existing_run_cannot_be_overwritten(self):
        prepared = make_prepared(self.root)
        run = FairRun(prepared, "input-002/B-1", self.root / "requested-out", "a" * 64,
                      self.root / "evidence.json", adapter=DatesAdapter(), gateway=FakeGateway([]), run_id="fixed")
        run.run()
        self.assertEqual(run.run_dir, self.root / "requested-out/fixed")
        again = FairRun(prepared, "input-002/B-1", self.root / "requested-out", "a" * 64,
                        self.root / "evidence.json", adapter=DatesAdapter(), gateway=FakeGateway([]), run_id="fixed")
        with self.assertRaises(AdmissionError):
            again.run()

    def test_open_window_snapshot_survives_release_log_update(self):
        prepared = make_prepared(self.root)
        run = make_run(self.root, prepared, "input-002/B-1", DatesAdapter(), FakeGateway([]))
        recovery = self.root / "window.json"
        write_json(recovery, {"window_id": "fixture", "released_at": ""})
        original = recovery.read_bytes()
        write_json(run.evidence_path, {"window": {"recovery_log": str(recovery)},
                                     "raw_hashes": {"recovery-log.json": sha(recovery)}})
        result = run.run()
        write_json(recovery, {"window_id": "fixture", "released_at": "after_run"})
        self.assertEqual(Path(result["isolation"]["recovery_snapshot"]).read_bytes(), original)


class ContractAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fake_repo(self):
        (self.root / "mentis").mkdir()
        (self.root / "mentis/method.py").write_text("VERSION=1\n", encoding="utf-8")
        protocol = self.root / PROTOCOL
        protocol.parent.mkdir(parents=True)
        protocol.write_text("E2 v1.1-frozen\n", encoding="utf-8")
        suite = self.root / "suite.json"
        suite.write_text("{}", encoding="utf-8")
        for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-qm", "baseline"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        return suite

    def test_clean_commit_freezes_and_code_or_protocol_drift_is_rejected(self):
        suite = self.fake_repo()
        spec_path = self.root.parent / (self.root.name + "-spec.json")
        self.addCleanup(spec_path.unlink, missing_ok=True)
        freeze_spec(spec_path, suite, self.root)
        validate_spec(spec_path, sha(suite), self.root)
        code = self.root / "mentis/method.py"
        code.write_text("VERSION=2\n", encoding="utf-8")
        with self.assertRaises(ReadinessError):
            validate_spec(spec_path, sha(suite), self.root)
        code.write_text("VERSION=1\n", encoding="utf-8")
        (self.root / PROTOCOL).write_text("v1.2-frozen", encoding="utf-8")
        with self.assertRaises(ReadinessError):
            validate_spec(spec_path, sha(suite), self.root)

    def test_dirty_tree_refuses_freeze(self):
        suite = self.fake_repo()
        (self.root / "mentis/method.py").write_text("dirty", encoding="utf-8")
        with self.assertRaises(ReadinessError):
            freeze_spec(self.root / "run-spec.json", suite, self.root)

    def test_worktree_git_pointer_and_common_metadata_are_both_protected(self):
        from mentis import eval_isolation as isolation

        self.fake_repo()
        suite = self.root / "docs/evaluation/e1/suite.json"
        suite.parent.mkdir(parents=True)
        suite.write_text("{}", encoding="utf-8")
        for args in (["add", "."], ["-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-qm", "suite"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        linked = self.root / "linked-worktree"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(isolation._repo_root_for(linked / "docs/evaluation/e1/suite.json"), linked.resolve())
        roots = isolation._git_metadata_roots(linked)
        self.assertEqual(roots, [(linked / ".git").resolve(), (self.root / ".git").resolve()])
        targets = isolation.build_probe_targets([], linked / "docs/evaluation/e1/suite.json")
        self.assertEqual(targets[-1]["path"], str((self.root / ".git/config").resolve()))

    def test_twenty_rows_and_unknown_artifact_are_exported_without_process_labels(self):
        suite = Path(__file__).resolve().parents[1] / "docs/evaluation/e1/suite.json"
        run_dir = self.root / "runs" / "one"
        run_dir.mkdir(parents=True)
        artifact = run_dir / "final.html"
        artifact.write_text(GOOD_PAGE, encoding="utf-8")
        write_json(run_dir / "run-manifest.json", {"run_id": "one", "arm": "input-002/M",
            "suite_sha256": sha(suite), "execution_status": "unknown", "budget": {"used": {"attempts": 1, "model_calls": 2}},
            "artifact": {"path": str(artifact), "exists": True, "sha256": sha(artifact)},
            "summary": {"verdict": "unverified", "candidates": [{"decision": "reject", "guard": "overflow"}]}})
        out = self.root / "export"
        result = summarize_runs(self.root / "runs", suite, out)
        self.assertEqual(result["expected_arms"], 20)
        self.assertEqual(result["recorded_arms"], 1)
        self.assertEqual(len(result["missing_arms"]), 19)
        self.assertIsNone(result["quality_conclusion"])
        blind = list((out / "blind-artifacts").glob("*.html"))
        self.assertEqual(len(blind), 1)
        self.assertEqual(blind[0].read_text(encoding="utf-8"), GOOD_PAGE)
        self.assertNotIn("M", blind[0].stem)
        task_card = json.loads(next((out / "blind-artifacts").glob("*-task.json")).read_text(encoding="utf-8"))
        self.assertEqual(set(task_card), {"goal", "public_materials", "public_context"})
        rates = guard_rates(out / "guard-review.json")
        self.assertIsNone(rates["false_suppression_rate"])
        self.assertEqual(rates["missing_labels"], 1)

    def test_guard_rate_requires_reviewer_and_basis(self):
        review = self.root / "guard.json"
        write_json(review, {"candidates": [
            {"decision": "reject", "guard": "overflow", "human_should_adopt": True, "reviewer": "A", "basis": "可映射且真实溢出"},
            {"decision": "pending", "guard": "overflow", "human_should_adopt": False, "reviewer": "A", "basis": "观察不可信"},
            {"decision": "plan_blind_spot", "human_should_adopt": True}]})
        rates = guard_rates(review)
        self.assertEqual(rates["missing_labels"], 1)
        self.assertEqual(rates["false_suppression_rate"], 0.5)
        self.assertEqual(rates["overflow_false_suppression_rate"], 0.5)

    def test_powershell_module_path_is_not_inherited_across_versions(self):
        from mentis.eval_isolation import _native_powershell_env, _codex_processes_since

        with patch.dict(os.environ, {"PSMODULEPATH": "test-pwsh7-path", "MENTIS_TEST_FLAG": "retained"}):
            child = _native_powershell_env()
        self.assertFalse(any(k.casefold() == "psmodulepath" for k in child))
        self.assertEqual(child["MENTIS_TEST_FLAG"], "retained")
        with patch("mentis.eval_isolation.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            self.assertIsNone(_codex_processes_since("2027-05-07T12:00:00+00:00"))

    def test_missing_credentials_and_injected_transport_do_not_count_as_http(self):
        from mentis.models.gateway import ModelConfig, ModelGateway

        with patch.dict(os.environ, {"MENTIS_TEST_MISSING_KEY": ""}):
            gateway = ModelGateway(ModelConfig(api_key_env="MENTIS_TEST_MISSING_KEY"))
            record = gateway.complete("system", "prompt")
        self.assertFalse(record.http_attempted)
        gateway = ModelGateway(transport=lambda *_: "{}")
        self.assertFalse(gateway.complete("system", "prompt").http_attempted)

    def test_release_cli_does_not_restore_answers_while_writer_may_be_active(self):
        from types import SimpleNamespace
        from mentis import eval_isolation as isolation

        prepared = self.root / "prepared"
        window = prepared / "isolation/window-test/window.json"
        write_json(window, {"window_id": "test", "created_at": "2027-05-07T12:00:00+00:00",
                            "window_dir": str(window.parent)})
        args = SimpleNamespace(action="release", prepared=str(prepared), suite="unused")
        for alive in (None, [{"pid": 123}]):
            with patch.object(isolation, "_codex_processes_since", return_value=alive), \
                 patch.object(isolation, "release_window") as release:
                result = isolation.eval_isolate_cli(args)
            self.assertFalse(result["ok"])
            self.assertTrue(result["window_kept"])
            release.assert_not_called()
