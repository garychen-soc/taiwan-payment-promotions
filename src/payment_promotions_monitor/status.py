from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlsplit

from .fetch import canonical_url


SOLD_OUT_PATTERNS = (
    re.compile(r"(?:[（(]\s*活動額滿\s*[）)]|已(?:於.{0,24})?(?:全數|全部)?額滿|業已(?:全數|全部)?額滿)"),
    re.compile(r"(?:回饋|額度|名額|活動).{0,30}(?:已達|達到).{0,12}(?:上限|額度)"),
    re.compile(r"已(?:贈|送|領|兌換|發放)(?:罄|完)"),
    re.compile(r"(?:已|自.{0,20}起)(?:停止回饋|提前結束)"),
)
CONDITIONAL_PATTERNS = (
    re.compile(r"(?:若|如|倘|一旦).{0,40}(?:額滿|達.{0,8}上限|停止回饋|提前結束)"),
    re.compile(r"(?:是否|有無).{0,12}(?:額滿|達.{0,8}上限)"),
    re.compile(r"(?:額滿|送完|用完|達.{0,8}上限)(?:為止|即止|即停止)"),
    re.compile(r"(?:額滿|達.{0,8}上限).{0,30}(?:將|會|另行|另於).{0,20}(?:公告|停止|結束)"),
    re.compile(r"除.{0,8}(?:已達|達到).{0,35}(?:上限|額滿)(?:外|者)"),
    re.compile(r"(?:當|若|如).{0,45}(?:出現|顯示|標示).{0,18}(?:已額滿|額滿)"),
)
APP_ONLY_PATTERNS = (
    re.compile(r"(?:App|APP|應用程式).{0,30}額滿公告"),
    re.compile(r"(?:額滿|回饋).{0,45}(?:僅公告於|僅於|請至).{0,35}(?:App|APP|應用程式)"),
    re.compile(r"(?:額滿|剩餘|回饋).{0,35}(?:僅|請).{0,20}(?:App|APP|應用程式).{0,30}(?:公告|查詢|顯示|通知)"),
    re.compile(r"(?:App|APP|應用程式).{0,30}(?:最新消息|公告).{0,30}(?:額滿|回饋上限)"),
    re.compile(r"(?:本網站|官網).{0,20}不另行公告.{0,30}(?:App|APP)"),
)
WHOLE_EVENT_PATTERNS = (
    re.compile(r"(?:本活動|活動回饋|總回饋).{0,24}(?:全數|全部)?(?:已.{0,12})?額滿"),
    re.compile(r"(?:所有|全部).{0,16}(?:名額|回饋|額度).{0,12}(?:額滿|用罄)"),
)
PARTIAL_PATTERNS = (
    re.compile(r"(?:\d{1,2}\s*月(?:份)?|活動[一二三四1234]|第[一二三四1234]重|加碼|指定通路|指定銀行).{0,40}(?:額滿|已達上限)"),
    re.compile(r"(?:額滿|已達上限).{0,40}(?:\d{1,2}\s*月(?:份)?|活動[一二三四1234]|第[一二三四1234]重|加碼)"),
    re.compile(r"(?:首週|本週|第[一二三四五六七八九十\d]+週|週次).{0,40}(?:額滿|已達上限)"),
    re.compile(r"(?:額滿|已達上限).{0,40}(?:首週|本週|第[一二三四五六七八九十\d]+週|週次)"),
)


@dataclass(frozen=True, slots=True)
class QuotaAssessment:
    status: str
    evidence_excerpt: str = ""
    evidence_complete: bool = True
    sold_out_at: str | None = None
    components: list[dict[str, str]] = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[。！？!?；;])|\n+", text.replace("\xa0", " "))
    return [re.sub(r"\s+", " ", chunk).strip() for chunk in chunks if chunk.strip()]


def _is_conditional(sentence: str) -> bool:
    if (
        re.search(r"(?:每人|每戶|每卡|每一帳戶|單一帳戶).{0,35}(?:上限|額滿)", sentence)
        and not re.search(r"(?:活動總|每月總|總回饋|總贈點|總名額).{0,8}上限", sentence)
    ):
        return True
    if re.search(r"(?:舉例|例如|假設|範例).{0,100}(?:已達|額滿)", sentence):
        return True
    if re.search(r"(?:已於|業已|截至.{0,20}(?:已|額滿))", sentence):
        return False
    return any(pattern.search(sentence) for pattern in CONDITIONAL_PATTERNS)


def _sold_out_date(sentence: str, event_year: int | None) -> str | None:
    full = re.search(r"(?P<y>20\d{2})[年/.\-](?P<m>\d{1,2})[月/.\-](?P<d>\d{1,2})日?", sentence)
    short = re.search(r"(?<!\d)(?P<m>\d{1,2})[月/](?P<d>\d{1,2})日?", sentence)
    match = full or short
    if not match:
        return None
    year = int(match.groupdict().get("y") or event_year or datetime.now().year)
    try:
        return date(year, int(match["m"]), int(match["d"])).isoformat()
    except ValueError:
        return None


