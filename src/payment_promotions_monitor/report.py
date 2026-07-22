from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .dates import lifecycle_for
from .models import Activity, RunResult


STATUS_LABELS = {
    "not_marked_full": "公開官網未見額滿公告",
    "partial_sold_out": "部分期別／子活動已額滿",
    "sold_out": "已額滿",
    "confirmed_available": "官網明確公告仍有名額",
    "unknown_app_only": "僅 App 可查，公開網頁無法確認",
    "unknown_source_failure": "來源失敗，狀態未知",
}
LIFECYCLE_LABELS = {
    "active": "進行中",
    "upcoming": "即將開始",
    "ended": "已結束",
    "cancelled": "已取消",
    "unknown": "日期待確認",
}


def _refresh_lifecycle(activity: Activity, now: datetime) -> None:
    try:
        start = date.fromisoformat(activity.start_date) if activity.start_date else None
        end = date.fromisoformat(activity.end_date) if activity.end_date else None
    except ValueError:
        start = end = None
    activity.lifecycle = lifecycle_for(start, end, now)


def _section(activity: Activity) -> str:
    if activity.review_required or activity.lifecycle == "unknown" or activity.quota_status == "unknown_source_failure":
        return "review_required"
    if activity.quota_status == "unknown_app_only":
        return "app_only_unknown"
    if activity.quota_status in {"partial_sold_out", "sold_out"}:
        return "sold_out"
    if activity.lifecycle == "upcoming":
        return "upcoming"
    return "active_public"


def build_payload(
    run: RunResult,
    activities: list[Activity],
    now: datetime,
    config: dict[str, Any],
    changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    included: list[Activity] = []
    expired_count = 0
    for activity in activities:
        _refresh_lifecycle(activity, now)
        if activity.lifecycle in {"ended", "cancelled"}:
            expired_count += 1
            continue
        included.append(activity)
        sections[_section(activity)].append(activity.to_dict())
    for values in sections.values():
        values.sort(key=lambda item: (item.get("start_date") or "9999-12-31", item["provider_name"], item["title"]))

    failed = [item.to_dict() for item in run.attempts if not item.ok]
    coverage_gaps = [
        {
            "provider_id": item.provider_id,
            "provider_name": item.provider_name,
            "source_name": item.source_name,
            "url": item.url,
            "issue": item.coverage_issue,
            "discovered_count": item.discovered_count,
        }
        for item in run.attempts
        if item.coverage_issue
    ]
    coverage_gaps.extend(run.crawl_limit_pending)
    by_provider: dict[str, dict[str, Any]] = {}
    for provider in config["providers"]:
        attempts = [item for item in run.attempts if item.provider_id == provider["id"]]
        success = sum(1 for item in attempts if item.ok)
        provider_gaps = [item.coverage_issue for item in attempts if item.coverage_issue]
        provider_gaps.extend(
            item["issue"] for item in run.crawl_limit_pending if item["provider_id"] == provider["id"]
        )
        discovery_roles = {"activity_listing", "mixed_listing", "taiwanpay_api_listing"}
        if any(not item.ok and item.role in discovery_roles for item in attempts):
            provider_gaps.append("source_fetch_failed")
        activity_sources = [
            source for source in provider.get("sources", []) if source.get("role") in discovery_roles
        ]
        has_discovery_source = bool(activity_sources)
        has_complete_discovery_source = any(
            source.get("coverage_scope", "complete") == "complete" for source in activity_sources
        )
        if run.mode == "full" and not has_discovery_source:
            provider_gaps.append("no_verified_activity_listing")
            coverage_gaps.append(
                {
                    "provider_id": provider["id"],
                    "provider_name": provider["name"],
                    "source_name": "業者活動發現來源",
                    "url": provider.get("seeds", [{}])[0].get("url", "") if provider.get("seeds") else "",
                    "issue": "no_verified_activity_listing",
                    "discovered_count": 0,
                }
            )
        elif run.mode == "full" and not has_complete_discovery_source:
            provider_gaps.append("partial_public_activity_discovery")
            coverage_gaps.append(
                {
                    "provider_id": provider["id"],
                    "provider_name": provider["name"],
                    "source_name": "官方主題活動總覽與關聯圖",
                    "url": activity_sources[0].get("display_url", activity_sources[0].get("url", "")),
                    "issue": "partial_public_activity_discovery",
                    "discovered_count": sum(item.discovered_count for item in attempts if item.role in discovery_roles),
                }
            )
        by_provider[provider["id"]] = {
            "name": provider["name"],
            "expected": len(attempts),
            "succeeded": success,
            "failed": len(attempts) - success,
            "discovery_status": "limited" if provider_gaps else "complete",
            "discovery_gaps": provider_gaps,
            "public_status_coverage": provider.get("public_status_coverage", "unknown"),
        }
    coverage = dict(run.coverage)
    coverage["discovery_issues"] = len(coverage_gaps)
    if coverage_gaps and coverage["status"] == "complete":
        coverage["status"] = "partial"
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "timezone": config.get("timezone", "Asia/Taipei"),
        "run": {
            "run_id": run.run_id,
            "mode": run.mode,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "coverage": coverage,
        },
        "summary": {
            "included_non_expired": len(included),
            "expired_excluded": expired_count,
            "active_public": len(sections["active_public"]),
            "sold_out_or_partial": len(sections["sold_out"]),
            "upcoming": len(sections["upcoming"]),
            "app_only_unknown": len(sections["app_only_unknown"]),
            "review_required": len(sections["review_required"]),
        },
        "sections": dict(sections),
        "coverage_by_provider": by_provider,
        "changes": changes or [],
        "source_failures": failed,
        "coverage_gaps": coverage_gaps,
    }


