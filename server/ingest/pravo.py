"""Источник: официальный портал правовой информации (publication.pravo.gov.ru).

Эндпоинты и поля те же, что использует парсер проекта (RSS → API документа).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

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


def fetch_rss(client: httpx.Client) -> str:
    r = client.get(RSS_URL, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_document(client: httpx.Client, eo_number: str, retries: int = 3) -> RawAct | None:
    for _ in range(retries):
        try:
            r = client.get(DOCUMENT_URL, params={"eoNumber": eo_number}, timeout=30)
            r.raise_for_status()
            return parse_document(eo_number, r.json())
        except (httpx.HTTPError, ValueError):
            continue
    return None
