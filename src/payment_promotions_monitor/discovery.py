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
from urllib.parse import parse_qs, quote, urlsplit

from .adapters import Document, external_id_from_url, parse_document, summarize_conditions
from .dates import DateRange, lifecycle_for, parse_date_range
from .fetch import FetchResult, canonical_url, fetch_url, is_allowed_url, post_json_url
from .html_extract import parse_html
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

    @staticmethod
    def _task_identity(task: Task) -> tuple[str, str]:
        identity_url = canonical_url(task.fetch_url)
        if task.external_id:
            identity_url += f"#external_id={task.external_id}"
        return task.provider["id"], identity_url

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
                if target["provider_id"] == "taiwanpay":
                    adapter = "taiwanpay_detail"
                elif target["provider_id"] == "fullpay":
                    adapter = "fullpay_detail"
                else:
                    adapter = ""
                external_id = (
                    target.get("external_id") or ""
                    if target["provider_id"] in {"taiwanpay", "fullpay"}
                    else ""
                )
                tasks.append(
                    Task(
                        provider=provider,
                        source_name="既有活動複查",
                        role="activity_detail",
                        fetch_url=target.get("source_url") or target["url"],
                        display_url=target["url"],
                        start_date=target.get("start_date") or "",
                        end_date=target.get("end_date") or "",
                        adapter=adapter,
                        external_id=external_id,
                    )
                )
        return tasks

    def _fetch_fullpay_detail(self, task: Task, fetched_at: str | None = None) -> FetchResult:
        result = fetch_url(
            task.fetch_url,
            task.provider["official_domains"],
            timeout=self.timeout,
            attempts=2,
        )
        payload = json.loads(result.text)
        detail = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("code") != "0000"
            or not isinstance(detail, dict)
            or not str(detail.get("title", "")).strip()
            or not str(detail.get("activity_start_time", "")).strip()
            or not str(detail.get("activity_end_time", "")).strip()
        ):
            raise RuntimeError("Fullpay detail API validation failed")

        event_id = task.external_id or external_id_from_url(task.display_url)
        if not event_id or not event_id.isdigit():
            raise RuntimeError("Fullpay detail is missing a numeric EventId")
        quota_url = f"https://service.pxpayplus.com/px-advertise/web/activity/login_info/{event_id}"
        observed_at = fetched_at or self.now.isoformat()
        full_quota_time = ""
        quota_unavailable = False
        try:
            quota_result = fetch_url(
                quota_url,
                task.provider["official_domains"],
                timeout=self.timeout,
                attempts=2,
            )
            quota_payload = json.loads(quota_result.text)
            quota_data = quota_payload.get("data") if isinstance(quota_payload, dict) else None
            if (
                not isinstance(quota_payload, dict)
                or quota_payload.get("code") != "0000"
                or not isinstance(quota_data, dict)
            ):
                raise RuntimeError("Fullpay public quota API validation failed")
            raw_full_quota_time = quota_data.get("full_quota_time")
            full_quota_time = raw_full_quota_time.strip() if isinstance(raw_full_quota_time, str) else ""
            self.attempts.append(
                SourceAttempt(
                    provider_id=task.provider["id"],
                    provider_name=task.provider["name"],
                    source_name=f"{task.source_name} → 公開額滿狀態",
                    role="status_page",
                    url=quota_url,
                    ok=True,
                    fetched_at=observed_at,
                    status_code=quota_result.status_code,
                    final_url=quota_result.final_url,
                )
            )
        except Exception as exc:
            quota_unavailable = True
            self.attempts.append(
                SourceAttempt(
                    provider_id=task.provider["id"],
                    provider_name=task.provider["name"],
                    source_name=f"{task.source_name} → 公開額滿狀態",
                    role="status_page",
                    url=quota_url,
                    ok=False,
                    fetched_at=observed_at,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                )
            )
        payload["public_quota_status"] = {
            "source_url": quota_url,
            "full_quota_time": full_quota_time,
            "notice": f"活動已達上限（官方公開狀態時間：{full_quota_time}）" if full_quota_time else "",
            "unavailable": quota_unavailable,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return FetchResult(
            requested_url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            body=body,
            text=body.decode("utf-8"),
            content_type="application/json",
            content_hash=hashlib.sha256(body).hexdigest(),
        )

    def _attempt(self, task: Task) -> CollectedDocument | None:
        key = self._task_identity(task)
        if key in self._seen_fetches:
            return None
        self._seen_fetches.add(key)
        fetched_at = self.now.isoformat()
        try:
            if task.adapter == "fullpay_detail":
                result = self._fetch_fullpay_detail(task, fetched_at)
            elif task.adapter in {"taiwanpay_listing", "taiwanpay_detail"}:
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
                    try:
                        candidate = post_json_url(
                            task.fetch_url,
                            payload,
                            task.provider["official_domains"],
                            timeout=self.timeout,
                        )
                    except Exception as exc:
                        api_error = f"{type(exc).__name__}: {exc}"
                        continue
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

    @staticmethod
    def _fullpay_event_id(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "marketing.pxpayplus.com":
            return None
        if parsed.path != "/pxplus_marketing_page/activity_content_page":
            return None
        values = parse_qs(parsed.query).get("EventId", [])
        event_id = values[0] if values else ""
        return event_id if event_id.isdigit() else None

    def _fullpay_activity_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        current_id = task.external_id or self._fullpay_event_id(task.display_url)
        discovered: list[Task] = []
        seen_ids: set[str] = set()
        for url, anchor_text in collected.document.links:
            event_id = self._fullpay_event_id(url)
            if not event_id or event_id == current_id or event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            display_url = (
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                f"activity_content_page?EventId={event_id}"
            )
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → 全支付活動 {event_id}",
                    role="activity_detail",
                    fetch_url=f"https://service.pxpayplus.com/px-advertise/web/activity/detail/{event_id}",
                    display_url=display_url,
                    title_hint=anchor_text,
                    max_links=30,
                    adapter="fullpay_detail",
                    external_id=event_id,
                )
            )
        return discovered

    def _set_discovery_result(self, task: Task, discovered_count: int, *, total_count: int | None = None) -> None:
        total = discovered_count if total_count is None else total_count
        for attempt in reversed(self.attempts):
            if attempt.provider_id == task.provider["id"] and canonical_url(attempt.url) == canonical_url(task.fetch_url):
                attempt.discovered_count = discovered_count
                if total == 0:
                    attempt.coverage_issue = "listing_zero_discovery"
                elif total > task.max_links:
                    attempt.coverage_issue = "listing_reached_max_links"
                break

    def _collect_fullpay_news(self, collected: CollectedDocument) -> None:
        task = collected.task
        data = collected.document.raw_json
        records: list[tuple[str, str, dict[str, Any]]] = []
        if isinstance(data, dict):
            for category, values in data.items():
                if not isinstance(values, dict):
                    continue
                for record_id, record in values.items():
                    if isinstance(record, dict):
                        records.append((str(category), str(record_id), record))
        records.sort(key=lambda item: str(item[2].get("date", "")), reverse=True)
        selected = records[: task.max_links]
        for category, record_id, record in selected:
            title = str(record.get("title", "")).strip() or f"全支付公告 {record_id}"
            date_text = str(record.get("date", "")).strip()
            content = str(record.get("content", ""))
            detail_url = f"https://www.pxpayplus.com/news_detail/{quote(category, safe='')}/{quote(record_id, safe='')}"
            parsed = parse_html(content, detail_url)
            official_links = [
                f"official_link: {url} {anchor_text}".strip()
                for url, anchor_text in parsed.links
                if is_allowed_url(url, task.provider["official_domains"])
            ]
            text = "\n".join(
                value
                for value in (f"title: {title}", f"date: {date_text}", parsed.text, *official_links)
                if value
            )
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            announcement_task = Task(
                provider=task.provider,
                source_name=f"{task.source_name} → {title[:80]}",
                role="announcement_detail",
                fetch_url=task.fetch_url,
                display_url=detail_url,
                title_hint=title,
                adapter="fullpay_news_detail",
                external_id=f"{category}:{record_id}",
            )
            self.announcement_documents.append(
                CollectedDocument(
                    task=announcement_task,
                    document=Document(
                        url=detail_url,
                        title=title,
                        text=text,
                        links=parsed.links,
                        content_hash=hashlib.sha256(raw).hexdigest(),
                        raw_json=record,
                    ),
                    fetched_at=collected.fetched_at,
                )
            )
        self._set_discovery_result(task, len(selected), total_count=len(records))

    def _discovered_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        if task.adapter == "fullpay_news_listing":
            self._collect_fullpay_news(collected)
            return []
        if task.adapter == "fullpay_hub_listing":
            all_discovered = self._fullpay_activity_tasks(collected)
            discovered = all_discovered[: task.max_links]
            self._set_discovery_result(task, len(discovered), total_count=len(all_discovered))
            return discovered
        if task.adapter == "fullpay_detail":
            return self._fullpay_activity_tasks(collected)[: task.max_links]
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
                # Breadth-first scheduling ensures every provider's registered
                # source gets a chance before one provider's link graph grows.
                queue.extend(discovered)
            delay = float(self.config.get("request_delay_seconds", 0.0))
            if delay:
                time.sleep(delay)

        pending_by_provider: dict[str, dict[str, Any]] = {}
        pending_identities: set[tuple[str, str]] = set()
        for task in queue[cursor:]:
            identity = self._task_identity(task)
            if identity in self._seen_fetches or identity in pending_identities:
                continue
            pending_identities.add(identity)
            provider_id = task.provider["id"]
            pending = pending_by_provider.setdefault(
                provider_id,
                {
                    "provider_id": provider_id,
                    "provider_name": task.provider["name"],
                    "source_name": "全域擷取上限",
                    "url": task.display_url,
                    "issue": "crawl_reached_max_total_pages",
                    "discovered_count": 0,
                },
            )
            pending["discovered_count"] += 1

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
            crawl_limit_pending=list(pending_by_provider.values()),
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
        public_quota_status = (
            document.raw_json.get("public_quota_status", {})
            if isinstance(document.raw_json, dict)
            else {}
        )
        quota_status = quota.status
        quota_evidence_complete = quota.evidence_complete
        if (
            quota_status == "not_marked_full"
            and isinstance(public_quota_status, dict)
            and public_quota_status.get("unavailable") is True
        ):
            quota_status = "unknown_source_failure"
            quota_evidence_complete = False
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
            quota_api_notice = (
                str(public_quota_status.get("notice", ""))
                if isinstance(public_quota_status, dict)
                else ""
            )
            from_quota_api = bool(quota_api_notice and quota_api_notice in quota.evidence_excerpt)
            evidence.append(
                Evidence(
                    source_url=(
                        str(public_quota_status.get("source_url", task.display_url))
                        if from_quota_api
                        else task.display_url
                    ),
                    excerpt=quota.evidence_excerpt,
                    observed_at=collected.fetched_at,
                    kind="activity_status_api" if from_quota_api else "activity_page",
                    content_hash=document.content_hash,
                )
            )
        review_required = (
            date_range.confidence in {"none", "low"}
            or lifecycle == "unknown"
            or "monitor_review_required: true" in document.text
            or quota_status == "unknown_source_failure"
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
            quota_status=quota_status,
            quota_evidence_complete=quota_evidence_complete,
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
