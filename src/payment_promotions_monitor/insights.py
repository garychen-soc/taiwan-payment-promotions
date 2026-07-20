from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


HIGH_RETURN_PERCENT = 10.0
HIGH_FIXED_REWARD_AMOUNT = 100.0
UPCOMING_WINDOW_DAYS = 14
EXPIRING_SOON_WINDOW_DAYS = 7

_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[%％]")
_YUAN_AMOUNT_RE = re.compile(
    r"(?:(?:新臺幣|新台幣|臺幣|台幣|NTD|NT\$)\s*)?"
    r"(?<![\d.])([0-9]{1,9}(?:,[0-9]{3})*(?:\.\d+)?)\s*元"
)
_NT_AMOUNT_RE = re.compile(r"(?:NTD|NT\$)\s*([0-9]{1,9}(?:,[0-9]{3})*(?:\.\d+)?)", re.I)

_PERCENT_REWARD_AFTER = re.compile(r"^(?:等值)?(?:現金|點數|紅利點數|全點)?\s*(?:回饋|返現|回贈)")
_PERCENT_REWARD_BEFORE = re.compile(
    r"(?:最高(?:可)?(?:享)?|加碼(?:最高)?(?:享)?|再加碼(?:最高)?(?:享)?|"
    r"享(?:有)?|可享|回饋|返現|回贈)\s*$"
)
_PERCENT_CAP_BEFORE = re.compile(r"(?:回饋|返現)(?:總額|總金額|金額)?上限\s*$")
_NON_REWARD_PERCENT_AFTER = re.compile(r"^(?:的)?\s*(?:手續費|服務費|利率|稅|折扣)")

_FIXED_ACTION_BEFORE = re.compile(
    r"(?:送|贈|贈送|可得|獲得|享|可享|享有|回饋|回饋金)"
    r"\s*(?:價值\s*)?(?:(?:新臺幣|新台幣|臺幣|台幣|NTD|NT\$)\s*)?$"
)
_FIXED_BENEFIT_AFTER = re.compile(
    r"^(?:等值)?(?:現金)?\s*(?:回饋|返現|優惠券|折價券|抵用券|購物金|現金券)"
)
_FIXED_EXCLUDED_BEFORE = re.compile(
    r"(?:消費|單筆|累積|交易|訂單)?\s*(?:滿|達|超過)\s*$|"
    r"(?:個人|每人|每戶|每帳戶|每卡|每月|單筆|活動)?(?:最高)?"
    r"(?:回饋|回饋金|回饋金額|總回饋|總金額)?\s*(?:上限|為限)\s*$"
)
_FIXED_EXCLUDED_AFTER = re.compile(r"^\s*(?:為限|上限|額滿|封頂)")


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _activity_text(item: dict[str, Any]) -> str:
    """Collect prose fields without stringifying dates or numeric metadata."""
    parts: list[str] = []
    for key in ("title", "conditions_summary", "description", "content", "summary", "highlights"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(entry).strip() for entry in value if isinstance(entry, str) and entry.strip())

    for evidence in item.get("evidence", []):
        if isinstance(evidence, dict):
            excerpt = evidence.get("excerpt")
            if isinstance(excerpt, str) and excerpt.strip():
                parts.append(excerpt.strip())
    return "\n".join(parts)


def _reward_percentages(text: str) -> list[float]:
    values: list[float] = []
    for match in _PERCENT_RE.finditer(text):
        value = float(match.group(1))
        if value <= 0 or value > 1000:
            continue

        before = text[max(0, match.start() - 24) : match.start()]
        after = text[match.end() : match.end() + 20]
        compact_before = re.sub(r"\s+", "", before)
        compact_after = re.sub(r"\s+", "", after)

        if _PERCENT_CAP_BEFORE.search(compact_before):
            continue
        if _NON_REWARD_PERCENT_AFTER.search(compact_after):
            continue
        if _PERCENT_REWARD_AFTER.search(compact_after) or _PERCENT_REWARD_BEFORE.search(compact_before):
            values.append(value)
    return values


def _is_fixed_reward_context(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 32) : start]
    after = text[end : end + 22]
    compact_before = re.sub(r"\s+", "", before)
    compact_after = re.sub(r"\s+", "", after)

    if _FIXED_EXCLUDED_AFTER.search(compact_after):
        return False
    if _FIXED_EXCLUDED_BEFORE.search(compact_before[-22:]):
        return False

    action_before = bool(_FIXED_ACTION_BEFORE.search(compact_before))
    benefit_after = bool(_FIXED_BENEFIT_AFTER.search(compact_after))
    return action_before and benefit_after


