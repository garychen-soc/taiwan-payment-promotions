#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from payment_promotions_monitor.dates import lifecycle_for  # noqa: E402
from payment_promotions_monitor.insights import analyze_activity  # noqa: E402


SECTION_ORDER = (
    "sold_out",
    "upcoming",
    "app_only_unknown",
    "active_public",
    "review_required",
)
QUOTA_STATUSES = {
    "not_marked_full",
    "partial_sold_out",
    "sold_out",
    "confirmed_available",
    "unknown_app_only",
    "unknown_source_failure",
}
PUBLIC_STATUS_SCOPES = {
    "public",
    "partial",
    "app_only",
    "unavailable",
    "unknown",
}
CONDITION_LABELS = {
    "location": "適用地點",
    "content": "活動內容",
    "reminder": "注意事項",
    "restrictions": "限制條件",
}
CONDITION_SECTION_RE = re.compile(
    r"(?:^|\s)(title|description|location|content|reminder|restrictions):\s*",
    re.IGNORECASE,
)
GOOGLE_CALENDAR_URL = "https://calendar.google.com/calendar/render"


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _allowed_url(url: str, domains: list[str]) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _date_value(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _activity_identity(item: dict[str, Any]) -> tuple[str, str]:
    provider_id = str(item.get("provider_id", ""))
    external_id = str(item.get("external_id") or "").strip()
    if external_id:
        return provider_id, f"id:{external_id}"
    return provider_id, f"url:{str(item.get('url', ''))}"


def _public_status_scope(
    provider: dict[str, Any],
    coverage_item: dict[str, Any],
) -> str:
    value = str(
        coverage_item.get("public_status_scope")
        or provider.get("public_status_scope")
        or "unknown"
    )
    return value if value in PUBLIC_STATUS_SCOPES else "unknown"


def _google_calendar_url(activity: dict[str, Any]) -> str:
    start = _date_value(activity.get("start_date"))
    end = _date_value(activity.get("end_date"))
    title = str(activity.get("title", "")).strip()
    if not start or not end or end < start or not title:
        return ""

    provider = str(activity.get("provider_name", "")).strip()
    insights = activity.get("insights", {})
    insight_summary = insights.get("human_summary", "") if isinstance(insights, dict) else ""
    summary = str(
        activity.get("editorial_summary")
        or insight_summary
        or activity.get("conditions_summary")
        or ""
    )
    summary = re.sub(r"\s+", " ", summary).strip()[:700]

    details: list[str] = []
    if provider:
        details.append(f"支付業者：{provider}")
    if summary:
        details.append(summary)

    official_url = str(activity.get("url", "")).strip()
    parsed_official_url = urlsplit(official_url)
    if parsed_official_url.scheme == "https" and parsed_official_url.hostname:
        details.append(f"官方活動頁：{official_url}")
    details.append("優惠名額與條件可能調整，實際內容以官方公告為準。")

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start:%Y%m%d}/{end + timedelta(days=1):%Y%m%d}",
        "details": "\n\n".join(details),
        "ctz": "Asia/Taipei",
    }
    return f"{GOOGLE_CALENDAR_URL}?{urlencode(params)}"


def _conditions_display(title: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    text = re.sub(r"\s+", " ", value).strip()
    matches = list(CONDITION_SECTION_RE.finditer(text))
    if not matches:
        return [text[:1500]]

    values: list[str] = []
    seen: set[str] = set()
    leading = text[: matches[0].start()].strip()
    if leading and leading != title:
        values.append(leading[:500])
        seen.add(leading)

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end() : end].strip()
        if not content or content == title or content in seen:
            continue
        seen.add(content)
        key = match.group(1).lower()
        if key in {"title", "description"}:
            rendered = content
        else:
            rendered = f"{CONDITION_LABELS[key]}：{content}"
        values.append(rendered[:700])
        if len(values) >= 5:
            break
    return values or [text[:1500]]


