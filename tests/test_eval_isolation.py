"""E1 隔离模块离线测试：ACL 事务窗口 / 确定性探针 / 强绑定准入。

Windows 真实 ACL 用例只使用 %TEMP% 直下临时目录（普通 ACL 世系，可恢复），
不触碰仓库受保护材料；非 Windows 环境自动跳过。夹具证明本模块逻辑；
真实双端探测由 eval-isolate probe 在真实环境另行执行（本轮一次真实核验）。
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from mentis import eval_isolation as iso
from mentis.evaluation import audit_suite, prepare_suite

NT = os.name == "nt"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc(dt_iso: str) -> datetime:
    return datetime.fromisoformat(dt_iso)


class IsolationFixture(unittest.TestCase):
    """共享夹具：最小有效 suite + 真实 prepare_suite 导出（普通 ACL 世系）。"""

    def setUp(self):
        self.root = (Path(tempfile.gettempdir()) / f"mentis-eval-iso-{uuid4().hex[:10]}")
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # 兜底：测试中途出错遗留的窗口按恢复日志事务回滚，不把 deny 留在磁盘上
        def _release_local_windows():
            for log_path in sorted(self.root.glob("**/window-*/window.json")):
                try:
                    log = json.loads(log_path.read_text(encoding="utf-8"))
                    log.setdefault("window_dir", str(log_path.parent))
                    iso.release_window(log)
                except Exception:
                    pass
        self.addCleanup(_release_local_windows)
        self.suite_dir = self.root / "freeze"
        (self.suite_dir / "samples").mkdir(parents=True)
        (self.suite_dir / "prompts").mkdir()
        (self.suite_dir / "reviewer.json").write_bytes(b"SECRET_AUTHOR_EXPECTED")
        prompts = {}
        for arm in ["B-0", "B-1", "B-2", "B-3", "M"]:
            raw = f"{arm} 提示词".encode("utf-8")
            (self.suite_dir / "prompts" / f"{arm}.md").write_bytes(raw)
            prompts[arm] = {"source": f"prompts/{arm}.md", "sha256": _sha_entry(raw)}
        page = b"<h1>page</h1>"
        (self.suite_dir / "samples" / "page.html").write_bytes(page)
        suite = {
            "schema_version": 1, "suite_id": "e1-dev-test", "status": "exploratory_frozen",
            "common": {"files": [], "policy": {"network": "none"}},
            "samples": [{
                "sample_id": "S1", "kind": "feedback", "review_status": "pending_human",
                "input": {"user_goal": "目标", "user_feedback": "",
                          "feedback_source": "none", "conversation_context": []},
                "files": [{"source": "samples/page.html", "target": "workspace/index.html",
                           "sha256": _sha_entry(page)}],
                "observations_missing": []}],
            "treatments": {"feedback": ["B-1", "B-2", "B-3", "M"],
                           "end_to_end": ["B-0", "B-1", "B-2", "M"],
                           "decision": ["B-1", "B-2", "B-3", "M"]},
            "prompts": prompts,
            "isolation": {"status": "unverified", "note": "夹具"},
        }
        self.suite_path = self.suite_dir / "suite.json"
        self.suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
        self.prepared = self.root / "prepared"
        prepare_suite(self.suite_path, self.prepared)
        self.protected = iso.protected_manifest(self.suite_path, self.prepared, "input-001/M")
        # 套件目录 + prepare-report + 三个非目标组（夹具不在仓库布局内，无 .git 目标）
        self.assertEqual(len(self.protected), 1 + 1 + 3)

    def probe_targets(self):
        return iso.build_probe_targets(
            iso.verification_targets(self.protected), self.suite_path)

    def fake_codex_result(self, workspace: Path, probe_targets=None) -> dict:
        """构造一份与真实探针输出结构一致的 Codex 端结果（全部 denied）。

        必须用传入的 probe_targets 构造：窗口开启后枚举受保护目录会失败。
        """
        targets = probe_targets if probe_targets is not None else []
        entries = []
        for t in targets:
            if t.get("kind") == "git_history":
                entries.append({"id": t["id"], "form": "git_show", "outcome": "denied",
                                "error": "fatal: unable to access"})
                continue
            for form in ("absolute", "parent_traversal", "junction"):
                entries.append({"id": t["id"], "form": form, "outcome": "denied",
                                "error": "PermissionError winerror=5"})
        input_sha = hashlib.sha256((self.prepared / "input-001" / "M" / "input.json")
                                   .read_bytes()).hexdigest()
        return {"schema": iso.PROBE_SCHEMA, "probe_version": iso.PROBE_VERSION,
                "workspace": str(workspace), "cwd": str(workspace), "user_env": "WY",
                "entries": entries,
                "positive": {"input.json": {"readable": True, "bytes": 10, "sha256": input_sha},
                             "prompt.md": {"readable": True, "bytes": 8,
                                           "sha256": "a" * 64},
                             "workspace_write": {"ok": True, "sha256": "b" * 64}}}


def _sha_entry(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ManifestTests(IsolationFixture):
    def test_manifest_rejects_unknown_arm(self):
        with self.assertRaisesRegex(iso.IsolationError, "不在 prepare-report"):
            iso.protected_manifest(self.suite_path, self.prepared, "input-009/Z")

    def test_manifest_rejects_path_escape_and_absolute_from_prepare_report(self):
        report_path = self.prepared / "prepare-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for bad in ["../outside", "C:/outside", "input-001/../../outside", ""]:
            with self.subTest(bad=bad):
                tampered = json.loads(json.dumps(report))
                tampered["packages"][0]["arms"][1]["path"] = bad
                report_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaises(iso.IsolationError):
                    iso.protected_manifest(self.suite_path, self.prepared, "input-001/M")
                report_path.write_text(json.dumps(report), encoding="utf-8")
        # 恢复后正常
        iso.protected_manifest(self.suite_path, self.prepared, "input-001/M")

    def test_historical_exports_and_git_included_by_layout(self):
        old_export = self.root / "old-export"
        old_export.mkdir()
        (old_export / "prepare-report.json").write_bytes(b"{}")
        stray = self.root / "stray-dir"
        stray.mkdir()
        protected = iso.protected_manifest(self.suite_path, self.prepared, "input-001/M")
        self.assertIn(old_export.resolve(), protected)
        self.assertNotIn(stray.resolve(), protected)
        # 夹具不在 docs/evaluation 布局内：不产生 .git 目标
        self.assertNotIn((self.root / ".git").resolve(), protected)

    def test_git_targets_only_for_repo_layout(self):
        # 夹具：无 git 目标
        self.assertEqual([t for t in self.probe_targets() if t["id"].startswith("GIT")], [])

    def test_verification_targets_prefer_sensitive_files(self):
        targets = iso.verification_targets(self.protected)
        probe_names = {Path(t["probe"]).name for t in targets}
        self.assertIn("reviewer.json", probe_names)
        self.assertIn("input.json", probe_names)
        for target in targets:
            self.assertEqual(target["sha256_at_apply"], _sha(Path(target["probe"])))


@unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
class WindowTransactionTests(IsolationFixture):
    def test_open_deny_release_restores_exact_original_acl(self):
        targets = iso.verification_targets(self.protected)
        log = iso.open_window(self.protected, self.root / "iso")
        try:
            for t in log["verification_targets"]:
                with self.assertRaises(PermissionError):
                    Path(t["probe"]).read_bytes()
            self.assertTrue((iso.WINDOW_LOCK_DIR / f"window-{log['window_id']}.json").is_file())
        finally:
            outcome = iso.release_window(log)
        self.assertTrue(outcome["verified"], outcome)
        for t in log["verification_targets"]:
            self.assertTrue(iso._readable(Path(t["probe"])), t["probe"])
        # 逐项 dump 与捕获原档一致（事务恢复，不是 /remove:d 近似）
        for target in log["targets"]:
            self.assertEqual(iso._normalize_dump(iso._icacls_dump(Path(target["path"]))),
                             iso._normalize_dump(target["dump"]))
        self.assertFalse((iso.WINDOW_LOCK_DIR / f"window-{log['window_id']}.json").exists())

    def test_exact_restore_beyond_remove_d(self):
        """原 ACL 含第三方 deny ACE 时，仅 /remove:d 会过度移除；事务恢复必须保真。"""
        victim = self.prepared / "input-001" / "B-1"
        subprocess.run(["icacls", str(victim), "/deny", "CodexSandboxOnline:F"],
                       capture_output=True)
        original_dump = iso._normalize_dump(iso._icacls_dump(victim))
        self.assertIn("CodexSandboxOnline", original_dump)
        log = iso.open_window(self.protected, self.root / "iso")
        outcome = iso.release_window(log)
        self.assertTrue(outcome["verified"], outcome)
        restored_dump = iso._normalize_dump(iso._icacls_dump(victim))
        self.assertEqual(original_dump, restored_dump,
                         "事务恢复必须保留窗口前已存在的第三方 deny ACE")
        subprocess.run(["icacls", str(victim), "/remove:d", "CodexSandboxOnline"],
                       capture_output=True)

    def test_atomic_log_failure_blocks_window(self):
        with mock.patch.object(iso, "_write_atomic", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(iso.IsolationError, "恢复日志写入失败"):
                iso.open_window(self.protected, self.root / "iso")
        for target in iso.verification_targets(self.protected):
            self.assertTrue(iso._readable(Path(target["probe"])), target["probe"])
        self.assertFalse(list(iso.WINDOW_LOCK_DIR.glob("window-*.json")))

    def test_partial_deny_failure_rolls_back_everything(self):
        real_icacls = iso._icacls

        def flaky(args):
            # 只对真实目标（套件目录）的 deny 注入失败；金丝雀的 deny 正常执行。
            # 排练阶段即会 deny 真实目标，因此失败在排练中触发：
            # 未开锁、未正式开窗，且全部目标已按原档恢复。
            if "/deny" in [str(a) for a in args] and "freeze" in str(args[0]):
                raise iso.IsolationError("icacls 注入失败")
            return real_icacls(args)

        with mock.patch.object(iso, "_icacls", side_effect=flaky):
            with self.assertRaisesRegex(iso.IsolationError, "排练失败"):
                iso.open_window(self.protected, self.root / "iso")
        for target in iso.verification_targets(self.protected):
            self.assertTrue(iso._readable(Path(target["probe"])), "部分失败必须全部回滚")
        self.assertFalse(list(iso.WINDOW_LOCK_DIR.glob("window-*.json")))

    def test_mutual_exclusion_on_overlapping_scope(self):
        log = iso.open_window(self.protected, self.root / "iso")
        try:
            with self.assertRaisesRegex(iso.IsolationError, "活动隔离窗口"):
                iso.open_window(self.protected, self.root / "iso2")
        finally:
            iso.release_window(log)
        # 释放后可再次开窗
        log2 = iso.open_window(self.protected, self.root / "iso")
        iso.release_window(log2)

    def test_restore_failure_reported_per_item_never_claims_restored(self):
        """恢复依据损坏 + 原档含第三方 deny：兜底 /remove:d 过度移除必须如实报告。"""
        victim = self.prepared / "input-001" / "B-1"
        subprocess.run(["icacls", str(victim), "/deny", "CodexSandboxOnline:F"],
                       capture_output=True)
        original_dump = iso._normalize_dump(iso._icacls_dump(victim))
        try:
            log = iso.open_window(self.protected, self.root / "iso")
            xml = next(Path(log["window_dir"]) / t["acl_xml"] for t in log["targets"]
                       if t["path"] == str(victim.resolve()))
            xml.unlink()
            outcome = iso.release_window(log)
        finally:
            subprocess.run(["icacls", str(victim), "/remove:d", "CodexSandboxOnline",
                            "desktop-2f92t1a\\wy", "CodexSandboxUsers",
                            "CodexSandboxOffline"], capture_output=True)
        self.assertFalse(outcome["verified"])
        failed_paths = {entry["path"] for entry in outcome["failed"]}
        self.assertIn(str(victim.resolve()), failed_paths)
        self.assertTrue(all(entry["detail"] for entry in outcome["failed"]))
        self.assertNotEqual(original_dump, "")

    def test_post_failure_gates_release_on_executor_alive(self):
        log = iso.open_window(self.protected, self.root / "iso")
        try:
            with mock.patch.object(iso, "_codex_processes_since", return_value=None):
                outcome = iso._post_failure(log, None, self.root / "p")
            self.assertTrue(outcome["window_kept"])
            with self.assertRaises(PermissionError):
                (self.suite_dir / "reviewer.json").read_bytes()
            with mock.patch.object(iso, "_codex_processes_since", return_value=[]):
                outcome = iso._post_failure(log, None, self.root / "p")
            self.assertFalse(outcome["window_kept"])
            self.assertTrue(iso._readable(self.suite_dir / "reviewer.json"))
        except AssertionError:
            iso.release_window(log)
            raise


class ProbeTests(IsolationFixture):
    def _write_workspace(self, targets):
        workspace = self.root / "ws"
        (workspace / "probe-codex").mkdir(parents=True)
        (workspace / "input.json").write_bytes(b'{"arm":"input-001/M"}')
        (workspace / "prompt.md").write_bytes(b"prompt!!")
        (workspace / "probe-targets.json").write_text(
            json.dumps(targets, ensure_ascii=False), encoding="utf-8-sig")
        return workspace

    def test_runner_classifies_missing_file_and_unreadable_target(self):
        input_sha = hashlib.sha256(b'{"arm":"input-001/M"}').hexdigest()
        targets = [{"id": "OK1", "kind": "file",
                    "path": str(self.root / "allowed.txt")}]
        (self.root / "allowed.txt").write_bytes(b"allowed-bytes")
        targets.append({"id": "MISS1", "kind": "file",
                        "path": str(self.root / "ghost" / "missing.txt")})
        workspace = self._write_workspace(targets)
        runner = self.root / "probe_runner.py"
        runner.write_text(iso._PROBE_RUNNER, encoding="utf-8")
        out = iso.run_deterministic_probe(
            workspace, targets, self.root / "probe-out", runner, sys.executable,
            "mentis", input_sha)
        problems = out["problems"]
        self.assertTrue(any("OK1" in p and "读到受保护内容" in p for p in problems),
                        problems)  # 无窗口时可读 → 必须如实报告 READ
        self.assertTrue(any("MISS1" in p and "不存在" in p for p in problems), problems)
        self.assertFalse(any("input.json" in p for p in problems),
                         "正向 input.json 哈希匹配时不报告")  # 正向哈希匹配

    @unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
    def test_runner_inside_window_all_denied_and_positive_hashes_match(self):
        targets = self.probe_targets()
        workspace = self.root / "ws"
        shutil.copytree(self.prepared / "input-001" / "M", workspace)
        (workspace / "probe-codex").mkdir(exist_ok=True)
        (workspace / "probe-targets.json").write_text(
            json.dumps(targets, ensure_ascii=False), encoding="utf-8-sig")
        runner = self.root / "probe_runner.py"
        runner.write_text(iso._PROBE_RUNNER, encoding="utf-8")
        input_sha = _sha(self.prepared / "input-001" / "M" / "input.json")
        log = iso.open_window(self.protected, self.root / "iso")
        try:
            out = iso.run_deterministic_probe(workspace, targets, self.root / "probe-out",
                                              runner, sys.executable, "mentis", input_sha)
            self.assertEqual(out["problems"], [], out["problems"])
            outcomes = {e["outcome"] for e in out["payload"]["entries"]}
            self.assertEqual(outcomes, {"denied"})
        finally:
            iso.release_window(log)

    def test_real_codex_probe_writes_full_bookkeeping(self):
        """真实 _run_codex_probe 必须落盘全部原始记录（此前真实运行漏 targets 文件）。"""
        from mentis.contracts import AttemptStatus
        from mentis.executors.codex_cli import CodexTaskResult

        targets = self.probe_targets()
        workspace = self.root / "ws"
        (workspace / "probe-codex").mkdir(parents=True)
        (workspace / "input.json").write_bytes(b'{"arm":"input-001/M"}')
        (workspace / "prompt.md").write_bytes(b"prompt!!")
        (workspace / "probe-targets.json").write_text(
            json.dumps(targets, ensure_ascii=False), encoding="utf-8-sig")
        probe_dir = self.root / "probe-bookkeeping"
        probe_dir.mkdir()

        class FakeAdapter:
            def start_task(self, spec):
                # 模拟真实 Codex：在工作区写出探针结果
                (workspace / "probe-codex" / "result.json").write_text(
                    json.dumps(self.fake_result, ensure_ascii=False), encoding="utf-8")
                return CodexTaskResult(
                    task_id="t", attempt_id="a", brief_version=1, workspace=str(workspace),
                    status=AttemptStatus.COMPLETED, native_thread_id="01a0e752-6690",
                    cli_version="codex-cli 0.158.0", stdin_status="sent",
                    usage={"input_tokens": 5, "output_tokens": 1})

        fake = FakeAdapter()
        fake.fake_result = self.fake_codex_result(workspace, targets)
        out = iso._run_codex_probe(fake, workspace, targets, probe_dir,
                                   self.root / "runner.py", "", 300)
        self.assertEqual(out["status"], "completed")
        for name in ("codex-probe-prompt.md", "codex-probe-response.txt",
                     "codex-probe-events.json", "codex-probe-targets.json"):
            self.assertTrue((probe_dir / name).is_file(), name)
        # 判定真实函数产出的结果为干净（全部 denied + 正向匹配）
        self.assertEqual(out["problems"], [], out["problems"])

    def test_judge_rejects_link_create_failed_and_skipped_entries(self):
        payload = {"schema": iso.PROBE_SCHEMA, "probe_version": iso.PROBE_VERSION,
                   "entries": [
                       {"id": "T1", "form": "absolute", "outcome": "denied"},
                       {"id": "T1", "form": "parent_traversal", "outcome": "denied"},
                       {"id": "T1", "form": "junction", "outcome": "link_create_failed"},
                       {"id": "T2", "form": "absolute", "outcome": "not_found"}],
                   "positive": {"input.json": {"readable": True, "sha256": "x"},
                                "prompt.md": {"readable": True},
                                "workspace_write": {"ok": True}}}
        problems = iso.judge_probe_entries(
            payload, [("T1", "absolute"), ("T1", "parent_traversal"), ("T1", "junction"),
                      ("T2", "absolute"), ("T2", "parent_traversal"), ("T2", "junction")], "")
        self.assertTrue(any("链接创建失败" in p for p in problems), problems)
        self.assertTrue(any("不存在" in p for p in problems), problems)
        self.assertTrue(any("缺失" in p for p in problems), problems)


@unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
class EvidenceAdmissionTests(IsolationFixture):
    def _run_flow_with_fake_codex(self):
        """真实 ACL 窗口 + 真实 Mentis 探针 + 构造的 Codex 端结果 → 正式证据。"""
        outcome = None
        with mock.patch.object(iso, "_run_codex_probe") as fake_codex:
            def side_effect(adapter, workspace, probe_targets, probe_dir,
                            runner_path, expected_input_sha, timeout_s):
                result = self.fake_codex_result(workspace, probe_targets)
                result_path = workspace / "probe-codex" / "result.json"
                result_path.parent.mkdir(exist_ok=True)
                result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                (probe_dir / "codex-probe-targets.json").write_text(
                    json.dumps(probe_targets, ensure_ascii=False), encoding="utf-8-sig")
                (probe_dir / "codex-probe-prompt.md").write_text("prompt", encoding="utf-8")
                (probe_dir / "codex-probe-response.txt").write_text("response", encoding="utf-8")
                (probe_dir / "codex-probe-events.json").write_text("[]", encoding="utf-8")
                return {"status": "completed", "payload": result, "problems": [],
                        "native_thread_id": "01a0e752-6690-7333-a317-7acf38facc8e",
                        "cli_version": "0.158.0", "usage": {"input_tokens": 1},
                        "sandbox": "workspace-write", "model": "cli-default",
                        "stdin_status": "sent"}
            fake_codex.side_effect = side_effect
            outcome = iso.run_probe_flow(self.suite_path, self.prepared, "input-001/M",
                                         self.root / "iso", adapter=object())
        return outcome

    def test_full_flow_writes_evidence_and_admission_passes_only_in_window(self):
        outcome = self._run_flow_with_fake_codex()
        try:
            self.assertEqual(outcome["problems"], [], outcome["problems"])
            self.assertTrue(outcome["ok"])
            evidence_path = Path(outcome["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["schema"], iso.ISO_SCHEMA)
            self.assertIn("window_id", evidence)
            self.assertIn("input_hashes", evidence)
            self.assertIn("raw_hashes", evidence)
            self.assertEqual(evidence["executor"]["cli_version"], "0.158.0")
            expected = {"prepared_dir": str(self.prepared.resolve()),
                        "target_arm": "input-001/M",
                        "suite_sha256": evidence["suite_sha256"]}
            result = iso.check_admission(evidence_path, expected=expected,
                                         codex_version="0.158.0")
            self.assertTrue(result["runtime_answer_isolation"]["passed"],
                            json.dumps(result, ensure_ascii=False))
        finally:
            if not outcome.get("window_released"):
                outcome["release_outcome"] = iso.release_window(outcome["window"])
                # 与 eval-isolate release 一致：事务恢复后在证据上记录释放时间
                evidence_path = Path(outcome["evidence_path"])
                ev = json.loads(evidence_path.read_text(encoding="utf-8"))
                ev["released_at"] = "2026-10-01T00:00:00+00:00"
                evidence_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        # 窗口释放后同一证据立即失效
        result = iso.check_admission(Path(outcome["evidence_path"]), codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        window_active = next(c for c in result["runtime_answer_isolation"]["checks"]
                             if c["name"] == "window_active")
        self.assertFalse(window_active["passed"])

    def test_admission_rejects_tampered_or_incomplete_evidence(self):
        outcome = self._run_flow_with_fake_codex()
        evidence_path = Path(outcome["evidence_path"])
        try:
            good = json.loads(evidence_path.read_text(encoding="utf-8"))
            # 1) 原始记录被改动 → 哈希不符
            tampered = json.loads(evidence_path.read_text(encoding="utf-8"))
            tampered["raw_hashes"]["mentis-probe-result.json"] = "0" * 64
            evidence_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path, codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            # 2) 探针版本变化 → 失效
            evidence_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            tampered = dict(good, probe_version="e1-probe/1")
            evidence_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path, codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            # 3) Codex 端 skipped / 有问题 → 失效
            evidence_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            tampered = json.loads(json.dumps(good))
            tampered["codex_env_probe"]["problems"] = ["未执行 Codex 端探测"]
            evidence_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path, codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            # 4) 身份/执行器版本/输入哈希不匹配 → 失效
            evidence_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path, codex_version="0.999.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            tampered = json.loads(json.dumps(good))
            tampered["input_hashes"]["input.json"] = "f" * 64
            evidence_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path, codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            # 5) prepared 绑定不匹配 → 失效
            evidence_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path,
                                         expected={"prepared_dir": "F:/other/prepared"},
                                         codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
            # 6) 证据过期 → 失效
            evidence_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            result = iso.check_admission(evidence_path,
                                         now=datetime.now(timezone.utc) + timedelta(hours=13),
                                         codex_version="0.158.0")
            self.assertFalse(result["runtime_answer_isolation"]["passed"])
        finally:
            if not outcome.get("window_released"):
                iso.release_window(outcome["window"])

    def test_no_codex_produces_debug_only_never_dual_success(self):
        outcome = iso.run_probe_flow(self.suite_path, self.prepared, "input-001/M",
                                     self.root / "iso", adapter=None)
        self.assertFalse(outcome["ok"], "单端探测不得返回双端成功")
        self.assertTrue(outcome.get("debug"))
        self.assertIsNone(outcome.get("evidence"))
        self.assertTrue((self.root / "iso" / "debug-mentis-probe.json").is_file())
        self.assertFalse((self.root / "iso" / "isolation-evidence.json").exists())
        self.assertTrue(outcome.get("window_released"))
        self.assertTrue(iso._readable(self.suite_dir / "reviewer.json"))


class EvalCheckIsolationTests(IsolationFixture):
    @unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
    def test_window_active_reports_admission_and_applied_frozen_checks(self):
        outcome = self._run_flow_with_fake_codex()
        evidence_path = Path(outcome["evidence_path"])
        try:
            self.assertFalse(iso._readable(self.suite_path), "窗口内 suite.json 应不可读")
            result = iso.eval_check_with_isolation(self.suite_path, evidence_path,
                                                   codex_version="0.158.0")
            self.assertEqual(result["status"], "prepared_not_ready")
            self.assertTrue(result["isolation_window"]["active"])
            self.assertTrue(result["checks"][0]["passed"],
                            json.dumps(result, ensure_ascii=False))
        finally:
            if not outcome.get("window_released"):
                iso.release_window(outcome["window"])

    def _run_flow_with_fake_codex(self):
        with mock.patch.object(iso, "_run_codex_probe") as fake_codex:
            def side_effect(adapter, workspace, probe_targets, probe_dir,
                            runner_path, expected_input_sha, timeout_s):
                result = self.fake_codex_result(workspace, probe_targets)
                result_path = workspace / "probe-codex" / "result.json"
                result_path.parent.mkdir(exist_ok=True)
                result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                (probe_dir / "codex-probe-targets.json").write_text(
                    json.dumps(probe_targets, ensure_ascii=False), encoding="utf-8-sig")
                (probe_dir / "codex-probe-prompt.md").write_text("prompt", encoding="utf-8")
                (probe_dir / "codex-probe-response.txt").write_text("response", encoding="utf-8")
                (probe_dir / "codex-probe-events.json").write_text("[]", encoding="utf-8")
                return {"status": "completed", "payload": result, "problems": [],
                        "native_thread_id": "x", "cli_version": "0.158.0",
                        "usage": {}, "sandbox": "workspace-write",
                        "model": "cli-default", "stdin_status": "sent"}
            fake_codex.side_effect = side_effect
            return iso.run_probe_flow(self.suite_path, self.prepared, "input-001/M",
                                      self.root / "iso", adapter=object())

    def test_no_evidence_keeps_plain_audit_result(self):
        result = iso.eval_check_with_isolation(self.suite_path, self.root / "none.json")
        self.assertNotEqual(result["status"], "invalid")
        iso_check = next(c for c in result["checks"]
                         if c["name"] == "runtime_answer_isolation")
        self.assertFalse(iso_check["passed"])
        self.assertNotIn("isolation_admission", result)

    @unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
    def test_released_evidence_blocks_and_tampered_suite_is_reported(self):
        outcome = self._run_flow_with_fake_codex()
        evidence_path = Path(outcome["evidence_path"])
        if not outcome.get("window_released"):
            iso.release_window(outcome["window"])
        result = iso.eval_check_with_isolation(self.suite_path, evidence_path,
                                               codex_version="0.158.0")
        self.assertFalse(result["isolation_admission"]["passed"], "窗口关闭后准入必须失效")
        self.suite_path.write_bytes(self.suite_path.read_bytes() + b"\n")
        result = iso.eval_check_with_isolation(self.suite_path, evidence_path,
                                               codex_version="0.158.0")
        self.assertTrue(any("不一致" in reason for reason in result["blocking_reasons"]),
                        result.get("blocking_reasons"))


if __name__ == "__main__":
    unittest.main()
