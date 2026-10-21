"""Клиенты языковых моделей с единым интерфейсом chat(system, user) → текст."""
from __future__ import annotations

import time
import uuid
from typing import Protocol

import httpx

from app.config import Settings


class LLM(Protocol):
    name: str

    def chat(self, system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 2500) -> str | None:
        """Возвращает текст ответа или None при ошибке сети/сервера (вызывающий код сам решает, что делать)."""


class GigaChatLLM:
    OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self, auth_key: str, scope: str, model: str, verify_ssl: bool = True,
                 client: httpx.Client | None = None) -> None:
        self.name = f"gigachat:{model}"
        self.auth_key, self.scope, self.model = auth_key, scope, model
        self.http = client or httpx.Client(verify=verify_ssl, timeout=90)
        self._token, self._exp = "", 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._exp:
            return self._token
        r = self.http.post(self.OAUTH, data={"scope": self.scope}, headers={
            "Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        r.raise_for_status()
        self._token, self._exp = r.json()["access_token"], time.time() + 25 * 60
        return self._token

    def chat(self, system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 2500) -> str | None:
        try:
            r = self.http.post(self.CHAT, headers={"Authorization": f"Bearer {self._access_token()}"}, json={
                "model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None


class OpenAICompatibleLLM:
    """OpenAI, vLLM, Ollama и любой шлюз с методом /chat/completions."""

    def __init__(self, base_url: str, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        self.name = f"openai-compatible:{model}"
        self.url, self.key, self.model = base_url.rstrip("/") + "/chat/completions", api_key, model
        self.http = client or httpx.Client(timeout=120)

    def chat(self, system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 2500) -> str | None:
        try:
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            r = self.http.post(self.url, headers=headers, json={
                "model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None


def make_llm(settings: Settings) -> LLM | None:
    """Выбирает модель по настройкам; None — модель не настроена (остаётся разбор по правилам)."""
    if settings.gigachat_auth_key:
        return GigaChatLLM(settings.gigachat_auth_key, settings.gigachat_scope, settings.gigachat_model,
                           settings.gigachat_verify_ssl)
    if settings.llm_base_url and settings.llm_model:
        return OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    return None
