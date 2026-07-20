from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .fetch import FetchResult, load_json, looks_like_json
from .html_extract import best_title, parse_html


@dataclass(slots=True)
class Document:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]]
    content_hash: str
    raw_json: object | None = None


TITLE_KEYS = (
    "activity_name",
    "activity_title",
    "event_name",
    "event_title",
    "title",
    "subject",
    "name",
)


def _walk_json(value: object) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and item.strip():
                found.append((str(key), item.strip()))
            elif isinstance(item, (dict, list)):
                found.extend(_walk_json(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_json(item))
    return found


def _json_title(pairs: list[tuple[str, str]]) -> str:
    lowered = [(key.lower(), value) for key, value in pairs]
    for wanted in TITLE_KEYS:
        for key, value in lowered:
            if key == wanted and 3 <= len(re.sub(r"<[^>]+>", "", value)) <= 240:
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()
    return "未辨識活動名稱"


def _json_text(pairs: list[tuple[str, str]], base_url: str) -> tuple[str, list[tuple[str, str]]]:
    lines: list[str] = []
    links: list[tuple[str, str]] = []
    for key, value in pairs:
        if "<" in value and ">" in value:
            parsed = parse_html(value, base_url)
            cleaned = parsed.text
            links.extend(parsed.links)
        else:
            cleaned = re.sub(r"\s+", " ", value).strip()
        if cleaned:
            lines.append(f"{key}: {cleaned}")
        for match in re.findall(r"https?://[^\s<>'\"]+", value):
            links.append((match.rstrip(".,);]"), ""))
    return "\n".join(lines), links


def parse_document(result: FetchResult) -> Document:
    if looks_like_json(result):
        data = load_json(result)
        parse_target = data
        if isinstance(data, dict):
            body = data.get("body")
            detail = body.get("campaignDetail") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                allowed_detail_keys = {
                    "systemSeq",
                    "templateType",
                    "title",
                    "description",
                    "startDate",
                    "endDate",
                    "location",
                    "target",
                    "content",
                    "reminder",
                    "joinBank",
                    "restrictions",
                    "notes",
                    "monitor_review_required",
                    "monitor_note",
                }
                parse_target = {key: value for key, value in detail.items() if key in allowed_detail_keys}
        pairs = _walk_json(parse_target)
        text, links = _json_text(pairs, result.final_url)
        return Document(
            url=result.final_url,
            title=_json_title(pairs),
            text=text,
            links=links,
            content_hash=result.content_hash,
            raw_json=data,
        )
    parsed = parse_html(result.text, result.final_url)
    return Document(
        url=result.final_url,
        title=best_title(parsed),
        text=parsed.text,
        links=parsed.links,
        content_hash=result.content_hash,
    )


def external_id_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for key in ("EventId", "eventId", "event_id", "id", "nID"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    final_part = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", final_part):
        return final_part
    return None


def summarize_conditions(text: str, limit: int = 520) -> str:
    sentences = [
        re.sub(r"\s+", " ", item).strip()
        for item in re.split(r"(?<=[。！？!?；;])|\n+", text)
        if item.strip()
    ]
    selected: list[str] = []
    for sentence in sentences:
        if any(word in sentence for word in ("回饋", "上限", "名額", "指定", "每筆", "每戶", "每月", "活動期間")):
            selected.append(sentence[:220])
        if sum(len(item) for item in selected) >= limit:
            break
    return " ".join(selected)[:limit]
