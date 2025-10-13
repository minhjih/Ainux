"""HTTP client for GPT-style chat completions."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .config import ProviderSettings


class ChatClientError(RuntimeError):
    """Raised when a chat completion request fails."""


@dataclass
class ChatCompletion:
    """Structured response returned by the chat client."""

    role: str
    content: str
    raw: Dict[str, object]
    usage: Optional[Dict[str, object]] = None


class ChatClient:
    """Minimal client for calling chat completion APIs."""

    def __init__(self, settings: ProviderSettings, *, timeout: int = 60):
        self._settings = settings
        self._timeout = timeout

    @property
    def settings(self) -> ProviderSettings:
        return self._settings

    def _endpoint(self) -> str:
        base = self._settings.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        if self._settings.organization:
            headers["OpenAI-Organization"] = self._settings.organization
        for key, value in self._settings.extra_headers.items():
            headers[key] = value
        return headers

    def _request(self, payload: Dict[str, object]) -> Dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(), data=body, headers=self._build_headers(), method="POST"
        )
        start = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise ChatClientError(f"Provider returned HTTP {exc.code}: {message}")
        except urllib.error.URLError as exc:
            raise ChatClientError(f"Failed to reach provider: {exc}")
        latency = time.time() - start
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChatClientError(f"Unable to parse JSON response ({exc}) -> {raw}")
        data.setdefault("latency", latency)
        return data

    def create_chat_completion(
        self,
        messages: Iterable[Dict[str, object]],
        *,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, object]] = None,
        extra_options: Optional[Dict[str, object]] = None,
    ) -> ChatCompletion:
        payload: Dict[str, object] = {
            "model": self._settings.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        if extra_options:
            payload.update(extra_options)

        data = self._request(payload)
        choices = data.get("choices")
        if not choices:
            raise ChatClientError("Model response did not contain any choices")
        first = choices[0]
        message = first.get("message") or {}
        content = str(message.get("content", ""))
        role = str(message.get("role", "assistant"))
        usage = data.get("usage")
        return ChatCompletion(role=role, content=content, raw=data, usage=usage)


def format_usage(usage: Optional[Dict[str, object]]) -> str:
    if not usage:
        return ""
    consumed = []
    prompt = usage.get("prompt_tokens")
    if prompt is not None:
        consumed.append(f"prompt={prompt}")
    completion = usage.get("completion_tokens")
    if completion is not None:
        consumed.append(f"completion={completion}")
    total = usage.get("total_tokens")
    if total is not None:
        consumed.append(f"total={total}")
    return ", ".join(str(item) for item in consumed if item)