def _fixed_reward_amounts(text: str) -> list[float]:
    values: list[float] = []
    occupied: list[tuple[int, int]] = []
    for match in _YUAN_AMOUNT_RE.finditer(text):
        if not _is_fixed_reward_context(text, match.start(), match.end()):
            continue
        values.append(float(match.group(1).replace(",", "")))
        occupied.append(match.span())

    for match in _NT_AMOUNT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        if not _is_fixed_reward_context(text, match.start(), match.end()):
            continue
        values.append(float(match.group(1).replace(",", "")))
    return values


def _display_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _json_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def analyze_activity(item: dict[str, Any], today: date) -> dict[str, Any]:
    """Return deterministic, human-facing promotion insights.

    ``is_upcoming`` covers activities starting in the next 14 days, while
    ``is_expiring_soon`` covers activities ending today or in the next 7 days.
    Reward values are extracted only from explicit reward language; spending
    thresholds, campaign budgets and per-person caps are intentionally ignored.
    """
    text = _activity_text(item)
    percentages = _reward_percentages(text)
    fixed_amounts = _fixed_reward_amounts(text)
    max_percent = max(percentages, default=None)
    fixed_amount = max(fixed_amounts, default=None)

    start_date = _coerce_date(item.get("start_date"))
    end_date = _coerce_date(item.get("end_date"))
    starts_in_days = (start_date - today).days if start_date else None
    ends_in_days = (end_date - today).days if end_date else None
    is_upcoming = starts_in_days is not None and 1 <= starts_in_days <= UPCOMING_WINDOW_DAYS
    is_expiring_soon = ends_in_days is not None and 0 <= ends_in_days <= EXPIRING_SOON_WINDOW_DAYS
    high_percentage = max_percent is not None and max_percent >= HIGH_RETURN_PERCENT
    high_fixed = fixed_amount is not None and fixed_amount >= HIGH_FIXED_REWARD_AMOUNT
    is_high_return = high_percentage or high_fixed

    tags: list[str] = []
    if high_percentage:
        tags.append("高回饋")
    if high_fixed:
        tags.append("高額回饋")
    if is_upcoming:
        tags.append("即將開始")
    if is_expiring_soon:
        tags.append("即將結束")

    quota_status = item.get("quota_status")
    quota_tag = {
        "sold_out": "已額滿",
        "partial_sold_out": "部分額滿",
        "unknown_app_only": "需至 App 確認",
    }.get(quota_status)
    if quota_tag:
        tags.append(quota_tag)

    summary_parts: list[str] = []
    if max_percent is not None:
        phrase = f"最高 {_display_number(max_percent)}% 回饋"
        if high_percentage:
            phrase += "，屬高回饋活動"
        summary_parts.append(phrase)
    if fixed_amount is not None:
        phrase = f"固定回饋最高 {_display_number(fixed_amount)} 元"
        if high_fixed:
            phrase += "，屬高額回饋"
        summary_parts.append(phrase)
    if is_upcoming:
        summary_parts.append(f"將於 {starts_in_days} 天後開始")
    elif starts_in_days == 0:
        summary_parts.append("今天開始")
    if is_expiring_soon:
        summary_parts.append("今天截止" if ends_in_days == 0 else f"將於 {ends_in_days} 天後截止")
    if quota_status == "sold_out":
        summary_parts.append("官方資訊顯示活動已額滿")
    elif quota_status == "partial_sold_out":
        summary_parts.append("部分期別或子活動已額滿")
    elif quota_status == "unknown_app_only":
        summary_parts.append("額滿狀態需至 App 確認")

    if not summary_parts:
        summary_parts.append("尚未辨識出明確回饋幅度，請查看官方活動辦法")

    return {
        "max_reward_percent": _json_number(max_percent),
        "fixed_reward_amount": _json_number(fixed_amount),
        "is_high_return": is_high_return,
        "is_upcoming": is_upcoming,
        "starts_in_days": starts_in_days,
        "is_expiring_soon": is_expiring_soon,
        "ends_in_days": ends_in_days,
        "insight_tags": tags,
        "human_summary": "；".join(summary_parts) + "。",
    }
