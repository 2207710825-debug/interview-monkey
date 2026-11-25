"""带连接复用的 OpenAI 兼容流式 LLM 提供器。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import urlparse


class LLMProviderError(RuntimeError):
    def __init__(self, category: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    checker = getattr(cancel, "is_set", None)
    return bool(checker()) if checker else bool(cancel)


class OpenAICompatLLMProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        temperature: float = 0.2,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        trust_env: bool | None = None,
    ):
        if not 0.0 <= temperature <= 0.3:
            raise ValueError("实时问答 temperature 必须在 0~0.3")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        # 本机 Ollama/网关绝不能走系统代理：Windows 的 httpx 会读取注册表代理，
        # 127.0.0.1 也可能被转发到 10808，导致 503 或 4 秒以上的额外延迟。
        host = urlparse(self.base_url).hostname
        self.trust_env = (
            host not in {"127.0.0.1", "localhost", "::1"}
            if trust_env is None else trust_env
        )
        self._client = None

    async def _get_client(self):
        if self._client is None:
            import httpx
            headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            timeout = httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
            self._client = httpx.AsyncClient(
                http2=True, headers=headers, timeout=timeout, trust_env=self.trust_env,
            )
        return self._client

    @staticmethod
    def _classify(exc: Exception) -> LLMProviderError:
        try:
            import httpx
            if isinstance(exc, httpx.TimeoutException):
                return LLMProviderError("timeout", "LLM 请求超时", True)
            if isinstance(exc, httpx.NetworkError):
                return LLMProviderError("network", f"LLM 网络错误: {exc}", True)
        except ImportError:
            pass
        if isinstance(exc, LLMProviderError):
            return exc
        return LLMProviderError("unknown", str(exc), False)

    async def stream(self, messages: Sequence[Mapping[str, str]], cancel=None) -> AsyncIterator[str]:
        client = await self._get_client()
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "stream": True,
        }
        for attempt in range(self.max_retries + 1):
            emitted = False
            try:
                async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as response:
                    if response.status_code in {401, 403}:
                        raise LLMProviderError("auth", "LLM 鉴权失败，请检查 API Key", False)
                    if response.status_code == 429:
                        raise LLMProviderError("rate_limit", "LLM 请求被限流", True)
                    if response.status_code >= 500:
                        raise LLMProviderError("server", f"LLM 服务错误: HTTP {response.status_code}", True)
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:300]
                        raise LLMProviderError("request", f"LLM 请求失败: HTTP {response.status_code} {body}", False)
                    async for line in response.aiter_lines():
                        if _cancelled(cancel):
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                            delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                        except (json.JSONDecodeError, IndexError, TypeError):
                            continue
                        if delta:
                            emitted = True
                            yield str(delta)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = self._classify(exc)
                # 已经向前端吐过 token 时不能从头重试，否则会产生重复回答。
                if emitted or not error.retryable or attempt >= self.max_retries:
                    raise error from exc
                await asyncio.sleep(0.4 * (2 ** attempt))

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()