def _component_from(sentence: str) -> dict[str, str]:
    month = re.search(r"(?<!\d)(\d{1,2})\s*月(?:份)?", sentence)
    week = re.search(r"(首週|本週|第[一二三四五六七八九十\d]+週)", sentence)
    component = re.search(r"(活動[一二三四1234]|第[一二三四1234]重|[^，。；]{0,16}加碼)", sentence)
    result = {"status": "sold_out", "evidence": sentence[:300]}
    if month:
        result["period"] = f"{int(month.group(1)):02d}月"
    elif week:
        result["period"] = week.group(1)
    if component:
        result["component"] = component.group(1).strip()
    return result


def analyze_quota(text: str, *, event_start: date | None = None) -> QuotaAssessment:
    explicit: list[str] = []
    for sentence in _sentences(text):
        if _is_conditional(sentence):
            continue
        if any(pattern.search(sentence) for pattern in SOLD_OUT_PATTERNS):
            explicit.append(sentence)

    if explicit:
        excerpt = explicit[0][:500]
        partial_sentences = [sentence for sentence in explicit if any(pattern.search(sentence) for pattern in PARTIAL_PATTERNS)]
        whole_event = any(
            any(pattern.search(sentence) for pattern in WHOLE_EVENT_PATTERNS)
            and not any(pattern.search(sentence) for pattern in PARTIAL_PATTERNS)
            for sentence in explicit
        )
        if partial_sentences and not whole_event:
            components = [_component_from(sentence) for sentence in partial_sentences]
            return QuotaAssessment(
                "partial_sold_out",
                excerpt,
                True,
                _sold_out_date(excerpt, event_start.year if event_start else None),
                components,
            )
        return QuotaAssessment(
            "sold_out",
            excerpt,
            True,
            _sold_out_date(excerpt, event_start.year if event_start else None),
        )

    app_sentence = next(
        (sentence for sentence in _sentences(text) if any(pattern.search(sentence) for pattern in APP_ONLY_PATTERNS)),
        "",
    )
    if app_sentence:
        return QuotaAssessment("unknown_app_only", app_sentence[:500], False)
    return QuotaAssessment("not_marked_full")


def _event_identifiers(url: str, text: str = "") -> set[str]:
    identifiers: set[str] = set()
    parsed = urlsplit(url)
    for key, value in parse_qsl(parsed.query):
        if key.lower() in {"eventid", "id", "event_id", "nid"} and value:
            identifiers.add(value.lower())
    identifiers.update(match.lower() for match in re.findall(r"(?:EventId|event_id|活動編號)[=:：\s]+([A-Za-z0-9_-]{2,})", text, re.I))
    return identifiers


def _normalize_title(title: str) -> str:
    value = re.sub(r"【[^】]*(?:額滿|公告)[^】]*】", "", title)
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value).lower()
    return re.sub(r"(?:額滿公告|額滿通知|活動公告|優惠活動)", "", value)


def _mentioned_months(text: str) -> set[str]:
    months = set(re.findall(r"(?<!\d)(\d{1,2})\s*月", text))
    months.update(re.findall(r"20\d{2}[年/.\-](\d{1,2})[月/.\-]", text))
    return {str(int(value)) for value in months if 1 <= int(value) <= 12}


def _core_title(title: str) -> str:
    value = _normalize_title(title)
    for word in (
        "ipassmoney",
        "linepaymoney",
        "icashpay",
        "一卡通",
        "悠遊付",
        "全支付",
        "台灣pay",
        "街口支付",
        "橘子支付",
        "活動",
        "優惠",
        "回饋",
        "額滿",
        "公告",
        "通知",
        "最高",
        "已於",
    ):
        value = value.replace(word, "")
    return re.sub(r"[0-9年月日至起止]", "", value)


def match_announcement(
    *,
    activity_url: str,
    activity_title: str,
    activity_text: str,
    announcement_url: str,
    announcement_title: str,
    announcement_text: str,
) -> tuple[bool, str, float]:
    if activity_url and announcement_url and activity_url.split("#", 1)[0] == announcement_url.split("#", 1)[0]:
        return True, "same_page", 1.0
    activity_ids = _event_identifiers(activity_url, activity_text)
    announcement_ids = _event_identifiers(announcement_url, announcement_text)
    announcement_urls = {
        match.rstrip(".,);]")
        for match in re.findall(r"https?://[^\s<>'\"]+", announcement_text)
    }
    if activity_url and any(canonical_url(url) == canonical_url(activity_url) for url in announcement_urls):
        return True, "event_id_or_url", 1.0
    if activity_ids & announcement_ids:
        return True, "event_id_or_url", 1.0
    a_title = _normalize_title(activity_title)
    n_title = _normalize_title(announcement_title)
    if not a_title or not n_title:
        return False, "insufficient", 0.0
    generic = {"活動訊息", "最新消息", "優惠活動", "活動專區", "公告", "台灣pay", "橘子支付", "linepaymoney"}
    if a_title in generic or n_title in generic:
        return False, "insufficient", 0.0
    score = SequenceMatcher(None, a_title, n_title).ratio()
    month_a = _mentioned_months(activity_title + activity_text[:1000])
    month_n = _mentioned_months(announcement_title + announcement_text[:1000])
    if month_a and month_n and not (month_a & month_n):
        return False, "period_conflict", score
    if score >= 0.78:
        return True, "normalized_title_period", score
    a_core = _core_title(activity_title)
    n_core = _core_title(announcement_title)
    shorter, longer = sorted((a_core, n_core), key=len)
    if len(shorter) >= 3 and shorter in longer:
        return True, "normalized_title_period", score
    return False, "low_confidence", score
