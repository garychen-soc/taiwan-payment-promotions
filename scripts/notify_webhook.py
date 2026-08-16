#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "reports" / "latest.json"
SITE_URL = "https://garychen-soc.github.io/taiwan-payment-promotions/"
WEBHOOK_ENV = "PAYMENT_PROMOTIONS_WEBHOOK_URL"
SECTION_ORDER = (
    "active_public",
    "upcoming",
    "sold_out",
    "app_only_unknown",
    "review_required",
)


def _load_report(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("report root must be an object")
    return value


def _activity_key(activity: dict[str, Any]) -> tuple[str, str]:
    provider = str(activity.get("provider_id") or activity.get("provider_name") or "unknown")
    identity = str(activity.get("external_id") or activity.get("url") or activity.get("title") or "")
    return provider, identity


def provider_counts(report: dict[str, Any]) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    sections = report.get("sections", {})
    if not isinstance(sections, dict):
        return []
    for section in SECTION_ORDER:
        activities = sections.get(section, [])
        if not isinstance(activities, list):
            continue
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            key = _activity_key(activity)
            if key in seen:
                continue
            seen.add(key)
            name = str(activity.get("provider_name") or activity.get("provider_id") or "未知業者")
            counts[name] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))


def _cache_summary(report: dict[str, Any]) -> str:
    cache = report.get("cache")
    if not isinstance(cache, dict):
        return "報表未提供重抓／沿用統計"

    refetched = cache.get("refetched")
    if refetched is None:
        refetched = cache.get("detail_requests")
    reused = cache.get("reused")
    if reused is None:
        reused = cache.get("records_reused")
    if refetched is None and reused is None:
        return "報表未提供重抓／沿用統計"
    return f"重抓 {int(refetched or 0)}、沿用 {int(reused or 0)}"


def infer_status(report: dict[str, Any]) -> str:
    coverage = report.get("run", {}).get("coverage", {})
    if not isinstance(coverage, dict):
        return "unknown"
    if coverage.get("transport_status") == "unavailable" or coverage.get("systemic_dns_failure"):
        return "failed"
    if int(coverage.get("failed", 0) or 0) > 0 or report.get("coverage_gaps"):
        return "partial"
    return "success"


def build_message(
    report: dict[str, Any],
    *,
    status: str | None = None,
    stage: str = "報表已產生",
    error: str = "",
    commit: str = "",
) -> str:
    status = status or infer_status(report)
    labels = {"success": "成功", "partial": "部分成功", "failed": "失敗", "unknown": "未知"}
    summary = report.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    gaps = report.get("coverage_gaps", [])
    gap_count = len(gaps) if isinstance(gaps, list) else 0
    changes = report.get("changes", [])
    change_count = len(changes) if isinstance(changes, list) else 0
    counts = provider_counts(report)
    providers = "、".join(f"{name} {count}" for name, count in counts) or "無可用資料"
    lines = [
        f"台灣支付優惠雷達｜{labels.get(status, status)}",
        f"進度：{stage}",
        f"活動：{int(summary.get('included_non_expired', 0) or 0)} 筆；額滿／部分額滿 {int(summary.get('sold_out_or_partial', 0) or 0)} 筆",
        f"各服務：{providers}",
        f"擷取：{_cache_summary(report)}；本輪變更 {change_count} 筆",
        f"Coverage gaps：{gap_count}",
        f"Commit：{commit or '未提供'}",
        f"網站：{SITE_URL}",
    ]
    if error:
        compact_error = " ".join(str(error).split())[:500]
        lines.append(f"錯誤：{compact_error}")
    return "\n".join(lines)


def current_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def post_webhook(url: str, message: str, timeout: int = 15) -> None:
    payload = json.dumps({"text": message}, ensure_ascii=False)
    result = subprocess.run(
        [
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--max-time",
            str(timeout),
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            payload,
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "webhook request failed").split())
        raise RuntimeError(detail[:500])


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a concise monitor result to a webhook")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--status", choices=("success", "partial", "failed", "unknown"))
    parser.add_argument("--stage", default="報表已產生")
    parser.add_argument("--error", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    report: dict[str, Any] = {}
    load_error = ""
    try:
        report = _load_report(args.report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        load_error = f"無法讀取 {args.report.name}: {exc}"

    status = args.status or ("failed" if load_error else None)
    error = args.error or load_error
    message = build_message(
        report,
        status=status,
        stage=args.stage,
        error=error,
        commit=args.commit or current_commit(),
    )
    if args.dry_run:
        print(message)
        return 0

    webhook_url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not webhook_url:
        print(f"{WEBHOOK_ENV} is not configured", file=sys.stderr)
        return 2
    try:
        post_webhook(webhook_url, message)
    except RuntimeError as exc:
        print(f"Webhook delivery failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "sent"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
