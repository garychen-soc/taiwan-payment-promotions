from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


DATE_LABELS = (
    "活動期間",
    "活動時間",
    "活動日期",
    "優惠期間",
    "優惠時間",
    "適用期間",
    "回饋期間",
    "activity_start_time",
)
RANGE_SEP = r"(?:至|到|起至|～|~|—|–|-|－)"
DATE_DECORATION = (
    r"(?:\s*[（(][^）)\n]{0,12}[）)])?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
)
WESTERN_TOKEN = r"(?P<year>20\d{2})\s*(?:年|[./-])\s*(?P<month>\d{1,2})\s*(?:月|[./-])\s*(?P<day>\d{1,2})\s*日?"
ROC_TOKEN = r"(?:民國\s*)?(?P<roc_year>1\d{2})\s*(?:年|[./-])\s*(?P<roc_month>\d{1,2})\s*(?:月|[./-])\s*(?P<roc_day>\d{1,2})\s*日?"


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date | None
    end: date | None
    confidence: str
    excerpt: str = ""


def _build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _from_match(match: re.Match[str], prefix: str = "") -> date | None:
    groups = match.groupdict()
    if groups.get(f"{prefix}year"):
        year = int(groups[f"{prefix}year"])
        month = int(groups[f"{prefix}month"])
        day = int(groups[f"{prefix}day"])
    else:
        year = int(groups[f"{prefix}roc_year"]) + 1911
        month = int(groups[f"{prefix}roc_month"])
        day = int(groups[f"{prefix}roc_day"])
    return _build_date(year, month, day)


def _candidate_windows(text: str) -> list[tuple[str, str]]:
    compact = re.sub(r"[\t\r]+", " ", text)
    windows: list[tuple[str, str]] = []
    label_positions: list[int] = []
    for label in DATE_LABELS:
        for match in re.finditer(re.escape(label), compact):
            label_positions.append(match.start())
    # Preserve document order rather than DATE_LABELS declaration order. This
    # lets a page's primary「活動時間」win over a later generic card-benefit
    #「活動期間」section.
    for start in sorted(set(label_positions)):
        # Official pages frequently render a heading such as「一、活動時間」
        # and place the actual range in the next block. Keep a small number of
        # following lines so the label remains associated with its date,
        # without pulling an entire page of unrelated sub-promotions in.
        raw_window = compact[start : start + 520]
        window = " ".join(raw_window.split("\n")[:3])[:420]
        windows.append(("high", window))
    windows.append(("low", compact[:20_000]))
    return windows


def _western_range(window: str) -> tuple[date | None, date | None] | None:
    full = re.compile(
        rf"(?P<sy>20\d{{2}})\s*(?:年|[./-])\s*(?P<sm>\d{{1,2}})\s*(?:月|[./-])\s*(?P<sd>\d{{1,2}})\s*日?"
        rf"{DATE_DECORATION}\s*{RANGE_SEP}\s*"
        rf"(?:(?P<ey>20\d{{2}})\s*(?:年|[./-])\s*)?(?P<em>\d{{1,2}})\s*(?:月|[./-])\s*(?P<ed>\d{{1,2}})\s*日?"
    )
    match = full.search(window)
    if not match:
        return None
    start = _build_date(int(match["sy"]), int(match["sm"]), int(match["sd"]))
    end_year = int(match["ey"] or match["sy"])
    end = _build_date(end_year, int(match["em"]), int(match["ed"]))
    if start and end and end < start and not match["ey"]:
        end = _build_date(end_year + 1, int(match["em"]), int(match["ed"]))
    return start, end


def _roc_range(window: str) -> tuple[date | None, date | None] | None:
    pattern = re.compile(
        rf"(?:民國\s*)?(?P<sy>1\d{{2}})\s*(?:年|[./-])\s*(?P<sm>\d{{1,2}})\s*(?:月|[./-])\s*(?P<sd>\d{{1,2}})\s*日?"
        rf"{DATE_DECORATION}\s*{RANGE_SEP}\s*"
        rf"(?:民國\s*)?(?:(?P<ey>1\d{{2}})\s*(?:年|[./-])\s*)?(?P<em>\d{{1,2}})\s*(?:月|[./-])\s*(?P<ed>\d{{1,2}})\s*日?"
    )
    match = pattern.search(window)
    if not match:
        return None
    start = _build_date(int(match["sy"]) + 1911, int(match["sm"]), int(match["sd"]))
    end_roc_year = int(match["ey"] or match["sy"])
    end = _build_date(end_roc_year + 1911, int(match["em"]), int(match["ed"]))
    if start and end and end < start and not match["ey"]:
        end = _build_date(end_roc_year + 1912, int(match["em"]), int(match["ed"]))
    return start, end


def _single_date(window: str) -> date | None:
    match = re.search(WESTERN_TOKEN, window)
    if match:
        return _from_match(match)
    match = re.search(ROC_TOKEN, window)
    if match:
        return _from_match(match)
    return None


def _explicitly_one_day(window: str) -> bool:
    return bool(
        re.search(r"(?:僅限|只限|限於).{0,50}(?:當日|一天)", window)
        or re.search(r"(?:當日|一天).{0,30}(?:活動|限定|有效)", window)
    )


def parse_date_range(text: str) -> DateRange:
    api_start = re.search(r"(?:startDate|start_date)\s*:\s*([^\n]{0,60})", text, re.I)
    api_end = re.search(r"(?:endDate|end_date)\s*:\s*([^\n]{0,60})", text, re.I)
    if api_start and api_end:
        start = _single_date(api_start.group(1))
        end = _single_date(api_end.group(1))
        if start and end:
            return DateRange(start, end, "high", f"{api_start.group(0)}; {api_end.group(0)}")
    json_start = re.search(r"activity_start_time\s*:\s*([^\n]{0,60})", text, re.I)
    json_end = re.search(r"activity_end_time\s*:\s*([^\n]{0,60})", text, re.I)
    if json_start and json_end:
        start = _single_date(json_start.group(1))
        end = _single_date(json_end.group(1))
        if start and end:
            return DateRange(start, end, "high", f"{json_start.group(0)}; {json_end.group(0)}")
    windows = _candidate_windows(text)
    # Prefer an explicit range near any activity-period label over a lone
    # deadline mentioned earlier. Official pages often put a completion date
    # before the actual campaign range in the same paragraph.
    for confidence, window in windows:
        parsed = _western_range(window) or _roc_range(window)
        if parsed and parsed[0] and parsed[1]:
            return DateRange(parsed[0], parsed[1], confidence, window[:260])
    for confidence, window in windows:
        if confidence == "high":
            single = _single_date(window)
            if single:
                if _explicitly_one_day(window):
                    return DateRange(single, single, "medium", window[:260])
                # A lone date is frequently a publish date, example date, or the
                # start of an open-ended/monthly promotion.  It is not evidence
                # that the campaign ended on that date.
                return DateRange(single, None, "low", window[:260])
    return DateRange(None, None, "none", "")


def lifecycle_for(start: date | None, end: date | None, now: datetime) -> str:
    today = now.date()
    if start and today < start:
        return "upcoming"
    if end and today > end:
        return "ended"
    if start or end:
        return "active"
    return "unknown"


def parse_now(value: str | None, timezone_name: str) -> datetime:
    timezone = ZoneInfo(timezone_name)
    if not value:
        return datetime.now(timezone)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)
