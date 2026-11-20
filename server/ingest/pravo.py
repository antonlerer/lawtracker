"""Источник: официальный портал правовой информации (publication.pravo.gov.ru).

Эндпоинты и поля те же, что использует парсер проекта (RSS → API документа).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import logging
import time

import httpx

BASE_URL = "http://publication.pravo.gov.ru"
RSS_URL = f"{BASE_URL}/api/Rss"
DOCUMENT_URL = f"{BASE_URL}/api/Document"
EO_RE = re.compile(r"Номер опубликования:\s*([0-9]+)")


@dataclass
class RawAct:
    eo_number: str
    type: str
    date: str          # как отдаёт портал (ГГГГ-ММ-ДД...)
    number: str
    title: str
    document_url: str


def parse_rss(xml_text: str) -> list[str]:
    """Возвращает уникальные номера опубликования из RSS-ленты (в порядке появления)."""
    root = ET.fromstring(xml_text)
    seen: list[str] = []
    for item in root.findall(".//item"):
        desc = (item.findtext("description") or "").strip()
        m = EO_RE.search(desc)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def parse_document(eo_number: str, payload: dict) -> RawAct | None:
    title = (payload.get("name") or "").strip()
    if not title:
        return None
    return RawAct(
        eo_number=eo_number, type=((payload.get("documentType") or {}).get("name") or "").strip(),
        date=(payload.get("documentDate") or "")[:10], number=(payload.get("number") or "").strip(), title=title,
        document_url=f"{BASE_URL}/document/{eo_number}")


BACKOFF = 2.0     # секунд; пауза растёт вдвое с каждой попыткой (в тестах обнуляется)
log = logging.getLogger("lawtracker.ingest")


def get_with_retry(client: httpx.Client, url: str, *, params: dict | None = None, timeout: float = 30,
                   retries: int = 3) -> httpx.Response:
    """GET с повторами при сетевых ошибках, таймаутах, 429 и 5xx. Остальные 4xx повторять бессмысленно."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = client.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code < 500 and exc.response.status_code != 429:
                break
        except httpx.HTTPError as exc:
            last = exc
        if attempt + 1 < retries:
            time.sleep(BACKOFF * (2 ** attempt))
    log.warning("не удалось получить %s: %s", url, last)
    raise last  # type: ignore[misc]


def fetch_rss(client: httpx.Client) -> str:
    return get_with_retry(client, RSS_URL).text


def fetch_document(client: httpx.Client, eo_number: str, retries: int = 3) -> RawAct | None:
    try:
        return parse_document(eo_number, get_with_retry(client, DOCUMENT_URL, params={"eoNumber": eo_number},
                                                        retries=retries).json())
    except (httpx.HTTPError, ValueError):
        return None
