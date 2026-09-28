"""DeepSeek 官方 OpenAI 兼容网关（stdlib 实现，无第三方依赖）。

- 模型 ID 依据 ADR-007：deepseek-v4.1-flash（2026-09-28 核对官方文档）。
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

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4.1-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY"


@dataclass
class ModelConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = API_KEY_ENV
    timeout_s: int = 60
    max_tokens: int = 2000
    temperature: float = 0.2


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
        self._transport = transport or self._http_transport
        self.force_mock = False  # fixture 模式下由调用方置 True，记录必须标注 Mock
        self.calls: list[CallRecord] = []
        self._n = 0

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
        self._n += 1
        mock = mock or self.force_mock or (self._transport is not self._http_transport)
        record = CallRecord(
            request_id=f"call-{self._n:03d}",
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
            record.response_text = self._transport(system, user_prompt, json_mode)
            record.ok = True
        except GatewayError as exc:
            record.error = str(exc)
        except Exception as exc:  # 网络/解析等异常统一记录，不外泄密钥
            record.error = f"{type(exc).__name__}: {exc}"
        record.latency_ms = int((time.monotonic() - started) * 1000)
        self.calls.append(record)
        return record

    # ---- 默认 HTTP 传输 ----

    def _http_transport(self, system: str, user_prompt: str, json_mode: bool) -> str:
        if not self.has_credentials():
            raise GatewayError(
                f"缺少凭据：请设置环境变量 {self.config.api_key_env}"
                "（DeepSeek API key），不入库、不写入日志"
            )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
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
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # pragma: no cover
                pass
            raise GatewayError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"网络错误: {exc.reason}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError(f"响应结构不符合预期: {json.dumps(data)[:300]}") from exc

    # ---- 用量解析 ----

    @staticmethod
    def extract_usage(record: CallRecord, raw_response: dict) -> None:
        """从原始响应提取 usage（调用方持有原始响应时使用）。"""
        record.usage = raw_response.get("usage", {}) or {}

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
