"""E1 离线夹具：冻结与清洁导出，不是模型或运行端权限隔离实测。"""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from mentis.evaluation import audit_suite, prepare_suite


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suite_dir = self.root / "freeze"
        self.suite_dir.mkdir()
        self.suite_path = self.suite_dir / "suite.json"
        self.output = self.root / "prepared"
        common = self.put("shared/rules.md", "公开领域资料。", "resources/rules.md")
        first = self.put("samples/E1-01/page.html", "<h1>活动</h1>", "workspace/index.html")
        second = self.put("samples/E1-02/copy.md", "合成文案包", "input/copy.md")
        self.put("reviewer.json", "SECRET_AUTHOR_EXPECTED")
        self.put("samples/E1-01/excluded.md", "SECRET_UNLISTED_FILE")
        prompts = {}
        for arm in ["B-0", "B-1", "B-2", "B-3", "M"]:
            entry = self.put(f"prompts/{arm}.md", f"{arm} 专业提示词")
            prompts[arm] = {key: entry[key] for key in ["source", "sha256"]}
        self.suite = {
            "schema_version": 1, "suite_id": "e1-dev-test", "status": "exploratory_frozen",
            "common": {"files": [common], "policy": {"max_auto_revisions": 2,
                       "network": "none", "available_tools": ["read", "observe"]}},
            "samples": [
                {"sample_id": "E1-01", "kind": "feedback", "review_status": "pending_human",
                 "input": {"user_goal": "制作合成活动页", "user_feedback": "（样例反馈）字太小",
                           "feedback_source": "simulated", "conversation_context": ["保留活动文案"]},
                 "files": [first], "observations_missing": ["interaction_behavior"]},
                {"sample_id": "E1-02", "kind": "end_to_end", "review_status": "pending_human",
                 "input": {"user_goal": "制作合成活动页", "user_feedback": "",
                           "feedback_source": "none", "conversation_context": []},
                 "files": [second], "observations_missing": []},
            ],
            "treatments": {"feedback": ["B-1", "B-2", "B-3", "M"],
                           "end_to_end": ["B-0", "B-1", "B-2", "M"],
                           "decision": ["B-1", "B-2", "B-3", "M"]},
            "prompts": prompts,
            "isolation": {"status": "unverified", "note": "尚无独立身份/挂载读取限制实测"},
        }
        self.write_suite()

    def put(self, source, content, target=None):
        raw = content.encode("utf-8")
        path = self.suite_dir / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entry = {"source": source, "sha256": sha(raw)}
        if target is not None:
            entry["target"] = target
        return entry

    def write_suite(self):
        self.suite_path.write_text(json.dumps(self.suite, ensure_ascii=False, indent=2), encoding="utf-8")

    def assert_rejected(self, expected):
        self.write_suite()
        with self.assertRaisesRegex(ValueError, expected):
            prepare_suite(self.suite_path, self.output)
        self.assertFalse(self.output.exists(), "无效清单不应创建导出目录")
        audit = audit_suite(self.suite_path)
        self.assertEqual(audit["status"], "invalid")
        self.assertTrue(audit["blocking_reasons"])

    def directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"本机未授权创建符号链接: {exc}")
            # Windows junction 是同一 resolve 边界；不需管理员权限。
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                     capture_output=True, check=False)
            if created.returncode:
                self.skipTest(f"本机符号链接与 junction 均不可创建: {created.returncode}")

    def test_exports_only_whitelist_and_no_evaluation_identity_inside_arms(self):
        report = prepare_suite(self.suite_path, self.output)
        self.assertEqual(report["status"], "prepared_not_ready")
        self.assertEqual(report["packages"][0]["sample_id"], "E1-01")
        self.assertEqual(report["packages"][0]["neutral_id"], "input-001")
        package = self.output / "input-001" / "M"
        self.assertEqual({path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()},
                         {"input.json", "prompt.md", "resources/rules.md", "workspace/index.html"})
        runtime_input = json.loads((package / "input.json").read_text(encoding="utf-8"))
        self.assertEqual(set(runtime_input), {"input", "policy", "observations_missing"})
        for row in report["packages"]:
            for arm in row["arms"]:
                arm_dir = self.output / arm["path"]
                payload = b"\n".join(path.read_bytes() for path in arm_dir.rglob("*") if path.is_file())
                for forbidden in [b"SECRET_AUTHOR_EXPECTED", b"SECRET_UNLISTED_FILE", b"sample_id",
                                  b"review_status", b"E1-01", b"E1-02", b"samples/E1", b"end_to_end"]:
                    self.assertNotIn(forbidden, payload)
        self.assertEqual(json.loads((self.output / "prepare-report.json").read_text(encoding="utf-8")), report)

    def test_shared_input_bytes_and_fingerprints_are_equal_for_each_treatment(self):
        report = prepare_suite(self.suite_path, self.output)
        for row in report["packages"]:
            inventories = []
            for arm in row["arms"]:
                package = self.output / arm["path"]
                inventories.append({path.relative_to(package).as_posix(): path.read_bytes()
                                    for path in package.rglob("*") if path.is_file() and path.name != "prompt.md"})
                self.assertEqual(sha((package / "prompt.md").read_bytes()), arm["prompt_sha256"])
                for file in row["shared_files"]:
                    self.assertEqual(sha((package / file["target"]).read_bytes()), file["sha256"])
            self.assertTrue(all(inventory == inventories[0] for inventory in inventories))
            self.assertEqual(len({arm["shared_fingerprint_sha256"] for arm in row["arms"]}), 1)
        self.assertNotEqual(report["packages"][0]["arms"][0]["shared_fingerprint_sha256"],
                            report["packages"][1]["arms"][0]["shared_fingerprint_sha256"])

    def test_changed_page_or_prompt_source_rejected_before_export(self):
        for relative in ["samples/E1-01/page.html", "prompts/B-1.md"]:
            with self.subTest(source=relative):
                path = self.suite_dir / relative
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.assert_rejected("sha256")
                path.write_bytes(original)

    def test_unknown_fields_in_input_and_manifest_are_rejected(self):
        original = copy.deepcopy(self.suite)
        for location in ["input", "sample", "root"]:
            with self.subTest(location=location):
                self.suite = copy.deepcopy(original)
                target = {"input": self.suite["samples"][0]["input"],
                          "sample": self.suite["samples"][0], "root": self.suite}[location]
                target["expected_answer"] = "SECRET_AUTHOR_EXPECTED"
                self.assert_rejected("白名单")

    def test_annotations_in_public_policy_are_rejected_recursively(self):
        self.suite["common"]["policy"]["nested"] = [{"gold_answer": "SECRET_AUTHOR_EXPECTED"}]
        self.assert_rejected("作者答案")

    def test_explicitly_listing_reviewer_file_under_neutral_target_rejected(self):
        self.suite["samples"][0]["files"].append(
            {"source": "reviewer.json", "target": "input/extra.json", "sha256": sha(b"SECRET_AUTHOR_EXPECTED")})
        self.assert_rejected("评价端")

    def test_source_absolute_parent_drive_ads_and_devices_rejected(self):
        original = self.suite["samples"][0]["files"][0]["source"]
        for source in ["../outside.html", "/outside.html", "C:\\outside.html", "C:outside.html",
                       "\\\\server\\share\\outside.html", "samples/E1-01/page.html:secret", "NUL"]:
            with self.subTest(source=source):
                self.suite["samples"][0]["files"][0]["source"] = source
                self.assert_rejected("路径|字符|设备名")
        self.suite["samples"][0]["files"][0]["source"] = original

    def test_symlink_source_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "page.html").write_bytes(b"outside")
        link = self.suite_dir / "escape"
        self.directory_link(link, outside)
        self.suite["samples"][0]["files"][0].update(source="escape/page.html", sha256=sha(b"outside"))
        self.assert_rejected("符号链接逃逸")

    def test_existing_output_link_does_not_write_outside_selected_directory(self):
        outside = self.root / "outside-output"
        outside.mkdir()
        self.directory_link(self.output, outside)
        with self.assertRaisesRegex(ValueError, "符号链接|junction"):
            prepare_suite(self.suite_path, self.output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_escape_reserved_and_identity_paths_are_rejected(self):
        for target in ["../outside.html", "C:/outside.html", "input.json", "INPUT.JSON",
                       "prompt.md", "reviewer.json", "prepare-report.json", "input.json/child",
                       "workspace/E1-01.html", "workspace/page.html:secret"]:
            with self.subTest(target=target):
                self.suite["samples"][0]["files"][0]["target"] = target
                self.assert_rejected("路径|保留|sample_id|字符")

    def test_duplicate_or_ancestor_targets_in_common_and_sample_rejected(self):
        first = self.suite["samples"][0]["files"][0]
        for target in ["resources/rules.md", "RESOURCES/RULES.MD", "resources", "resources/rules.md/child"]:
            with self.subTest(target=target):
                first["target"] = target
                self.assert_rejected("target 重复|目录碰撞")
        first["target"] = "workspace/index.html"
        self.suite["samples"][0]["files"].append(copy.deepcopy(first))
        self.assert_rejected("target 重复")

    def test_export_never_overwrites_existing_output(self):
        prepare_suite(self.suite_path, self.output)
        marker = self.output / "prepare-report.json"
        original = marker.read_bytes()
        with self.assertRaises(FileExistsError):
            prepare_suite(self.suite_path, self.output)
        self.assertEqual(marker.read_bytes(), original)

    def test_nonempty_output_refused_and_empty_output_accepted(self):
        self.output.mkdir()
        marker = self.output / "user-work.txt"
        marker.write_text("保留", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            prepare_suite(self.suite_path, self.output)
        self.assertEqual(marker.read_text(encoding="utf-8"), "保留")
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(prepare_suite(self.suite_path, empty)["status"], "prepared_not_ready")

    def test_declared_verified_isolation_is_never_accepted_as_evidence(self):
        self.suite["isolation"]["status"] = "verified"
        self.suite["isolation"]["note"] = "另一个目录，同一个Windows用户"
        for sample in self.suite["samples"]:
            sample["review_status"] = "independent_reviewed"
            sample["observations_missing"] = []
        self.write_suite()
        audit = audit_suite(self.suite_path)
        self.assertEqual(audit["status"], "prepared_not_ready")
        isolation = next(check for check in audit["checks"] if check["name"] == "runtime_answer_isolation")
        self.assertFalse(isolation["passed"])
        self.assertEqual(isolation["declared_status"], "verified")
        self.assertTrue(any("隔离证明" in reason for reason in audit["blocking_reasons"]))

    def test_pending_human_limits_formal_claims_without_extra_exploration_gate(self):
        audit = audit_suite(self.suite_path)
        self.assertEqual(audit["status"], "prepared_not_ready")
        self.assertEqual(len(audit["exploratory_blocking_reasons"]), 1)
        self.assertIn("隔离证明", audit["exploratory_blocking_reasons"][0])
        self.assertFalse(any("人工复核" in reason for reason in audit["exploratory_blocking_reasons"]))
        self.assertTrue(any("人工复核" in reason for reason in audit["formal_claim_limits"]))
        self.assertTrue(any("阈值尚未冻结" in reason for reason in audit["formal_claim_limits"]))

    def test_no_initial_artifact_is_not_missing_observation_for_e2e_or_decision(self):
        for kind in ["end_to_end", "decision"]:
            with self.subTest(kind=kind):
                self.suite["samples"][1]["kind"] = kind
                self.suite["samples"][1]["observations_missing"] = ["initial_artifact_not_applicable"]
                self.write_suite()
                audit = audit_suite(self.suite_path)
                missing = [reason for reason in audit["formal_claim_limits"] if "显式缺测" in reason]
                self.assertEqual(len(missing), 1)
                self.assertIn("E1-01", missing[0])
                self.assertNotIn("E1-02", missing[0])

    def test_decision_sample_is_exported_without_b0(self):
        self.suite["samples"][0]["kind"] = "decision"
        self.write_suite()
        report = prepare_suite(self.suite_path, self.output)
        self.assertEqual({arm["treatment"] for arm in report["packages"][0]["arms"]},
                         {"B-1", "B-2", "B-3", "M"})
        self.assertFalse((self.output / "input-001" / "B-0").exists())

    def test_feedback_source_mismatch_and_duplicate_json_keys_rejected(self):
        self.suite["samples"][0]["input"]["feedback_source"] = "none"
        self.assert_rejected("来源不一致")
        self.suite_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "字段重复"):
            prepare_suite(self.suite_path, self.output)


if __name__ == "__main__":
    unittest.main()
