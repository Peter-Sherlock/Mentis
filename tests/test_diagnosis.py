"""P2.2 离线检查：网关记录与脱敏、结构观察器、诊断校验（全部不触网）。"""

import json
import os
import io
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from mentis.capabilities.registry import load_registry
from mentis.capabilities.selection import select_for_task
from mentis.diagnosis import (
    build_user_prompt,
    parse_and_validate,
    render_bundle_markdown,
    run_diagnosis,
)
from mentis.models.gateway import ModelConfig, ModelGateway
from mentis.observation import observe_structure

FIXTURE = os.path.join("tests", "fixtures", "mock_diagnosis_response.json")

SAMPLE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>演示页</title></head>
<body>
<h1>周六手冲入门工作坊</h1>
<p>时间：2026-11-14 14:00—16:00｜地点：拾光咖啡（演示）｜价格：¥99</p>
<a href="#signup" id="signup">报名：留下姓名和联系方式</a>
<form><input name="name" required><input name="contact"><button type="submit">提交</button></form>
</body></html>
"""

GOOD_JSON = json.dumps(
    {
        "diagnoses": [
            {
                "phenomenon": "报名入口是链接不是表单",
                "evidence_refs": ["link:1"],
                "severity": "plan_changing",
                "interpretation": "链接无法承载校验与确认反馈",
                "modification_candidates": ["升级为本地表单"],
            }
        ],
        "next_action": "agent_decide",
        "decision_requests": [],
    },
    ensure_ascii=False,
)


def fake_gateway(responses, **kw):
    queue = list(responses)
    return ModelGateway(config=ModelConfig(), transport=lambda s, u, j: queue.pop(0))


def make_observation(tmpdir):
    path = os.path.join(tmpdir, "page.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_PAGE)
    return observe_structure(path)


class GatewayTests(unittest.TestCase):
    def test_default_http_transport_records_real_usage_and_model(self):
        payload = {
            "choices": [{"message": {"content": GOOD_JSON}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 35, "total_tokens": 155},
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
                gw = ModelGateway(config=ModelConfig())
                record = gw.complete("system", "prompt")
        sent = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "deepseek-flash")
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertTrue(record.ok)
        self.assertFalse(record.mock)
        self.assertEqual(gw.usage_summary()["real_calls"], 1)
        self.assertEqual(gw.usage_summary()["prompt_tokens"], 120)
        self.assertEqual(gw.usage_summary()["completion_tokens"], 35)

    def test_http_error_does_not_record_server_body_or_key(self):
        secret = "test-secret"
        failure = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 401, "Bearer " + secret,
            {}, io.BytesIO(("Bearer " + secret).encode("utf-8")),
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            with patch("urllib.request.urlopen", side_effect=failure):
                record = ModelGateway().complete("system", "prompt")
        self.assertFalse(record.ok)
        self.assertEqual(record.error, "HTTP 401")
        self.assertNotIn(secret, json.dumps(record.__dict__))

    def test_missing_credentials_recorded_not_raised(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            gw = ModelGateway(config=ModelConfig())
            record = gw.complete("s", "u")
        self.assertFalse(record.ok)
        self.assertIn("缺少凭据", record.error)

    def test_env_file_fallback_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, ".env")
            with open(env_file, "w", encoding="utf-8") as f:
                f.write('# local secret\nDEEPSEEK_API_KEY="file-secret"\n')
            gw = ModelGateway(config=ModelConfig(env_file=env_file))
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(gw.api_key, "file-secret")
                self.assertTrue(gw.has_credentials())
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-secret"}):
                self.assertEqual(gw.api_key, "environment-secret")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
                self.assertFalse(gw.has_credentials())

    def test_fake_transport_success_and_no_secret_in_record(self):
        gw = fake_gateway([GOOD_JSON])
        record = gw.complete("system", "prompt")
        self.assertTrue(record.ok)
        self.assertEqual(record.response_text, GOOD_JSON)
        self.assertTrue(record.mock)  # 注入传输即视为 Mock 路径
        blob = json.dumps(record.__dict__, ensure_ascii=False)
        self.assertNotIn("sk-", blob)
        self.assertEqual(gw.usage_summary()["mock_calls"], 1)
        self.assertEqual(gw.usage_summary()["real_calls"], 0)

    def test_request_ids_are_unique_across_gateway_instances(self):
        first = fake_gateway([GOOD_JSON]).complete("s", "u")
        second = fake_gateway([GOOD_JSON]).complete("s", "u")
        self.assertNotEqual(first.request_id, second.request_id)


class ObserverTests(unittest.TestCase):
    def test_structure_items_and_limitations(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            kinds = {i.kind for i in obs.items}
            self.assertIn("h1", kinds)
            self.assertIn("a", kinds)
            self.assertIn("form", kinds)
            self.assertIn("button", kinds)
            self.assertTrue(any("未验证" in lim for lim in obs.limitations))
            self.assertIn("h:1", obs.item_ids())

    def test_img_alt_presence_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "img.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write('<html><body><h1>标题</h1><img src="hero.png"></body></html>')
            obs = observe_structure(path)
            img = next(i for i in obs.items if i.kind == "img")
            self.assertEqual(img.extra["alt_present"], "no")

    def test_body_text_is_observed_without_head_or_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write('<html><head><title>不属于正文</title></head><body>'
                        '<h1>手冲工作坊</h1><p>日期 11 月 14 日，价格 ¥99</p>'
                        '<script>secret_in_script</script></body></html>')
            obs = observe_structure(path)
            rendered = obs.render_text()
            self.assertIn("日期 11 月 14 日，价格 ¥99", rendered)
            self.assertIn("text:1", obs.item_ids())
            self.assertNotIn("secret_in_script", rendered)
            self.assertNotIn("不属于正文", rendered)
            self.assertTrue(any("观察记录未出现不等于实际页面缺失" in lim for lim in obs.limitations))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry()

    def test_valid_fixture_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            bundle = parse_and_validate(
                GOOD_JSON, obs, goal="g", model="test", call_ids=["call-001"], usage={}, mock=True
            )
            self.assertEqual(len(bundle.valid_diagnoses), 1)
            self.assertEqual(bundle.next_action, "agent_decide")
            self.assertTrue(bundle.next_action_valid)

    def test_fabricated_evidence_ref_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            bad = json.dumps(
                {
                    "diagnoses": [
                        {
                            "phenomenon": "配图缺失",
                            "evidence_refs": ["img:99"],
                            "severity": "minor",
                            "interpretation": "x",
                            "modification_candidates": ["y"],
                        }
                    ],
                    "next_action": "stop",
                    "decision_requests": [],
                },
                ensure_ascii=False,
            )
            bundle = parse_and_validate(bad, obs, "g", "test", ["c"], {}, True)
            self.assertEqual(len(bundle.valid_diagnoses), 0)
            self.assertIn("虚构证据位置", bundle.invalid_diagnoses[0]["reasons"][0])

    def test_decision_request_requires_three_elements(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            bad = json.dumps(
                {
                    "diagnoses": [],
                    "next_action": "request_user",
                    "decision_requests": [
                        {"question": "要不要深色模式？", "blocking_reason": "", "user_unique_info_or_auth_basis": "", "excluded_autonomous_paths": ""}
                    ],
                },
                ensure_ascii=False,
            )
            bundle = parse_and_validate(bad, obs, "g", "test", ["c"], {}, True)
            self.assertEqual(len(bundle.valid_decision_requests), 0)
            reasons = "；".join(bundle.invalid_decision_requests[0]["reasons"])
            self.assertIn("阻塞原因", reasons)
            self.assertIn("用户独有信息", reasons)
            self.assertIn("已排除的自主路径", reasons)
            self.assertFalse(bundle.next_action_valid)

    def test_missing_response_fields_are_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            bundle = parse_and_validate("{}", obs, "g", "test", ["c"], {}, True)
            self.assertTrue(bundle.errors)
            self.assertFalse(bundle.next_action_valid)

    def test_unexpected_nested_evidence_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            raw = json.dumps({
                "diagnoses": [{
                    "phenomenon": "示例", "evidence_refs": [{"id": "link:1"}],
                    "severity": "minor", "interpretation": "示例",
                    "modification_candidates": ["修改链接"],
                }],
                "next_action": "agent_decide", "decision_requests": [],
            }, ensure_ascii=False)
            bundle = parse_and_validate(raw, obs, "g", "test", ["c"], {}, True)
            self.assertEqual(len(bundle.invalid_diagnoses), 1)
            self.assertIn("字符串数组", bundle.invalid_diagnoses[0]["reasons"][0])

    def test_illegal_json_reported_structure_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            bundle = parse_and_validate("这不是JSON", obs, "g", "test", ["c"], {}, True)
            self.assertIn("JSON 解析失败", bundle.errors[0])
            self.assertEqual(bundle.valid_diagnoses, [])

    def test_bounded_repair_one_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            gw = fake_gateway(["抱歉，{不是json", GOOD_JSON])
            bundle = run_diagnosis(gw, "目标", "", obs)
            self.assertEqual(len(bundle.call_ids), 2)
            self.assertEqual(len(bundle.valid_diagnoses), 1)

    def test_prompt_contains_observation_and_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = make_observation(tmp)
            registry = self.registry
            context, _ = select_for_task(
                registry, task_label="t", task_keywords=["单页活动介绍页"], decision_needs=["信息密度"]
            )
            prompt = build_user_prompt("目标", "像促销广告", obs, context)
            self.assertIn("[观察记录]", prompt)
            self.assertIn("[边界]", prompt)
            self.assertIn("IH-3", prompt)  # 领域依据片段进入提示词


class CliTests(unittest.TestCase):
    def _run_cli(self, *args, strip_key=True):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if strip_key:
            env["DEEPSEEK_API_KEY"] = ""
        return subprocess.run(
            [sys.executable, "-m", "mentis", "diagnose", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=env,
        )

    def test_no_credentials_exits_with_guidance(self):
        result = self._run_cli("--page", "runtime/demo/event_page_v1.html")
        if result.returncode == 0:
            self.fail("无凭据不应成功")
        self.assertIn("缺少凭据", result.stdout + result.stderr)
        self.assertIn("DEEPSEEK_API_KEY", result.stdout + result.stderr)

    def test_fixture_mode_is_labeled_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "page.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write(SAMPLE_PAGE)
            result = self._run_cli("--page", page, "--fixture", FIXTURE)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            out = result.stdout
            self.assertIn("Mock", out)
            self.assertIn("虚构证据位置", out)  # 夹具中的坏示例被拒收
            self.assertIn("缺三要素", out)
            self.assertIn("[已保存]", out)

    def test_credentials_alone_do_not_send_data(self):
        from mentis.diagnose_cli import run_diagnose

        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "page.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write(SAMPLE_PAGE)
            args = SimpleNamespace(page=page, fixture="", real=False, goal="", feedback="")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}):
                with patch("mentis.diagnose_cli.run_diagnosis", side_effect=AssertionError("should not call")):
                    self.assertEqual(run_diagnose(args), 2)


if __name__ == "__main__":
    unittest.main()