def _flatten_report(report: dict[str, Any], today: date) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    sections = report.get("sections", {})
    for section in SECTION_ORDER:
        values = sections.get(section, [])
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if item.get("lifecycle") in {"ended", "cancelled"}:
                continue
            key = _activity_identity(item)
            if key in seen:
                continue
            seen.add(key)
            item["conditions_display"] = _conditions_display(
                str(item.get("title", "")), item.get("conditions_summary")
            )
            item["insights"] = analyze_activity(item, today)
            activities.append(item)
    return activities


def _supplemental_activities(
    supplement: dict[str, Any],
    config: dict[str, Any],
    existing: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    providers = {item["id"]: item for item in config.get("providers", []) if isinstance(item, dict) and item.get("id")}
    seen = {_activity_identity(item) for item in existing}
    accepted: list[dict[str, Any]] = []
    for raw in supplement.get("supplemental_activities", []):
        if not isinstance(raw, dict):
            continue
        provider = providers.get(str(raw.get("provider_id", "")))
        if not provider:
            continue
        url = str(raw.get("url", ""))
        if not _allowed_url(url, [str(value).lower() for value in provider.get("official_domains", [])]):
            continue
        key = _activity_identity(
            {
                "provider_id": provider["id"],
                "external_id": raw.get("external_id"),
                "url": url,
            }
        )
        if key in seen or not str(raw.get("title", "")).strip():
            continue
        start = _date_value(raw.get("start_date"))
        end = _date_value(raw.get("end_date"))
        lifecycle = lifecycle_for(start, end, datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo("Asia/Taipei")))
        if lifecycle == "ended":
            continue
        quota_status = str(raw.get("quota_status", "not_marked_full"))
        if quota_status not in QUOTA_STATUSES:
            quota_status = "not_marked_full"
        item = {
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "title": str(raw["title"]).strip(),
            "url": url,
            "source_url": url,
            "external_id": raw.get("external_id"),
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "lifecycle": lifecycle,
            "quota_status": quota_status,
            "quota_evidence_complete": bool(raw.get("evidence")),
            "review_required": not (start and end),
            "date_confidence": "ai_official_source",
            "conditions_summary": str(raw.get("conditions_summary", ""))[:1200],
            "fetched_at": supplement.get("generated_at", ""),
            "content_hash": "",
            "evidence": raw.get("evidence", []) if isinstance(raw.get("evidence"), list) else [],
            "components": raw.get("components", []) if isinstance(raw.get("components"), list) else [],
            "ai_supplemental": True,
        }
        item["conditions_display"] = _conditions_display(item["title"], item["conditions_summary"])
        item["insights"] = analyze_activity(item, today)
        accepted.append(item)
        seen.add(key)
    return accepted


def _highlight(kind: str, activity: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "provider_name": activity.get("provider_name", ""),
        "title": activity.get("title", ""),
        "summary": summary,
        "url": activity.get("url", ""),
    }


def _automatic_highlights(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, values: list[dict[str, Any]], limit: int, summary_key: str = "human_summary") -> None:
        count = 0
        for item in values:
            url = str(item.get("url", ""))
            if not url or url in seen:
                continue
            insight = item.get("insights", {})
            summary = str(insight.get(summary_key) or item.get("conditions_summary") or "請查看官方活動辦法。")
            result.append(_highlight(kind, item, summary[:180]))
            seen.add(url)
            count += 1
            if count >= limit:
                break

    available = [item for item in activities if item.get("quota_status") not in {"sold_out", "partial_sold_out"}]
    high_return = sorted(
        [item for item in available if item.get("insights", {}).get("is_high_return")],
        key=lambda item: (
            float(item.get("insights", {}).get("max_reward_percent") or 0),
            int(item.get("insights", {}).get("fixed_reward_amount") or 0),
        ),
        reverse=True,
    )
    upcoming = sorted(
        [item for item in available if item.get("insights", {}).get("is_upcoming")],
        key=lambda item: int(item.get("insights", {}).get("starts_in_days") or 9999),
    )
    expiring = sorted(
        [item for item in available if item.get("insights", {}).get("is_expiring_soon")],
        key=lambda item: int(item.get("insights", {}).get("ends_in_days") or 9999),
    )
    sold_out = [item for item in activities if item.get("quota_status") in {"sold_out", "partial_sold_out"}]
    add("high_return", high_return, 3)
    add("upcoming", upcoming, 2)
    add("expiring", expiring, 2)
    add("sold_out", sold_out, 2, "human_summary")
    return result[:8]


