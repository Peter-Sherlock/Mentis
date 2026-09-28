"""DeepSeek 官方 OpenAI 兼容网关（stdlib 实现，无第三方依赖）。

- 模型 ID 依据 ADR-007：deepseek-flash（DeepSeek-V4.1-Flash）。
- 凭据只从环境变量读取（DEEPSEEK_API_KEY），任何记录不得包含密钥。
- transport 可注入：测试用假传输，不触网。
- 不做自动重发风暴：失败有界（本次调用内最多一次有界 JSON 修复由上层负责，
  网关本身不重试）。
"""

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY"


@dataclass
class ModelConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = API_KEY_ENV
    timeout_s: int = 60
    max_tokens: int = 2000
    temperature: float = 0.2
    max_input_chars: int = 25000


@dataclass
class CallRecord:
    request_id: str
    model: str
    host: str                 # 只记主机，不记完整 URL 与密钥
    ok: bool
    latency_ms: int
    created_at: str
    prompt_sha256: str        # 提示词指纹（不入库全文，供排障对齐）
    response_text: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""
    mock: bool = False        # Mock 调用必须显式标注


class GatewayError(RuntimeError):
    pass


class ModelGateway:
    def __init__(self, config: ModelConfig | None = None, transport=None):
        self.config = config or ModelConfig()
        self._injected_transport = transport is not None
        self._transport = transport if transport is not None else self._http_transport
        self.force_mock = False  # fixture 模式下由调用方置 True，记录必须标注 Mock
        self.calls: list[CallRecord] = []

    @property
    def api_key(self) -> str:
        import os

        return os.environ.get(self.config.api_key_env, "")

    def has_credentials(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user_prompt: str, json_mode: bool = True, mock: bool = False) -> CallRecord:
        """一次调用；返回 CallRecord（含 usage 与错误），失败不抛异常而是记录。

        注入传输（测试替身）或 fixture 模式一律标注 Mock，防止混入真实成功数。
        """
        mock = mock or self.force_mock or self._injected_transport
        record = CallRecord(
            request_id=f"call-{uuid4().hex[:16]}",
            model=self.config.model,
            host=urlsplit(self.config.base_url).netloc,
            ok=False,
            latency_ms=0,
            created_at=datetime.now().isoformat(timespec="seconds"),
            prompt_sha256=hashlib.sha256((system + user_prompt).encode("utf-8")).hexdigest()[:16],
            mock=mock,
        )
        started = time.monotonic()
        try:
            result = self._transport(system, user_prompt, json_mode)
            if isinstance(result, tuple) and len(result) == 2:
                record.response_text, raw_usage = result
                if isinstance(raw_usage, dict):
                    record.usage = {
                        key: value for key, value in raw_usage.items()
                        if key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                        and type(value) is int and value >= 0
                    }
            else:
                record.response_text = result
            if not isinstance(record.response_text, str):
                raise GatewayError("响应 content 不是字符串")
            record.ok = True
        except GatewayError as exc:
            record.error = str(exc)
        except Exception as exc:  # 异常消息可能含请求内容或凭据；只记录类型
            record.error = type(exc).__name__
        record.latency_ms = int((time.monotonic() - started) * 1000)
        self.calls.append(record)
        return record

    # ---- 默认 HTTP 传输 ----

    def _http_transport(self, system: str, user_prompt: str, json_mode: bool) -> tuple[str, dict]:
        if not self.has_credentials():
            raise GatewayError(
                f"缺少凭据：请设置环境变量 {self.config.api_key_env}"
                "（DeepSeek API key），不入库、不写入日志"
            )
        if len(system) + len(user_prompt) > self.config.max_input_chars:
            raise GatewayError(f"输入过长：超过本轮 {self.config.max_input_chars} 字符上限，未发送")
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"网络错误: {type(exc.reason).__name__}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise GatewayError("响应 content 为空")
            return content, data.get("usage", {})
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError("响应结构不符合预期") from exc

    # ---- 用量汇总 ----
    def usage_summary(self) -> dict:
        ok_calls = [c for c in self.calls if c.ok and not c.mock]
        return {
            "real_calls": len(ok_calls),
            "mock_calls": len([c for c in self.calls if c.mock and c.ok]),
            "failed_calls": len([c for c in self.calls if not c.ok]),
            "prompt_tokens": sum(c.usage.get("prompt_tokens", 0) for c in ok_calls),
            "completion_tokens": sum(c.usage.get("completion_tokens", 0) for c in ok_calls),
            "model": self.config.model,
        }
