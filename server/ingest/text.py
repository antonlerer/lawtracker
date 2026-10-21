"""Получение текста акта. Официальный портал отдаёт PDF (тот же адрес, что использует парсер проекта)."""
from __future__ import annotations

import io
import re

import httpx

from .pravo import BASE_URL


def clean_text(text: str) -> str:
    """Склеивает переносы слов, убирает номера страниц и лишние пробелы."""
    text = text.replace("­", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)                  # перенос слова через дефис в конце строки
    text = re.sub(r"\n\s*\d{1,3}\s*\n", "\n", text)               # строка, состоящая только из номера страницы
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pdf_to_text(data: bytes, max_pages: int = 80) -> str | None:
    """Достаёт текст из PDF. Для сканов без текстового слоя возвращает None (нужен OCR или ручной ввод)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
    except Exception:
        return None
    text = clean_text("\n".join(pages))
    return text if len(text) >= 80 else None


def fetch_text(client: httpx.Client, eo_number: str) -> str | None:
    try:
        r = client.get(f"{BASE_URL}/File/Pdf", params={"eoNumber": eo_number}, timeout=60)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return pdf_to_text(r.content)
