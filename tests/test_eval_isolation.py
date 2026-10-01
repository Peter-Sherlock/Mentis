"""E1 隔离模块离线测试：真实 deny ACL 窗口 + 证据/准入行为；不涉及模型与执行器。

Windows 真实 ACL 用例只使用临时目录，不触碰仓库受保护材料；
非 Windows 环境自动跳过。夹具证明本模块逻辑，不替代真实
Mentis/Codex 双端探测（后者由 eval-isolate probe 在真实环境执行）。
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from mentis.eval_isolation import (
    ISO_SCHEMA, IsolationError, apply_window, check_admission, current_identity,
    eval_check_with_isolation, judge_codex_probe, judge_mentis_probe,
    protected_manifest, release_window, run_probe_flow, verification_targets,
    write_evidence, _readable,
)
from mentis.evaluation import audit_suite, prepare_suite

NT = os.name == "nt"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc(dt_iso: str) -> datetime:
    return datetime.fromisoformat(dt_iso)


class IsolationFixture(unittest.TestCase):
    """共享的临时"冻结套件 + 准备目录"结构（最小有效 suite，可被 audit_suite 通过）。

    根目录建在 %TEMP% 直下（mkdir 继承普通用户 ACL）而不是 tempfile 根：
    Python 3.14 tempfile 根带 OWNER RIGHTS ACE，其下 deny 生效后会锁死
    属主 WRITE_DAC，窗口无法解除——这正是金丝雀预检要拦截的环境。
    """

    def setUp(self):
        self.root = (Path(tempfile.gettempdir())
                     / f"mentis-eval-iso-{uuid4().hex[:10]}")
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
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
        # 准备目录：用真实 prepare_suite 生成，保证 prepare-report 结构一致
        self.prepared = self.root / "prepared"
        prepare_suite(self.suite_path, self.prepared)
        self.protected = protected_manifest(self.suite_path, self.prepared, "input-001/M")
        # 套件目录 + 身份映射 + 除目标组外的全部输入包（S1 有 B-1/B-2/B-3 三个非目标组）
        self.assertEqual(len(self.protected), 1 + 1 + 3)

    def write_probe_workspace(self):
        workspace = self.root / "probe-workspace"
        (workspace / "probe-codex").mkdir(parents=True)
        (workspace / "input.json").write_bytes(b'{"arm":"input-001/M"}')
        (workspace / "prompt.md").write_bytes(b"prompt")
        return workspace

    def evidence_with(self, protected=None, **overrides):
        """基于当前真实身份/时间构造一份可定制的证据夹具。"""
        protected = protected if protected is not None else self.protected
        targets = verification_targets(protected)
        base = {
            "schema": ISO_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "valid_until": (datetime.now(timezone.utc)
                            + timedelta(hours=12)).isoformat(timespec="seconds"),
            "identity": current_identity(),
            "suite_path": str(self.suite_path),
            "suite_id": "e1-dev-test",
            "suite_sha256": "a" * 64,
            "frozen_input_checks": {"frozen_source_hashes": {"passed": True, "file_count": 5}},
            "window": {"protected": [str(p) for p in protected],
                       "accounts": current_identity()["user"],
                       "verification_targets": targets},
            "mentis_env_probe": {"positive": {}, "negative": []},
            "mentis_probe_problems": [],
            "codex_env_probe": {"status": "completed", "cli_version": "0.158.0",
                                "denied_files": ["denied-1.txt"], "read_files": [],
                                "positive_write": "OK", "usage": {}},
            "codex_probe_problems": [],
            "probe_dirs": [],
            "released_at": "",
        }
        base.update(overrides)
        return base


def _sha_entry(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ManifestTests(IsolationFixture):
    def test_manifest_rejects_unknown_arm(self):
        with self.assertRaisesRegex(IsolationError, "不在 prepare-report"):
            protected_manifest(self.suite_path, self.prepared, "input-009/Z")

    def test_verification_targets_prefer_sensitive_files(self):
        targets = verification_targets(self.protected)
        probe_names = {Path(t["probe"]).name for t in targets}
        self.assertIn("reviewer.json", probe_names)
        self.assertIn("input.json", probe_names)
        for target in targets:
            self.assertEqual(target["sha256_at_apply"], _sha(Path(target["probe"])))

    def test_verification_target_required_for_empty_dir(self):
        empty = self.root / "empty-protected"
        empty.mkdir()
        with self.assertRaisesRegex(IsolationError, "没有可验证的文件"):
            verification_targets([empty])
        with self.assertRaisesRegex(IsolationError, "不存在"):
            verification_targets([self.root / "missing"])


@unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
class WindowTests(IsolationFixture):
    def test_apply_denies_and_release_restores(self):
        targets = verification_targets(self.protected)
        window = apply_window(self.protected)
        try:
            for target in targets:
                with self.assertRaises(PermissionError):
                    Path(target["probe"]).read_bytes()
        finally:
            release_window(self.protected, window["accounts"])
        for target in targets:
            self.assertTrue(_readable(Path(target["probe"])), target["probe"])

    def test_ineffective_deny_never_passes_and_leaves_no_window(self):
        # 场景一：icacls 被架空（deny 没有真实写入）→ 金丝雀预检立即拦截
        with mock.patch("mentis.eval_isolation._icacls") as fake:
            fake.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            with self.assertRaisesRegex(IsolationError, "金丝雀 deny 未生效"):
                apply_window(self.protected)
        self.assertTrue(_readable(self.suite_dir / "reviewer.json"),
                        "失败的 apply 不得留下半开窗口")
        # 场景二：金丝雀被 mock 通过，但 deny 未生效 → live 检查失败并尽力解除
        with mock.patch("mentis.eval_isolation._icacls") as fake, \
                mock.patch("mentis.eval_isolation._canary_check"):
            fake.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            with self.assertRaisesRegex(IsolationError, "隔离窗口无效"):
                apply_window(self.protected)
        self.assertTrue(_readable(self.suite_dir / "reviewer.json"),
                        "失败的 apply 不得留下半开窗口")
        window = apply_window(self.protected)
        try:
            with self.assertRaisesRegex(IsolationError, "已不可读"):
                apply_window(self.protected)
        finally:
            release_window(self.protected, window["accounts"])

    def test_real_mentis_env_probe_inside_window(self):
        """真实同用户子进程：窗口内正向可读写、反向三类读取全部拒绝。"""
        workspace = self.write_probe_workspace()
        probe_files = [Path(t["probe"]) for t in verification_targets(self.protected)]
        window = apply_window(self.protected)
        try:
            from mentis.eval_isolation import probe_mentis_env
            payload = probe_mentis_env(workspace, probe_files, self.root / "probe-out")
            self.assertEqual(judge_mentis_probe(payload), [])
            forms = {row["form"]: row["result"] for row in payload["negative"]}
            self.assertTrue(forms and set(forms.values()) == {"denied"},
                            f"反向探测必须全部拒绝: {forms}")
            self.assertEqual(payload["positive"]["workspace_write"], "probe-write-ok")
        finally:
            release_window(self.protected, window["accounts"])

    def test_mentis_probe_detects_readable_protected_file(self):
        """无 deny 时探测必须如实报告读到内容（反向证据失败）。"""
        workspace = self.write_probe_workspace()
        from mentis.eval_isolation import probe_mentis_env
        payload = probe_mentis_env(workspace, [self.suite_dir / "reviewer.json"],
                                   self.root / "probe-out2")
        problems = judge_mentis_probe(payload)
        self.assertTrue(any("反向证据失败" in p for p in problems), problems)


class JudgeTests(IsolationFixture):
    def test_mentis_judge_flags_missing_positive_and_unexpected_errors(self):
        problems = judge_mentis_probe({"positive": {"error": "boom"}, "negative": [
            {"form": "absolute", "result": "other_error", "error": "FileNotFoundError"}]})
        self.assertTrue(any("正向证据缺失" in p for p in problems))
        self.assertTrue(any("非预期错误" in p for p in problems))

    def test_codex_judge_flags_missing_markers_and_reads(self):
        base = {"expected_denied": ["denied-1.txt", "denied-2.txt"],
                "denied_files": ["denied-1.txt"], "read_files": [],
                "positive_write": "OK", "input_json_bytes": 10}
        self.assertEqual(judge_codex_probe({**base,
                                            "denied_files": ["denied-1.txt", "denied-2.txt"]}), [])
        problems = judge_codex_probe(base)
        self.assertTrue(any("反向证据不完整" in p for p in problems))
        self.assertTrue(any("反向证据失败" in p for p in
                            judge_codex_probe({**base, "read_files": ["read-1.txt"]})))
        self.assertTrue(any("正向证据缺失" in p for p in
                            judge_codex_probe({**base, "positive_write": ""})))
        self.assertTrue(any("input.json" in p for p in
                            judge_codex_probe({**base, "input_json_bytes": 0})))


class EvidenceTests(IsolationFixture):
    def test_write_evidence_rejects_failed_audit(self):
        audit = {"status": "invalid", "checks": [], "blocking_reasons": ["x"]}
        with self.assertRaisesRegex(IsolationError, "冻结源核对未通过"):
            write_evidence(self.root / "e.json", self.suite_path, audit,
                           window={}, mentis_payload={}, mentis_problems=[],
                           codex_payload={}, codex_problems=[], probe_dirs=[])

    def test_write_evidence_records_identity_and_ttl(self):
        audit = {"status": "prepared_not_ready", "suite_id": "e1-dev-test",
                 "suite_sha256": "b" * 64,
                 "checks": [{"name": "schema_and_input_whitelist", "passed": True},
                            {"name": "source_paths_and_targets", "passed": True},
                            {"name": "frozen_source_hashes", "passed": True, "file_count": 5}]}
        evidence = write_evidence(
            self.root / "e.json", self.suite_path, audit,
            window={"protected": ["p"], "verification_targets": []},
            mentis_payload={"positive": {}, "negative": []}, mentis_problems=[],
            codex_payload={"cli_version": "0.158.0", "status": "completed"}, codex_problems=[],
            probe_dirs=["d"], ttl_s=3600)
        loaded = json.loads((self.root / "e.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema"], ISO_SCHEMA)
        self.assertEqual(loaded["identity"], current_identity())
        self.assertEqual(loaded["suite_sha256"], "b" * 64)
        self.assertEqual(loaded["codex_env_probe"]["cli_version"], "0.158.0")
        gap = (_utc(loaded["valid_until"]) - _utc(loaded["created_at"])).total_seconds()
        self.assertEqual(gap, 3600)
        self.assertEqual(evidence["released_at"], "")


class AdmissionTests(IsolationFixture):
    def _write(self, **overrides) -> Path:
        evidence = self.evidence_with(**overrides)
        path = self.root / "isolation-evidence.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_missing_or_invalid_evidence_stays_not_ready(self):
        result = check_admission(self.root / "none.json")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        (self.root / "bad.json").write_text("{oops", encoding="utf-8")
        result = check_admission(self.root / "bad.json")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])

    @unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
    def test_full_admission_passes_only_with_live_window(self):
        # 证据必须在开窗前构造：deny 后无法再枚举目录取验证点
        path = self._write()
        window = apply_window(self.protected)
        try:
            result = check_admission(path, codex_version="0.158.0")
            self.assertTrue(result["runtime_answer_isolation"]["passed"],
                            json.dumps(result, ensure_ascii=False))
        finally:
            release_window(self.protected, window["accounts"])
        # 关闭窗口后：同一份证据必须立即失效——清单声明不算数
        result = check_admission(path, codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        live = next(c for c in result["runtime_answer_isolation"]["checks"]
                    if c["name"] == "live_denial_current")
        self.assertFalse(live["passed"])

    def test_expired_evidence_rejected(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        path = self._write(created_at=stale, valid_until=stale)
        result = check_admission(path, now=datetime.now(timezone.utc) + timedelta(hours=1),
                                 codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        self.assertTrue(any(c["name"] == "evidence_fresh" and not c["passed"]
                            for c in result["runtime_answer_isolation"]["checks"]))

    def test_identity_and_codex_version_mismatch_rejected(self):
        path = self._write(identity={"user": "OTHER\\someone", "host": "OTHER-PC",
                                     "python": "py"})
        result = check_admission(path, codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        path = self._write()
        result = check_admission(path, codex_version="0.999.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])

    def test_released_or_dirty_probe_evidence_rejected(self):
        path = self._write(released_at="2026-10-01T00:00:00+00:00")
        result = check_admission(path, codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        self.assertTrue(any(c["name"] == "window_active" and not c["passed"]
                            for c in result["runtime_answer_isolation"]["checks"]))
        path = self._write(mentis_probe_problems=["反向证据失败：读到了内容"])
        result = check_admission(path, codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])

    def test_protected_manifest_must_be_covered_by_targets(self):
        path = self._write()
        result = check_admission(path, protected=self.protected + [self.root / "extra-dir"],
                                 codex_version="0.158.0")
        self.assertFalse(result["runtime_answer_isolation"]["passed"])
        self.assertTrue(any(c["name"] == "protected_manifest_covered" and not c["passed"]
                            for c in result["runtime_answer_isolation"]["checks"]))


@unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
class EvalCheckIsolationTests(IsolationFixture):
    def test_window_active_reports_admission_and_applied_frozen_checks(self):
        audit = audit_suite(self.suite_path)
        self.assertNotEqual(audit["status"], "invalid")
        window = apply_window(self.protected)
        try:
            evidence_path = self.prepared / "isolation" / "isolation-evidence.json"
            write_evidence(evidence_path, self.suite_path, audit, window,
                           {"positive": {}, "negative": []}, [],
                           {"cli_version": "0.158.0", "status": "completed"}, [], [])
            self.assertFalse(_readable(self.suite_path), "窗口内 suite.json 应不可读")
            result = eval_check_with_isolation(self.suite_path, evidence_path,
                                               codex_version="0.158.0")
            self.assertEqual(result["status"], "prepared_not_ready")
            self.assertTrue(result["isolation_window"]["active"])
            self.assertTrue(result["checks"][0]["passed"],
                            json.dumps(result, ensure_ascii=False))
        finally:
            release_window(self.protected, window["accounts"])

    def test_no_evidence_keeps_plain_audit_result(self):
        result = eval_check_with_isolation(self.suite_path, self.root / "none.json")
        self.assertNotEqual(result["status"], "invalid")
        iso = next(c for c in result["checks"] if c["name"] == "runtime_answer_isolation")
        self.assertFalse(iso["passed"])
        self.assertNotIn("isolation_admission", result)

    def test_released_evidence_blocks_and_tampered_suite_is_reported(self):
        audit = audit_suite(self.suite_path)
        window = apply_window(self.protected)
        try:
            evidence_path = self.prepared / "isolation" / "isolation-evidence.json"
            write_evidence(evidence_path, self.suite_path, audit, window,
                           {"positive": {}, "negative": []}, [],
                           {"cli_version": "0.158.0", "status": "completed"}, [], [])
        finally:
            release_window(self.protected, window["accounts"])
        result = eval_check_with_isolation(self.suite_path,
                                           self.prepared / "isolation"
                                           / "isolation-evidence.json",
                                           codex_version="0.158.0")
        self.assertFalse(result["isolation_admission"]["passed"], "窗口关闭后准入必须失效")
        # 套件被改动：闭环核对必须报告证据与磁盘不一致
        self.suite_path.write_bytes(self.suite_path.read_bytes() + b"\n")
        result = eval_check_with_isolation(self.suite_path,
                                           self.prepared / "isolation"
                                           / "isolation-evidence.json",
                                           codex_version="0.158.0")
        self.assertTrue(any("不一致" in reason for reason in result["blocking_reasons"]),
                        result.get("blocking_reasons"))


class ProbeFlowTests(IsolationFixture):
    def test_failed_probe_releases_window_and_writes_no_evidence(self):
        with mock.patch("mentis.eval_isolation.probe_mentis_env",
                        side_effect=IsolationError("探测崩溃")):
            with self.assertRaises(IsolationError):
                run_probe_flow(self.suite_path, self.prepared, "input-001/M",
                               self.root / "iso", adapter=None)
        self.assertTrue(_readable(self.suite_dir / "reviewer.json"),
                        "失败后窗口必须已解除")
        self.assertFalse((self.root / "iso" / "isolation-evidence.json").exists())

    @unittest.skipUnless(NT, "真实 deny ACL 仅在 Windows 验证")
    def test_successful_mentis_probe_writes_evidence_and_keeps_window(self):
        outcome = run_probe_flow(self.suite_path, self.prepared, "input-001/M",
                                 self.root / "iso", adapter=None)
        try:
            self.assertEqual(outcome["problems"], [], outcome["problems"])
            self.assertIsNotNone(outcome["evidence"])
            self.assertTrue((self.root / "iso" / "isolation-evidence.json").is_file())
            live = outcome["window"]["live_denial_check"]
            self.assertTrue(all(row["denied"] for row in live))
            # 窗口保持开启（证据已写）： Mentis 端反向目标此刻仍拒读
            with self.assertRaises(PermissionError):
                (self.suite_dir / "reviewer.json").read_bytes()
        finally:
            release_window(self.protected, outcome["window"]["accounts"])


if __name__ == "__main__":
    unittest.main()