def _ai_highlights(
    supplement: dict[str, Any],
    config: dict[str, Any],
    activities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    provider_domains = {
        provider["id"]: [str(value).lower() for value in provider.get("official_domains", [])]
        for provider in config.get("providers", [])
        if isinstance(provider, dict) and provider.get("id")
    }
    provider_by_name = {str(item.get("provider_name")): str(item.get("provider_id")) for item in activities}
    result: list[dict[str, Any]] = []
    for raw in supplement.get("highlights", []):
        if not isinstance(raw, dict):
            continue
        provider_id = str(raw.get("provider_id") or provider_by_name.get(str(raw.get("provider_name", "")), ""))
        url = str(raw.get("url", ""))
        if provider_id not in provider_domains or not _allowed_url(url, provider_domains[provider_id]):
            continue
        title = str(raw.get("title", "")).strip()
        summary = str(raw.get("summary", "")).strip()
        if not title or not summary:
            continue
        result.append(
            {
                "kind": str(raw.get("kind", "ai_pick")),
                "provider_name": str(raw.get("provider_name", "")),
                "title": title[:240],
                "summary": summary[:300],
                "url": url,
                "ai_reviewed": True,
            }
        )
    return result[:8]


def build(report_path: Path, output_dir: Path, supplement_path: Path) -> Path:
    report = _load_json(report_path)
    if not isinstance(report, dict):
        raise ValueError(f"Invalid report: {report_path}")
    config = _load_json(ROOT / "config" / "sources.json", {})
    supplement = _load_json(supplement_path, {})
    if not isinstance(config, dict) or not isinstance(supplement, dict):
        raise ValueError("Invalid configuration or AI supplement")
    timezone = ZoneInfo(str(report.get("timezone", "Asia/Taipei")))
    generated_at = datetime.fromisoformat(str(report["generated_at"])).astimezone(timezone)
    today = generated_at.date()
    activities = _flatten_report(report, today)
    supplemental_activities = _supplemental_activities(supplement, config, activities, today)
    activities.extend(supplemental_activities)
    ai_highlights = _ai_highlights(supplement, config, activities)
    highlights = ai_highlights or _automatic_highlights(activities)
    highlights_by_url = {
        str(item.get("url")): item
        for item in highlights
        if isinstance(item, dict) and item.get("url")
    }
    for activity in activities:
        highlight = highlights_by_url.get(str(activity.get("url", "")))
        if highlight:
            activity["is_featured"] = True
            activity["highlight_kind"] = str(highlight.get("kind", "ai_pick"))
            activity["editorial_summary"] = str(highlight.get("summary", ""))[:300]
        calendar_url = _google_calendar_url(activity)
        if calendar_url:
            activity["google_calendar_url"] = calendar_url
    coverage = report.get("run", {}).get("coverage", {})
    failures = report.get("source_failures", [])
    gaps = report.get("coverage_gaps", [])
    registered_sources = coverage.get("registered_sources")
    extended_checks = coverage.get("extended_checks")
    has_grouped_health = isinstance(registered_sources, dict) and isinstance(extended_checks, dict)
    if has_grouped_health:
        official_failed = int(registered_sources.get("failed", 0) or 0)
        extended_failed = int(extended_checks.get("failed", 0) or 0)
        official_expected = int(registered_sources.get("expected", 0) or 0)
        official_succeeded = int(registered_sources.get("succeeded", 0) or 0)
        health_status = (
            "unavailable"
            if official_expected > 0 and official_succeeded == 0
            else (
                "normal"
                if official_failed == 0 and extended_failed == 0 and not gaps
                else "partial"
            )
        )
    else:
        registered_sources = {}
        extended_checks = {}
        legacy_transport_status = str(coverage.get("transport_status", ""))
        health_status = (
            "unavailable"
            if legacy_transport_status == "unavailable"
            else ("normal" if not failures else "partial")
        )
    review_label = "官網列表皆可直接解析"
    if gaps and supplemental_activities:
        review_label = (
            f"{len(gaps)} 個官網列表待補強；AI 已補入 "
            f"{len(supplemental_activities)} 筆官方網域活動"
        )
    elif gaps:
        review_label = f"{len(gaps)} 個官網列表待補強"
    source_health = {
        "status": health_status,
        "label": {
            "normal": "資料更新正常",
            "partial": "部分官網檢查未完成",
            "unavailable": "官方入口暫時無法讀取",
        }[health_status],
        # Legacy request-level counters remain available to old clients.
        "succeeded": coverage.get("succeeded", 0),
        "expected": coverage.get("expected", 0),
        "official_sources": registered_sources,
        "extended_checks": extended_checks,
        "failures": failures,
        "needs_ai_review": len(gaps),
        "review_label": review_label,
        "coverage_gaps": gaps,
    }
    activity_counts: dict[str, int] = {}
    for item in activities:
        provider_id = str(item.get("provider_id", ""))
        if provider_id:
            activity_counts[provider_id] = activity_counts.get(provider_id, 0) + 1
    report_coverage = report.get("coverage_by_provider", {})
    provider_coverage: list[dict[str, Any]] = []
    for provider in config.get("providers", []):
        if not isinstance(provider, dict) or not provider.get("id"):
            continue
        provider_id = str(provider["id"])
        coverage_item = report_coverage.get(provider_id, {})
        discovery_status = str(coverage_item.get("discovery_status", "limited"))
        coverage_note = str(
            coverage_item.get("public_status_coverage")
            or provider.get("public_status_coverage", "未提供公開狀態說明")
        )
        public_status_state = _public_status_scope(provider, coverage_item)
        official_sources = coverage_item.get("registered_sources", {})
        extended_checks = coverage_item.get("extended_checks", {})
        provider_coverage.append(
            {
                "provider_id": provider_id,
                "provider_name": str(provider.get("name", provider_id)),
                "activity_count": activity_counts.get(provider_id, 0),
                "discovery_status": discovery_status,
                "discovery_label": "完整" if discovery_status == "complete" else "部分涵蓋",
                "succeeded": int(coverage_item.get("succeeded", 0) or 0),
                "expected": int(coverage_item.get("expected", 0) or 0),
                "official_sources": official_sources if isinstance(official_sources, dict) else {},
                "extended_checks": extended_checks if isinstance(extended_checks, dict) else {},
                "public_status_coverage": public_status_state,
                "coverage_note": coverage_note,
            }
        )
    providers = sorted(
        (item["provider_name"] for item in provider_coverage),
        key=str.casefold,
    )
    payload = {
        "schema_version": 1,
        "generated_at": report["generated_at"],
        "build_at": datetime.now(timezone).isoformat(),
        "timezone": str(report.get("timezone", "Asia/Taipei")),
        "headline": str(supplement.get("headline") or "今天值得留意的支付優惠"),
        "summary": report.get("summary", {}),
        "source_health": source_health,
        "providers": providers,
        "provider_coverage": provider_coverage,
        "highlights": highlights,
        "activities": activities,
        "analysis_method": "local_rules_and_codex_review" if ai_highlights else "local_rules",
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    data_dir = output_dir / "data"
    assets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    asset_digest = hashlib.sha256()
    for asset in sorted((ROOT / "web" / "assets").iterdir(), key=lambda item: item.name):
        if asset.is_file():
            shutil.copy2(asset, assets_dir / asset.name)
            asset_digest.update(asset.name.encode("utf-8"))
            asset_digest.update(asset.read_bytes())
    asset_version = asset_digest.hexdigest()[:12]
    index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    (output_dir / "index.html").write_text(
        index_html.replace("__ASSET_VERSION__", asset_version), encoding="utf-8"
    )
    manifest = ROOT / "web" / "manifest.webmanifest"
    if manifest.exists():
        shutil.copy2(manifest, output_dir / manifest.name)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    data_path = data_dir / "promotions.json"
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the mobile GitHub Pages promotion dashboard")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "latest.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs")
    parser.add_argument("--ai-supplement", type=Path, default=ROOT / "data" / "ai_supplement.json")
    args = parser.parse_args()
    output = build(args.report, args.output_dir, args.ai_supplement)
    print(json.dumps({"status": "complete", "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
