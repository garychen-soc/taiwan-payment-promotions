from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from .adapters import Document, external_id_from_url, parse_document, summarize_conditions
from .dates import DateRange, lifecycle_for, parse_date_range
from .fetch import FetchResult, canonical_url, fetch_url, is_allowed_url, post_json_url
from .models import Activity, Evidence, RunResult, SourceAttempt
from .status import analyze_quota, match_announcement


@dataclass(slots=True)
class Task:
    provider: dict[str, Any]
    source_name: str
    role: str
    fetch_url: str
    display_url: str
    title_hint: str = ""
    patterns: list[str] | None = None
    max_links: int = 12
    adapter: str = ""
    external_id: str = ""
    start_date: str = ""
    end_date: str = ""


@dataclass(slots=True)
class CollectedDocument:
    task: Task
    document: Document
    fetched_at: str


class Crawler:
    def __init__(self, config: dict[str, Any], now: datetime, *, timeout: float = 20.0) -> None:
        self.config = config
        self.now = now
        self.timeout = timeout
        self.attempts: list[SourceAttempt] = []
        self.activity_documents: list[CollectedDocument] = []
        self.announcement_documents: list[CollectedDocument] = []
        self._seen_fetches: set[tuple[str, str]] = set()

    def _initial_tasks(self, mode: str, recheck_targets: list[dict[str, str]]) -> list[Task]:
        tasks: list[Task] = []
        providers = {item["id"]: item for item in self.config["providers"]}
        for provider in self.config["providers"]:
            for source in provider.get("sources", []):
                role = source["role"]
                if mode == "status" and role not in {
                    "announcement_listing",
                    "announcement_detail",
                    "mixed_listing",
                    "mixed_detail",
                    "status_page",
                    "taiwanpay_api_listing",
                }:
                    continue
                tasks.append(
                    Task(
                        provider=provider,
                        source_name=source["name"],
                        role=role,
                        fetch_url=source["url"],
                        display_url=source.get("display_url", source["url"]),
                        patterns=source.get("link_patterns"),
                        max_links=int(source.get("max_links", 12)),
                        adapter=source.get("adapter", ""),
                    )
                )
            if mode == "full":
                for seed in provider.get("seeds", []):
                    tasks.append(
                        Task(
                            provider=provider,
                            source_name=seed.get("name", "seed"),
                            role="activity_detail",
                            fetch_url=seed.get("fetch_url", seed["url"]),
                            display_url=seed["url"],
                            title_hint=seed.get("title", ""),
                            adapter=seed.get("adapter", ""),
                        external_id=seed.get("external_id", ""),
                        start_date=seed.get("start_date", ""),
                        end_date=seed.get("end_date", ""),
                        )
                    )
        if mode == "status":
            for target in recheck_targets:
                provider = providers.get(target["provider_id"])
                if not provider:
                    continue
                tasks.append(
                    Task(
                        provider=provider,
                        source_name="既有活動複查",
                        role="activity_detail",
                        fetch_url=target.get("source_url") or target["url"],
                        display_url=target["url"],
                        start_date=target.get("start_date") or "",
                        end_date=target.get("end_date") or "",
                        adapter="taiwanpay_detail" if target["provider_id"] == "taiwanpay" else "",
                        external_id=(target.get("external_id") or "") if target["provider_id"] == "taiwanpay" else "",
                    )
                )
        return tasks

    def _attempt(self, task: Task) -> CollectedDocument | None:
        identity_url = canonical_url(task.fetch_url)
        if task.external_id:
            identity_url += f"#external_id={task.external_id}"
        key = (task.provider["id"], identity_url)
        if key in self._seen_fetches:
            return None
        self._seen_fetches.add(key)
        fetched_at = self.now.isoformat()
        try:
            if task.adapter in {"taiwanpay_listing", "taiwanpay_detail"}:
                tx_id = "TF020109" if task.adapter == "taiwanpay_listing" else "TF020110"
                body = (
                    {"status": "0", "paymentType": "10"}
                    if task.adapter == "taiwanpay_listing"
                    else {"systemSeq": task.external_id}
                )
                result = None
                api_error = "unknown response"
                for _ in range(2):
                    payload = {
                        "header": {
                            "ctxSn": uuid.uuid4().hex,
                            "txDate": datetime.now(self.now.tzinfo).strftime("%Y%m%dT%H%M%S%z"),
                            "txId": tx_id,
                        },
                        "body": body,
                    }
                    candidate = post_json_url(
                        task.fetch_url,
                        payload,
                        task.provider["official_domains"],
                        timeout=self.timeout,
                    )
                    parsed_response = json.loads(candidate.text)
                    return_code = parsed_response.get("header", {}).get("returnCode")
                    if return_code != "TF0000":
                        api_error = f"returnCode={return_code}"
                        continue
                    if task.adapter == "taiwanpay_detail":
                        detail = parsed_response.get("body", {}).get("campaignDetail", {})
                        returned_id = detail.get("systemSeq")
                        if returned_id != task.external_id:
                            api_error = f"requested {task.external_id}, received {returned_id}"
                            continue
                        returned_title = str(detail.get("title", ""))
                        if task.title_hint and returned_title and not self._titles_compatible(task.title_hint, returned_title):
                            fallback = {
                                "header": parsed_response.get("header", {}),
                                "body": {
                                    "campaignDetail": {
                                        "systemSeq": task.external_id,
                                        "title": task.title_hint,
                                        "description": task.title_hint,
                                        "startDate": task.start_date,
                                        "endDate": task.end_date,
                                        "monitor_review_required": "true",
                                        "monitor_note": "官方詳情與官方列表標題不一致；本輪採列表名稱與日期，未採詳情額滿文字。",
                                    }
                                },
                            }
                            fallback_body = json.dumps(fallback, ensure_ascii=False).encode("utf-8")
                            candidate = FetchResult(
                                requested_url=candidate.requested_url,
                                final_url=candidate.final_url,
                                status_code=candidate.status_code,
                                body=fallback_body,
                                text=fallback_body.decode("utf-8"),
                                content_type="application/json",
                                content_hash=hashlib.sha256(fallback_body).hexdigest(),
                            )
                    result = candidate
                    break
                if result is None:
                    raise RuntimeError(f"Taiwan Pay API validation failed: {api_error}")
            else:
                result = fetch_url(
                    task.fetch_url,
                    task.provider["official_domains"],
                    timeout=self.timeout,
                    attempts=2,
                )
            document = parse_document(result)
            self.attempts.append(
                SourceAttempt(
                    provider_id=task.provider["id"],
                    provider_name=task.provider["name"],
                    source_name=task.source_name,
                    role=task.role,
                    url=task.fetch_url,
                    ok=True,
                    fetched_at=fetched_at,
                    status_code=result.status_code,
                    final_url=result.final_url,
                )
            )
            return CollectedDocument(task=task, document=document, fetched_at=fetched_at)
        except Exception as exc:  # individual sources must not abort the run
            self.attempts.append(
                SourceAttempt(
                    provider_id=task.provider["id"],
                    provider_name=task.provider["name"],
                    source_name=task.source_name,
                    role=task.role,
                    url=task.fetch_url,
                    ok=False,
                    fetched_at=fetched_at,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                )
            )
            return None

    @staticmethod
    def _titles_compatible(list_title: str, detail_title: str) -> bool:
        def normalize(value: str) -> str:
            value = re.sub(r"[（(【\[]\s*活動額滿\s*[）)】\]]", "", value)
            return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())

        left = normalize(list_title)
        right = normalize(detail_title)
        if not left or not right:
            return True
        shorter, longer = sorted((left, right), key=len)
        return shorter in longer or SequenceMatcher(None, left, right).ratio() >= 0.55

    def _discovered_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        if task.role == "taiwanpay_api_listing":
            data = collected.document.raw_json if isinstance(collected.document.raw_json, dict) else {}
            body = data.get("body", {}) if isinstance(data, dict) else {}
            campaigns: list[dict[str, Any]] = []
            if isinstance(body, dict):
                for key in ("recommendedCampaigns", "normalCampaigns"):
                    values = body.get(key, [])
                    if isinstance(values, list):
                        campaigns.extend(item for item in values if isinstance(item, dict))
            discovered: list[Task] = []
            seen_ids: set[str] = set()
            for campaign in campaigns:
                external_id = str(campaign.get("systemSeq", ""))
                if not external_id or external_id in seen_ids:
                    continue
                end_value = str(campaign.get("endDate", ""))
                try:
                    if end_value and date.fromisoformat(end_value) < self.now.date():
                        continue
                except ValueError:
                    pass
                seen_ids.add(external_id)
                display_url = f"https://www.taiwanpay.com.tw/fisc-tpay/news/event/{external_id}"
                discovered.append(
                    Task(
                        provider=task.provider,
                        source_name=f"台灣 Pay 活動 API → {str(campaign.get('title', external_id))[:80]}",
                        role="activity_detail",
                        fetch_url="https://www.taiwanpay.com.tw/tpay/v1.0.0/950/taiwanpayfapi/TF02/TF020110",
                        display_url=display_url,
                        title_hint=str(campaign.get("title", "")),
                        adapter="taiwanpay_detail",
                        external_id=external_id,
                        start_date=str(campaign.get("startDate", "")),
                        end_date=str(campaign.get("endDate", "")),
                    )
                )
                if len(discovered) >= task.max_links:
                    break
            for attempt in reversed(self.attempts):
                if attempt.provider_id == task.provider["id"] and attempt.source_name == task.source_name:
                    attempt.discovered_count = len(discovered)
                    if not discovered:
                        attempt.coverage_issue = "listing_zero_discovery"
                    elif len(discovered) >= task.max_links:
                        attempt.coverage_issue = "listing_reached_max_links"
                    break
            return discovered
        if task.role not in {"activity_listing", "announcement_listing", "mixed_listing"}:
            return []
        patterns = [re.compile(value, re.I) for value in (task.patterns or [])]
        if not patterns:
            return []
        discovered: list[Task] = []
        seen: set[str] = set()
        next_role = {
            "activity_listing": "activity_detail",
            "announcement_listing": "announcement_detail",
            "mixed_listing": "mixed_detail",
        }[task.role]
        for url, anchor_text in collected.document.links:
            if not is_allowed_url(url, task.provider["official_domains"]):
                continue
            haystack = f"{url}\n{anchor_text}"
            if not any(pattern.search(haystack) for pattern in patterns):
                continue
            normalized = canonical_url(url)
            if normalized in seen or normalized == canonical_url(task.fetch_url):
                continue
            seen.add(normalized)
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → 詳情",
                    role=next_role,
                    fetch_url=url,
                    display_url=url,
                    title_hint=anchor_text,
                )
            )
            if len(discovered) >= task.max_links:
                break
        for attempt in reversed(self.attempts):
            if attempt.provider_id == task.provider["id"] and canonical_url(attempt.url) == canonical_url(task.fetch_url):
                attempt.discovered_count = len(discovered)
                if not discovered:
                    attempt.coverage_issue = "listing_zero_discovery"
                elif len(discovered) >= task.max_links:
                    attempt.coverage_issue = "listing_reached_max_links"
                break
        return discovered

    def collect(self, mode: str, recheck_targets: list[dict[str, str]] | None = None) -> RunResult:
        started_at = self.now.isoformat()
        queue = self._initial_tasks(mode, recheck_targets or [])
        max_pages = int(self.config.get("max_total_pages", 140))
        cursor = 0
        while cursor < len(queue) and len(self._seen_fetches) < max_pages:
            task = queue[cursor]
            cursor += 1
            collected = self._attempt(task)
            if not collected:
                continue
            if task.role in {"activity_detail", "status_page", "mixed_detail"}:
                self.activity_documents.append(collected)
            if task.role in {"announcement_detail", "mixed_detail"}:
                self.announcement_documents.append(collected)
            discovered = self._discovered_tasks(collected)
            if discovered:
                queue[cursor:cursor] = discovered
            delay = float(self.config.get("request_delay_seconds", 0.0))
            if delay:
                time.sleep(delay)

        activities = [self._activity_from_document(item) for item in self.activity_documents]
        activities = self._deduplicate(activities)
        self._apply_announcements(activities)
        finished_at = datetime.now(self.now.tzinfo).isoformat()
        return RunResult(
            run_id=str(uuid.uuid4()),
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            activities=activities,
            attempts=self.attempts,
        )

    def _activity_from_document(self, collected: CollectedDocument) -> Activity:
        task = collected.task
        document = collected.document
        date_range = parse_date_range(document.text)
        title_date_range = parse_date_range(f"活動期間：{task.title_hint or document.title}")
        if date_range.confidence in {"none", "low"} and title_date_range.start and title_date_range.end:
            date_range = DateRange(title_date_range.start, title_date_range.end, "high", title_date_range.excerpt)
        if (not date_range.start or not date_range.end) and task.start_date and task.end_date:
            try:
                date_range = DateRange(date.fromisoformat(task.start_date), date.fromisoformat(task.end_date), "high", "official listing API")
            except ValueError:
                pass
        lifecycle = lifecycle_for(date_range.start, date_range.end, self.now)
        quota = analyze_quota(document.text, event_start=date_range.start)
        title = document.title
        hint = re.sub(r"\s+", " ", task.title_hint).strip()
        generic_titles = {
            "活動訊息",
            "最新消息",
            "優惠活動",
            "台灣pay",
            "橘子支付",
            "line pay money",
        }
        title_is_generic = (
            title == "未辨識活動名稱"
            or len(title) < 4
            or title.strip().lower() in generic_titles
            or title.startswith("悠遊付｜一卡一付")
            or title.startswith("活動訊息")
            or "{{" in title
        )
        if hint and 4 <= len(hint) <= 180 and title_is_generic:
            title = hint
            title_is_generic = False
        if title_is_generic:
            excluded = generic_titles | {"首頁", "回上一頁", "活動內容", "活動訊息"}
            content_title = next(
                (
                    line.strip()
                    for line in document.text.splitlines()[:50]
                    if 6 <= len(line.strip()) <= 180
                    and line.strip().lower() not in excluded
                    and not line.strip().startswith(("首頁", "Menu", ":::", "活動訊息 -"))
                ),
                "",
            )
            if content_title:
                title = content_title
        evidence: list[Evidence] = []
        if quota.evidence_excerpt:
            evidence.append(
                Evidence(
                    source_url=task.display_url,
                    excerpt=quota.evidence_excerpt,
                    observed_at=collected.fetched_at,
                    kind="activity_page",
                    content_hash=document.content_hash,
                )
            )
        review_required = (
            date_range.confidence in {"none", "low"}
            or lifecycle == "unknown"
            or "monitor_review_required: true" in document.text
        )
        return Activity(
            provider_id=task.provider["id"],
            provider_name=task.provider["name"],
            title=title,
            url=task.display_url,
            source_url=document.url,
            external_id=task.external_id or external_id_from_url(task.display_url) or external_id_from_url(document.url),
            start_date=date_range.start.isoformat() if date_range.start else None,
            end_date=date_range.end.isoformat() if date_range.end else None,
            lifecycle=lifecycle,
            quota_status=quota.status,
            quota_evidence_complete=quota.evidence_complete,
            review_required=review_required,
            date_confidence=date_range.confidence,
            conditions_summary=summarize_conditions(document.text),
            fetched_at=collected.fetched_at,
            content_hash=document.content_hash,
            evidence=evidence,
            components=quota.components,
        )

    @staticmethod
    def _deduplicate(activities: list[Activity]) -> list[Activity]:
        selected: dict[tuple[str, str], Activity] = {}
        for item in activities:
            key = (item.provider_id, f"id:{item.external_id}" if item.external_id else canonical_url(item.url))
            current = selected.get(key)
            if current is None or (current.review_required and not item.review_required):
                selected[key] = item
        return list(selected.values())

    def _apply_announcements(self, activities: list[Activity]) -> None:
        activity_docs = {
            (item.task.provider["id"], canonical_url(item.task.display_url)): item.document.text
            for item in self.activity_documents
        }
        for announcement in self.announcement_documents:
            assessment = analyze_quota(announcement.document.text)
            if assessment.status not in {"partial_sold_out", "sold_out"}:
                continue
            for activity in activities:
                if activity.provider_id != announcement.task.provider["id"]:
                    continue
                if canonical_url(activity.url) == canonical_url(announcement.task.display_url):
                    # mixed_detail pages are already assessed as activity pages;
                    # applying the identical document again duplicates evidence
                    # and partial-period components.
                    continue
                activity_text = activity_docs.get((activity.provider_id, canonical_url(activity.url)), "")
                matched, method, score = match_announcement(
                    activity_url=activity.url,
                    activity_title=activity.title,
                    activity_text=activity_text,
                    announcement_url=announcement.task.display_url,
                    announcement_title=announcement.task.title_hint or announcement.document.title,
                    announcement_text=announcement.document.text,
                )
                if not matched:
                    continue
                if assessment.status == "sold_out" or activity.quota_status != "sold_out":
                    activity.quota_status = assessment.status
                activity.quota_evidence_complete = True
                for component in assessment.components:
                    if component not in activity.components:
                        activity.components.append(component)
                activity.evidence.append(
                    Evidence(
                        source_url=announcement.task.display_url,
                        excerpt=assessment.evidence_excerpt,
                        observed_at=announcement.fetched_at,
                        kind=f"latest_announcement:{method}:{score:.2f}",
                        content_hash=announcement.document.content_hash,
                    )
                )
