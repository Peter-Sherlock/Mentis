"""DeepSeek 官方 OpenAI 兼容网关（stdlib 实现，无第三方依赖）。

- 模型 ID 依据 ADR-007：deepseek-flash（DeepSeek-V4.1-Flash）。
- 凭据优先读取环境变量 DEEPSEEK_API_KEY；未定义时读取项目根目录 .env。
  任何记录不得包含密钥。
- transport 可注入：测试用假传输，不触网。
- 不做自动重发风暴：失败有界（本次调用内最多一次有界 JSON 修复由上层负责，
  网关本身不重试）。
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass
class ModelConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = API_KEY_ENV
    env_file: Path = DEFAULT_ENV_FILE
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
    image_refs: list = field(default_factory=list)  # ["名称;sha256前12位"]，不含图像本体


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
        # 已定义的环境变量始终优先，空值也能显式禁用本地 .env 回退。
        from_environment = os.environ.get(self.config.api_key_env)
        if from_environment is not None:
            return from_environment.strip()
        try:
            with Path(self.config.env_file).open(encoding="utf-8-sig") as env_file:
                for line in env_file:
                    name, separator, value = line.partition("=")
                    if separator and name.strip() == self.config.api_key_env:
                        value = value.strip()
                        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                            value = value[1:-1]
                        return value
        except (OSError, UnicodeError):
            pass
        return ""

    def has_credentials(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user_prompt: str, json_mode: bool = True, mock: bool = False,
                 images: list | None = None) -> CallRecord:
        """一次调用；返回 CallRecord（含 usage 与错误），失败不抛异常而是记录。

        images 为 [(名称, PNG 字节)] 时走多模态消息；记录只存名称与指纹，不存图像本体。
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
        if images:
            record.image_refs = [
                f"{name};{hashlib.sha256(blob).hexdigest()[:12]}" for name, blob in images]
            if self._injected_transport:
                record.error = "GatewayError: 注入传输不支持图像输入（仅默认 HTTP 传输）"
                record.latency_ms = 0
                self.calls.append(record)
                return record
        started = time.monotonic()
        try:
            if images:
                result = self._transport(system, user_prompt, json_mode, images)
            else:
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

    @staticmethod
    def build_content_parts(user_prompt: str, images: list) -> list:
        """OpenAI 兼容多模态 content：文本在前，图像（data URL）在后。"""
        parts = [{"type": "text", "text": user_prompt}]
        for _name, blob in images:
            parts.append({"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,"
                                               + base64.b64encode(blob).decode("ascii")}})
        return parts

    def _http_transport(self, system: str, user_prompt: str, json_mode: bool, images: list | None = None) -> tuple[str, dict]:
        if not self.has_credentials():
            raise GatewayError(
                f"缺少凭据：请设置环境变量 {self.config.api_key_env}"
                "，或在项目根目录 .env 中设置同名键；不入库、不写入日志"
            )
        if len(system) + len(user_prompt) > self.config.max_input_chars:
            raise GatewayError(f"输入过长：超过本轮 {self.config.max_input_chars} 字符上限，未发送")
        user_content: str | list = user_prompt
        if images:
            user_content = self.build_content_parts(user_prompt, images)
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
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
