"""Минимальный клиент GigaChat (OAuth + chat/completions) с проверкой TLS по умолчанию."""
from __future__ import annotations

import json
import re
import time
import uuid

import httpx

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


class GigaChat:
    def __init__(self, auth_key: str, scope: str = "GIGACHAT_API_PERS", model: str = "GigaChat-2",
                 verify_ssl: bool = True, client: httpx.Client | None = None) -> None:
        self.auth_key, self.scope, self.model = auth_key, scope, model
        self.http = client or httpx.Client(verify=verify_ssl, timeout=40)
        self._token, self._exp = "", 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._exp:
            return self._token
        r = self.http.post(OAUTH_URL, data={"scope": self.scope}, headers={
            "Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        r.raise_for_status()
        self._token, self._exp = r.json()["access_token"], time.time() + 25 * 60
        return self._token

    def complete_json(self, system: str, user: str) -> dict | None:
        """Возвращает разобранный JSON или None при любой ошибке (вызывающий код откатывается на правила)."""
        try:
            r = self.http.post(CHAT_URL, headers={"Authorization": f"Bearer {self._access_token()}"}, json={
                "model": self.model, "temperature": 0.1, "max_tokens": 900,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else None
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            return None