def _period(item: dict[str, Any]) -> str:
    start = item.get("start_date") or "?"
    end = item.get("end_date") or "?"
    return start if start == end else f"{start} ～ {end}"


def _activity_markdown(item: dict[str, Any]) -> list[str]:
    status = STATUS_LABELS.get(item["quota_status"], item["quota_status"])
    lines = [
        f"- [{item['provider_name']}] [{item['title']}]({item['url']})",
        f"  - 期間：{_period(item)}（{LIFECYCLE_LABELS.get(item['lifecycle'], item['lifecycle'])}）",
        f"  - 額度狀態：{status}",
    ]
    if item.get("conditions_summary"):
        lines.append(f"  - 重點：{item['conditions_summary']}")
    for component in item.get("components", []):
        label = " / ".join(value for value in (component.get("component"), component.get("period")) if value)
        lines.append(f"  - 已額滿項目：{label or '子項目'}")
    if item.get("evidence"):
        evidence = item["evidence"][-1]
        lines.append(f"  - 證據：[{evidence['kind']}]({evidence['source_url']}) — {evidence['excerpt'][:240]}")
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    generated = payload["generated_at"]
    coverage = payload["run"]["coverage"]
    summary = payload["summary"]
    lines = [
        "# 台灣行動／電子支付優惠監測",
        "",
        f"更新時間：{generated}",
        f"本輪網址擷取：{coverage['succeeded']}/{coverage['expected']}（{coverage['rate']:.0%}，{coverage.get('transport_status', coverage['status'])}）",
        f"活動發現覆蓋：{coverage['status']}（{coverage.get('discovery_issues', 0)} 個待補強缺口）",
        f"未過期活動：{summary['included_non_expired']}；已排除過期：{summary['expired_excluded']}",
        "",
        "> 「公開官網未見額滿公告」只代表本輪沒有找到明確額滿證據，不保證仍有名額。",
        "",
    ]
    lines.extend(["## 本輪狀態變化", ""])
    if not payload.get("changes"):
        lines.extend(["- 沒有偵測到既有活動的額滿狀態變化。", ""])
    else:
        for change in payload["changes"]:
            old_label = STATUS_LABELS.get(change["from"], change["from"])
            new_label = STATUS_LABELS.get(change["to"], change["to"])
            lines.append(
                f"- [{change['provider_name']}] [{change['title']}]({change['url']})：{old_label} → {new_label}"
            )
        lines.append("")
    headings = (
        ("sold_out", "已額滿或部分額滿（活動仍未過期）"),
        ("active_public", "進行中：公開官網未見額滿公告"),
        ("upcoming", "即將開始"),
        ("app_only_unknown", "僅 App 可確認額滿狀態"),
        ("review_required", "需要 AI／人工複核"),
    )
    sections = payload["sections"]
    for key, heading in headings:
        lines.extend([f"## {heading}", ""])
        items = sections.get(key, [])
        if not items:
            lines.extend(["- 無", ""])
            continue
        for item in items:
            lines.extend(_activity_markdown(item))
        lines.append("")

    lines.extend(["## 來源失敗與覆蓋缺口", ""])
    failures = payload["source_failures"]
    if not failures:
        lines.append("- 本輪沒有來源擷取失敗。")
    else:
        for item in failures:
            lines.append(f"- {item['provider_name']}／{item['source_name']}：{item['error']} — {item['url']}")
    gaps = payload.get("coverage_gaps", [])
    issue_labels = {
        "listing_zero_discovery": "列表可讀取但未發現任何詳情連結",
        "listing_reached_max_links": "發現數達擷取上限，可能仍有未遍歷項目",
        "no_verified_activity_listing": "尚無已驗證的公開活動列表，只能由已知活動與 AI 官方網域搜尋補充",
        "partial_public_activity_discovery": "已接入官方主題總覽與活動關聯圖，但官方沒有公開的全站完整活動清單",
        "crawl_reached_max_total_pages": "本輪已達全域擷取上限，仍有已發現的官方頁面尚未讀取",
    }
    if gaps:
        lines.extend(["", "### 活動發現覆蓋缺口", ""])
        for item in gaps:
            label = issue_labels.get(item["issue"], item["issue"])
            suffix = f" — {item['url']}" if item.get("url") else ""
            lines.append(f"- {item['provider_name']}／{item['source_name']}：{label}{suffix}")
    lines.extend(["", "## 業者公開狀態覆蓋", ""])
    for value in payload["coverage_by_provider"].values():
        lines.append(
            f"- {value['name']}：本輪 {value['succeeded']}/{value['expected']}；活動發現 {value['discovery_status']}；{value['public_status_coverage']}"
        )
    lines.append("")
    return "\n".join(lines)


def write_reports(payload: dict[str, Any], output_dir: Path, mode: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(payload["generated_at"]).strftime("%Y%m%d-%H%M%S")
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    markdown = render_markdown(payload)
    json_path = output_dir / f"{stamp}-{mode}.json"
    markdown_path = output_dir / f"{stamp}-{mode}.md"
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    (output_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path
