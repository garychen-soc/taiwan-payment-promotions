from __future__ import annotations

import json
import hashlib
import html
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

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
    summary_hint: str = ""
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
    def __init__(
        self,
        config: dict[str, Any],
        now: datetime,
        *,
        timeout: float = 20.0,
        discovery_state: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.now = now
        self.timeout = timeout
        self.discovery_state = discovery_state or {}
        self.attempts: list[SourceAttempt] = []
        self.activity_documents: list[CollectedDocument] = []
        self.announcement_documents: list[CollectedDocument] = []
        self._seen_fetches: set[tuple[str, str]] = set()
        self._fullpay_scan_plans: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _task_identity(task: Task) -> tuple[str, str]:
        identity_url = canonical_url(task.fetch_url)
        if task.external_id:
            identity_url += f"#external_id={task.external_id}"
        return task.provider["id"], identity_url

    def _initial_tasks(self, mode: str, recheck_targets: list[dict[str, str]]) -> list[Task]:
        tasks: list[Task] = []
        probe_tasks: list[Task] = []
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
                } and source.get("adapter") not in {"famipay_listing"}:
                    continue
                if mode == "full" and source.get("adapter") == "fullpay_id_scan":
                    probe_tasks.extend(self._fullpay_probe_tasks(provider, source))
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
        if mode in {"full", "status"}:
            for target in recheck_targets:
                provider = providers.get(target["provider_id"])
                if not provider:
                    continue
                if mode == "full" and target["provider_id"] in {
                    "easywallet",
                    "ipassmoney",
                    "jkopay",
                }:
                    # Their registered listing adapters enumerate the complete
                    # public catalogue and attach authoritative list metadata.
                    # Generic historical rechecks are queued before discovered
                    # details and would otherwise win de-duplication, dropping
                    # titles/dates that exist only on the listing.
                    continue
                source_url = target.get("source_url") or target["url"]
                if target["provider_id"] == "famipay" or (
                    target["provider_id"] == "pxpay" and "GetEventList" in source_url
                ):
                    # These records are synthesized from their registered
                    # official listing/API adapters. Re-fetching the raw source
                    # as a generic detail would create a misleading duplicate;
                    # the registered adapters are the authoritative refresh
                    # path, while prior confirmed state remains in storage.
                    continue
                if target["provider_id"] == "taiwanpay":
                    adapter = "taiwanpay_detail"
                elif target["provider_id"] == "fullpay":
                    adapter = "fullpay_detail"
                elif target["provider_id"] == "easywallet":
                    adapter = "easywallet_detail"
                elif target["provider_id"] == "ipassmoney":
                    adapter = "ipass_detail"
                elif target["provider_id"] == "jkopay":
                    adapter = "jkopay_detail"
                elif (
                    target["provider_id"] == "pxpay"
                    and "/campaign/pxpay-card/" in source_url
                ):
                    adapter = "pxmart_campaign_detail"
                else:
                    adapter = ""
                external_id = target.get("external_id") or ""
                tasks.append(
                    Task(
                        provider=provider,
                        source_name="既有活動複查",
                        role="activity_detail",
                        fetch_url=source_url,
                        display_url=target["url"],
                        title_hint=target.get("title") or "",
                        start_date=target.get("start_date") or "",
                        end_date=target.get("end_date") or "",
                        adapter=adapter,
                        external_id=external_id,
                    )
                )
        # Numeric probes run only after every registered source and seed has
        # had a chance. This keeps one provider's ID space from starving the
        # rest of the registry when a global crawl cap is configured.
        return tasks + probe_tasks

    def _fullpay_probe_tasks(
        self,
        provider: dict[str, Any],
        source: dict[str, Any],
    ) -> list[Task]:
        provider_id = str(provider["id"])
        raw_state = self.discovery_state.get(provider_id, {})
        if isinstance(raw_state, dict):
            previous_highest = max(0, int(raw_state.get("highest_valid_event_id", 0)))
            previous_frontier = max(0, int(raw_state.get("scan_frontier_event_id", 0)))
        else:  # backward-compatible with the original single-watermark state
            previous_highest = max(0, int(raw_state or 0))
            previous_frontier = previous_highest
        minimum = max(1, int(source.get("minimum_event_id", 1)))
        bootstrap_end = max(minimum, int(source.get("bootstrap_end_id", 160)))
        rescan_window = max(1, int(source.get("rescan_window", 128)))
        frontier_buffer = max(1, int(source.get("frontier_buffer", 32)))
        maximum_probe_ids = max(1, int(source.get("maximum_probe_ids", 220)))
        if previous_frontier:
            start = max(minimum, previous_frontier - rescan_window + 1)
            end = previous_frontier + frontier_buffer
        else:
            start = minimum
            end = bootstrap_end
        end = min(end, start + maximum_probe_ids - 1)
        self._fullpay_scan_plans[provider_id] = {
            "previous_highest": previous_highest,
            "previous_frontier": previous_frontier,
            "start": start,
            "end": end,
            "outcomes": {},
        }
        return [
            Task(
                provider=provider,
                source_name=f"{source['name']} → EventId {event_id}",
                role="activity_probe",
                fetch_url=(
                    "https://service.pxpayplus.com/px-advertise/web/activity/"
                    f"detail/{event_id}"
                ),
                display_url=(
                    "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                    f"activity_content_page?EventId={event_id}"
                ),
                max_links=0,
                adapter="fullpay_id_probe",
                external_id=str(event_id),
            )
            for event_id in range(start, end + 1)
        ]

    def _record_fullpay_probe_outcome(self, task: Task, outcome: str) -> None:
        plan = self._fullpay_scan_plans.get(str(task.provider["id"]))
        if not plan or not task.external_id.isdigit():
            return
        event_id = int(task.external_id)
        if int(plan["start"]) <= event_id <= int(plan["end"]):
            plan["outcomes"][event_id] = outcome

    def _fullpay_discovery_state_updates(self) -> dict[str, dict[str, int]]:
        updates: dict[str, dict[str, int]] = {}
        for provider_id, plan in self._fullpay_scan_plans.items():
            expected = set(range(int(plan["start"]), int(plan["end"]) + 1))
            outcomes: dict[int, str] = plan["outcomes"]
            if expected - outcomes.keys() or any(value == "failure" for value in outcomes.values()):
                continue
            valid_ids = [event_id for event_id, value in outcomes.items() if value == "valid"]
            highest_valid = max([int(plan["previous_highest"]), *valid_ids])
            updates[provider_id] = {
                "highest_valid_event_id": highest_valid,
                "scan_frontier_event_id": max(int(plan["previous_frontier"]), int(plan["end"])),
            }
        return updates

    def _fullpay_discovery_scan_summary(self) -> dict[str, dict[str, Any]]:
        state_updates = self._fullpay_discovery_state_updates()
        summary: dict[str, dict[str, Any]] = {}
        for provider_id, plan in self._fullpay_scan_plans.items():
            expected = set(range(int(plan["start"]), int(plan["end"]) + 1))
            outcomes: dict[int, str] = plan["outcomes"]
            valid_ids = [event_id for event_id, value in outcomes.items() if value == "valid"]
            previous_highest = int(plan["previous_highest"])
            previous_frontier = int(plan["previous_frontier"])
            highest_valid = max([previous_highest, *valid_ids])
            update = state_updates.get(provider_id, {})
            summary[provider_id] = {
                "scan_start": int(plan["start"]),
                "scan_end": int(plan["end"]),
                "probed_count": len(outcomes),
                "valid_count": sum(value == "valid" for value in outcomes.values()),
                "empty_count": sum(value == "not_found" for value in outcomes.values()),
                "failure_count": sum(value == "failure" for value in outcomes.values())
                + len(expected - outcomes.keys()),
                "complete": not (expected - outcomes.keys())
                and all(value != "failure" for value in outcomes.values()),
                "previous_highest_valid_event_id": previous_highest,
                "highest_valid_event_id": highest_valid,
                "previous_scan_frontier_event_id": previous_frontier,
                "scan_frontier_event_id": int(update.get("scan_frontier_event_id", previous_frontier)),
                "frontier_advanced": int(update.get("scan_frontier_event_id", previous_frontier))
                > previous_frontier,
            }
        return summary

    def _fullpay_detail_is_expired(self, detail: dict[str, Any]) -> bool:
        value = str(detail.get("activity_end_time", ""))
        match = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", value)
        if not match:
            return False
        try:
            return date(*(int(part) for part in match.groups())) < self.now.date()
        except ValueError:
            return False

    def _fetch_fullpay_detail(
        self,
        task: Task,
        fetched_at: str | None = None,
        *,
        initial_result: FetchResult | None = None,
        skip_expired_quota: bool = False,
    ) -> FetchResult:
        result = initial_result or fetch_url(
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
        if skip_expired_quota and self._fullpay_detail_is_expired(detail):
            payload["public_quota_status"] = {
                "source_url": quota_url,
                "full_quota_time": "",
                "notice": "",
                "unavailable": False,
                "skipped": "expired_activity",
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

    @staticmethod
    def _embedded_next_data(result: FetchResult) -> Document:
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            result.text,
            re.I | re.S,
        )
        if not match:
            raise RuntimeError("PX Mart campaign page is missing __NEXT_DATA__")
        try:
            data = json.loads(match.group(1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("PX Mart campaign __NEXT_DATA__ is invalid") from exc
        if not isinstance(data, dict):
            raise RuntimeError("PX Mart campaign __NEXT_DATA__ has an unexpected shape")
        return Document(
            url=result.final_url,
            title="PX Pay 與信用卡",
            text="PX Pay 與信用卡官方活動列表",
            links=[],
            content_hash=result.content_hash,
            raw_json=data,
        )

    @staticmethod
    def _html_listing_document(result: FetchResult, title: str) -> Document:
        document = parse_document(result)
        return Document(
            url=document.url,
            title=document.title or title,
            text=document.text,
            links=document.links,
            content_hash=document.content_hash,
            raw_json={"html": result.text},
        )

    @staticmethod
    def _jkopay_document(result: FetchResult) -> Document:
        document = parse_document(result)
        source_html = parse_html(result.text, result.final_url)
        decoded_chunks: list[str] = []
        for match in re.finditer(
            r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)',
            result.text,
        ):
            try:
                decoded = json.loads(match.group(1))
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, str):
                decoded_chunks.append(decoded)
        decoded_html = "\n".join(decoded_chunks)
        parsed = parse_html(decoded_html, result.final_url) if decoded_html else None
        description_match = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']',
            result.text,
            re.I,
        )
        description = html.unescape(description_match.group(1)).strip() if description_match else ""
        text = "\n".join(
            value
            for value in (
                description,
                parsed.text if parsed else "",
                document.text,
            )
            if value
        )
        links = list(document.links)
        if parsed:
            links.extend(parsed.links)
        return Document(
            url=result.final_url,
            # JkoPay commonly starts promotion titles with a date range such as
            # "2026/7/1 - 9/30 ...". The generic title cleanup treats " - "
            # as a site-name separator, so preserve the official metadata.
            title=source_html.title or document.title,
            text=text,
            links=links,
            content_hash=result.content_hash,
        )

    @staticmethod
    def _official_metadata_document(document: Document, task: Task) -> Document:
        metadata = "\n".join(
            value
            for value in (
                f"startDate: {task.start_date}" if task.start_date else "",
                f"endDate: {task.end_date}" if task.end_date else "",
                f"official_api_title: {task.title_hint}" if task.title_hint else "",
                f"official_api_summary: {task.summary_hint}" if task.summary_hint else "",
            )
            if value
        )
        if not metadata:
            return document
        text = f"{metadata}\n{document.text}" if document.text else metadata
        digest = hashlib.sha256(f"{document.content_hash}\n{metadata}".encode("utf-8")).hexdigest()
        return Document(
            url=document.url,
            title=task.title_hint or document.title,
            text=text,
            links=document.links,
            content_hash=digest,
            raw_json=document.raw_json,
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
            elif task.adapter == "fullpay_id_probe":
                initial_result = fetch_url(
                    task.fetch_url,
                    task.provider["official_domains"],
                    timeout=self.timeout,
                    attempts=2,
                )
                initial_payload = json.loads(initial_result.text)
                if isinstance(initial_payload, dict) and initial_payload.get("code") == "2001":
                    self._record_fullpay_probe_outcome(task, "not_found")
                    self.attempts.append(
                        SourceAttempt(
                            provider_id=task.provider["id"],
                            provider_name=task.provider["name"],
                            source_name=task.source_name,
                            role=task.role,
                            url=task.fetch_url,
                            ok=True,
                            fetched_at=fetched_at,
                            status_code=initial_result.status_code,
                            final_url=initial_result.final_url,
                        )
                    )
                    return None
                result = self._fetch_fullpay_detail(
                    task,
                    fetched_at,
                    initial_result=initial_result,
                    skip_expired_quota=True,
                )
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
            if task.adapter == "pxmart_campaign_listing":
                document = self._embedded_next_data(result)
            elif task.adapter == "famipay_listing":
                document = Document(
                    url=result.final_url,
                    title="全家官方活動列表",
                    text="全家便利商店官方活動列表",
                    links=[],
                    content_hash=result.content_hash,
                    raw_json={"html": result.text},
                )
            elif task.adapter in {"easywallet_listing", "ipass_listing", "jkopay_listing"}:
                document = self._html_listing_document(result, task.source_name)
            elif task.adapter == "jkopay_detail":
                document = self._jkopay_document(result)
            else:
                document = parse_document(result)
            if task.adapter == "pi_wordpress_listing" and not isinstance(document.raw_json, list):
                raise RuntimeError("Pi wallet WordPress response is not a post list")
            if task.adapter in {"pxmart_quota_periods", "pxmart_quota_detail"}:
                quota_data = document.raw_json
                if (
                    not isinstance(quota_data, dict)
                    or quota_data.get("success") is not True
                    or not isinstance(quota_data.get("data"), list)
                ):
                    raise RuntimeError("PX Pay quota API validation failed")
            if task.adapter in {"pi_post_detail", "easywallet_detail", "ipass_detail"}:
                document = self._official_metadata_document(document, task)
            if task.adapter in {"fullpay_detail", "fullpay_id_probe"}:
                self._record_fullpay_probe_outcome(task, "valid")
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
                    discovered_count=1 if task.adapter == "fullpay_id_probe" else 0,
                )
            )
            return CollectedDocument(task=task, document=document, fetched_at=fetched_at)
        except Exception as exc:  # individual sources must not abort the run
            if task.adapter in {"fullpay_detail", "fullpay_id_probe"}:
                self._record_fullpay_probe_outcome(task, "failure")
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
                    coverage_issue="id_probe_incomplete" if task.adapter == "fullpay_id_probe" else None,
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

    @staticmethod
    def _wordpress_rendered(value: object) -> str:
        rendered = value.get("rendered", "") if isinstance(value, dict) else value
        if not isinstance(rendered, str):
            return ""
        parsed = parse_html(html.unescape(rendered), "https://web.piapp.com.tw/")
        return html.unescape(parsed.text).strip()

    def _pi_post_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        records = collected.document.raw_json
        if not isinstance(records, list):
            self._set_discovery_result(task, 0)
            return []
        discovered: list[Task] = []
        seen: set[str] = set()
        next_role = "announcement_detail" if task.role == "announcement_listing" else "activity_detail"
        for record in records:
            if not isinstance(record, dict):
                continue
            url = str(record.get("link", "")).strip()
            if not url or not is_allowed_url(url, task.provider["official_domains"]):
                continue
            normalized = canonical_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            title = self._wordpress_rendered(record.get("title"))
            summary = self._wordpress_rendered(record.get("excerpt"))
            summary_dates = (
                parse_date_range(f"活動期間：{summary}")
                if summary
                else DateRange(None, None, "none")
            )
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {title[:80] or '官方文章'}",
                    role=next_role,
                    fetch_url=url,
                    display_url=url,
                    title_hint=title,
                    summary_hint=summary,
                    adapter="pi_post_detail",
                    external_id=str(record.get("id", "")),
                    start_date=summary_dates.start.isoformat() if summary_dates.start and summary_dates.end else "",
                    end_date=summary_dates.end.isoformat() if summary_dates.start and summary_dates.end else "",
                )
            )
            if len(discovered) >= task.max_links:
                break
        self._set_discovery_result(task, len(discovered), total_count=len(records))
        return discovered

    @staticmethod
    def _listing_html(document: Document) -> str:
        if not isinstance(document.raw_json, dict):
            return ""
        value = document.raw_json.get("html", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _clean_html_fragment(value: str, base_url: str) -> str:
        return parse_html(html.unescape(value), base_url).text.strip()

    def _easywallet_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        source = self._listing_html(collected.document)
        records: list[Task] = []
        seen_ids: set[str] = set()
        card_pattern = re.compile(
            r'<a[^>]+href=["\']'
            r'(?P<href>/benefit/content(?:\.php)?\?id=(?P<id>\d+))'
            r'["\'][^>]*>(?P<body>.*?)</a>',
            re.I | re.S,
        )
        for match in card_pattern.finditer(source):
            external_id = match.group("id")
            if external_id in seen_ids:
                continue
            body = match.group("body")
            title_match = re.search(
                r'<p[^>]+class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>(.*?)</p>',
                body,
                re.I | re.S,
            )
            date_match = re.search(
                r'<p[^>]+class=["\'][^"\']*\bdate\b[^"\']*["\'][^>]*>(.*?)</p>',
                body,
                re.I | re.S,
            )
            if not title_match:
                continue
            title = self._clean_html_fragment(title_match.group(1), collected.document.url)
            date_text = (
                self._clean_html_fragment(date_match.group(1), collected.document.url)
                if date_match
                else ""
            )
            date_range = parse_date_range(f"活動期間：{date_text}") if date_text else DateRange(None, None, "none")
            if date_range.end and date_range.end < self.now.date():
                continue
            seen_ids.add(external_id)
            detail_url = f"https://easywallet.easycard.com.tw/benefit/content?id={external_id}"
            records.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {title[:80]}",
                    role="activity_detail",
                    fetch_url=detail_url,
                    display_url=detail_url,
                    title_hint=title,
                    adapter="easywallet_detail",
                    external_id=external_id,
                    start_date=date_range.start.isoformat() if date_range.start else "",
                    end_date=date_range.end.isoformat() if date_range.end else "",
                )
            )
        selected = records[: task.max_links]
        self._set_discovery_result(task, len(selected), total_count=len(records))
        return selected

    def _ipass_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        source = self._listing_html(collected.document)
        detail_tasks: list[Task] = []
        seen_ids: set[str] = set()
        card_pattern = re.compile(
            r'<div[^>]+class=["\'][^"\']*\bportfolio-title\b[^"\']*["\'][^>]*>'
            r'.*?<h3[^>]*>.*?<a[^>]+href=["\']'
            r'(?P<href>/Preferential/Detail/(?P<id>[A-Za-z0-9_-]+))'
            r'["\'][^>]*>(?P<title>.*?)</a>.*?</h3>'
            r'(?P<meta>.*?)</div>',
            re.I | re.S,
        )
        for match in card_pattern.finditer(source):
            external_id = match.group("id")
            if external_id in seen_ids:
                continue
            title = self._clean_html_fragment(match.group("title"), collected.document.url)
            dates = re.findall(
                r'<span[^>]+class=["\'][^"\']*\blabeldate\b[^"\']*["\'][^>]*>(.*?)</span>',
                match.group("meta"),
                re.I | re.S,
            )
            date_range = DateRange(None, None, "none")
            if len(dates) >= 2:
                start_text = self._clean_html_fragment(dates[0], collected.document.url)
                end_text = self._clean_html_fragment(dates[1], collected.document.url)
                date_range = parse_date_range(f"活動期間：{start_text} ~ {end_text}")
            if date_range.end and date_range.end < self.now.date():
                continue
            seen_ids.add(external_id)
            detail_url = f"https://www.i-pass.com.tw/Preferential/Detail/{external_id}"
            detail_tasks.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {title[:80]}",
                    role="activity_detail",
                    fetch_url=detail_url,
                    display_url=detail_url,
                    title_hint=title,
                    adapter="ipass_detail",
                    external_id=external_id,
                    start_date=date_range.start.isoformat() if date_range.start else "",
                    end_date=date_range.end.isoformat() if date_range.end else "",
                )
            )

        page_tasks: list[Task] = []
        seen_pages: set[str] = set()
        for value in re.findall(
            r'href=["\'](/Preferential\?[^"\']*\bpage=\d+[^"\']*)["\']',
            source,
            re.I,
        ):
            path = html.unescape(value)
            page_url = f"https://www.i-pass.com.tw{path}"
            normalized = canonical_url(page_url)
            if normalized == canonical_url(task.fetch_url) or normalized in seen_pages:
                continue
            seen_pages.add(normalized)
            page_tasks.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → 分頁",
                    role=task.role,
                    fetch_url=page_url,
                    display_url=page_url,
                    max_links=task.max_links,
                    adapter="ipass_listing",
                )
            )
        selected_details = detail_tasks[: task.max_links]
        self._set_discovery_result(task, len(selected_details), total_count=len(detail_tasks))
        return page_tasks + selected_details

    def _jkopay_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        source = self._listing_html(collected.document)
        candidates = re.findall(
            r'https://mkt\.jkopay\.com/(?:zh-TW/)?(?:campaign|event)/[A-Za-z0-9_-]+',
            source,
            re.I,
        )
        discovered: list[Task] = []
        seen_slugs: set[str] = set()
        for candidate in candidates:
            parsed = urlsplit(candidate)
            path = re.sub(r"^/zh-TW", "", parsed.path, flags=re.I)
            parts = path.strip("/").split("/")
            if len(parts) != 2 or parts[0] not in {"campaign", "event"}:
                continue
            kind, slug = parts
            if slug.lower().startswith("newevent"):
                continue
            identity = slug.lower()
            if identity in seen_slugs:
                continue
            seen_slugs.add(identity)
            detail_url = f"https://mkt.jkopay.com/zh-TW/{kind}/{slug}"
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {slug}",
                    role="activity_detail",
                    fetch_url=detail_url,
                    display_url=detail_url,
                    adapter="jkopay_detail",
                    # Campaign and event routes are aliases on the official
                    # site. Slug identity also merges the matching seed.
                    external_id=slug,
                )
            )
        selected = discovered[: task.max_links]
        self._set_discovery_result(task, len(selected), total_count=len(discovered))
        return selected

    @staticmethod
    def _pxmart_campaigns(data: object) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        props = data.get("props", {})
        page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
        campaigns = page_props.get("campaigns", []) if isinstance(page_props, dict) else []
        return [item for item in campaigns if isinstance(item, dict)] if isinstance(campaigns, list) else []

    def _pxmart_campaign_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        labeled: list[dict[str, Any]] = []
        for campaign in self._pxmart_campaigns(collected.document.raw_json):
            attributes = campaign.get("attributes", {})
            if not isinstance(attributes, dict) or str(attributes.get("label", "")).strip().casefold() != "px pay":
                continue
            labeled.append(campaign)

        discovered: list[Task] = []
        for campaign in labeled:
            attributes = campaign["attributes"]
            title = html.unescape(str(attributes.get("title", "")).strip())
            start_date = str(attributes.get("openDate", "")).strip()
            end_date = str(attributes.get("closeDate", "")).strip()
            try:
                if end_date and date.fromisoformat(end_date) < self.now.date():
                    continue
            except ValueError:
                pass
            slug = str(attributes.get("slug", "")).strip() or title
            if not title or not slug:
                continue
            display_url = f"https://www.pxmart.com.tw/campaign/pxpay-card/{quote(slug, safe='')}"
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {title[:80]}",
                    role="activity_detail",
                    fetch_url=display_url,
                    display_url=display_url,
                    title_hint=title,
                    adapter="pxmart_campaign_detail",
                    external_id=f"pxmart-campaign-{campaign.get('id', hashlib.sha256(title.encode()).hexdigest()[:12])}",
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            if len(discovered) >= task.max_links:
                break
        self._set_discovery_result(task, len(discovered), total_count=len(labeled))
        return discovered

    @staticmethod
    def _period_dates(value: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"\s*(20\d{2}-\d{2}-\d{2})\s*~\s*(20\d{2}-\d{2}-\d{2})\s*", value)
        if not match:
            return None
        try:
            date.fromisoformat(match.group(1))
            date.fromisoformat(match.group(2))
        except ValueError:
            return None
        return match.group(1), match.group(2)

    def _pxmart_quota_tasks(self, collected: CollectedDocument) -> list[Task]:
        task = collected.task
        data = collected.document.raw_json
        records = data.get("data", []) if isinstance(data, dict) and data.get("success") is True else []
        if not isinstance(records, list):
            records = []
        eligible: list[tuple[str, str, str]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            period = str(record.get("period", ""))
            parsed = self._period_dates(period)
            if not parsed or date.fromisoformat(parsed[1]) < self.now.date():
                continue
            eligible.append((period, parsed[0], parsed[1]))

        discovered: list[Task] = []
        for period, start_date, end_date in eligible[: task.max_links]:
            query = urlencode(
                {
                    "event_type": "1",
                    "period": period,
                    "pay_type": json.dumps(["1"], separators=(",", ":")),
                }
            )
            discovered.append(
                Task(
                    provider=task.provider,
                    source_name=f"{task.source_name} → {period}",
                    role="quota_listing_detail",
                    fetch_url=f"https://cardpoint.pxmartevent.com.tw/event/GetEventList?{query}",
                    display_url="https://cardpoint.pxmartevent.com.tw/",
                    title_hint="PX Pay 指定銀行週末滿額福利點活動",
                    adapter="pxmart_quota_detail",
                    external_id=f"pxpay-quota-{period}",
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        self._set_discovery_result(task, len(discovered), total_count=len(eligible))
        return discovered

    @staticmethod
    def _pxpay_eligible_record(record: dict[str, Any]) -> bool:
        raw = record.get("pay_type")
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return False
        return isinstance(values, list) and any(str(value) == "1" for value in values)

    def _collect_pxmart_quota(self, collected: CollectedDocument) -> None:
        task = collected.task
        data = collected.document.raw_json
        records = data.get("data", []) if isinstance(data, dict) and data.get("success") is True else []
        if not isinstance(records, list):
            records = []
        records = [
            record
            for record in records
            if isinstance(record, dict) and self._pxpay_eligible_record(record)
        ]
        if not records:
            self._set_discovery_result(task, 0)
            return

        sold_out: list[dict[str, Any]] = []
        available: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for record in records:
            disbursed_time = str(record.get("disbursed_time") or "").strip()
            left_count = record.get("left_count")
            if disbursed_time or (isinstance(left_count, (int, float)) and left_count <= 0):
                sold_out.append(record)
            elif isinstance(left_count, (int, float)) and left_count > 0:
                available.append(record)
            else:
                unknown.append(record)

        start_date = task.start_date
        end_date = task.end_date
        lines = [
            f"title: {task.title_hint}",
            f"活動期間：{start_date} 至 {end_date}",
        ]
        for record in sold_out:
            bank = str(record.get("bank_name", "指定銀行")).strip()
            disbursed = str(record.get("disbursed_time") or "").strip()
            timing = f"，官方額滿時間 {disbursed}" if disbursed else ""
            lines.append(f"指定銀行 {bank} 本期名額已額滿{timing}。")
        for record in available:
            bank = str(record.get("bank_name", "指定銀行")).strip()
            left_count = int(record["left_count"])
            lines.append(f"指定銀行 {bank} 官方即時狀態尚有 {left_count:,} 份名額。")
        for record in unknown:
            bank = str(record.get("bank_name", "指定銀行")).strip()
            lines.append(f"指定銀行 {bank} 未提供可判讀的剩餘名額。")
        type_texts = list(
            dict.fromkeys(str(record.get("type_text", "")).strip() for record in records if record.get("type_text"))
        )
        lines.extend(f"回饋條件：{value}" for value in type_texts)
        if sold_out and not available and not unknown:
            lines.append("所有指定銀行名額皆已額滿。")

        serialized = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        first_notice = lines[2] if sold_out else ""
        available_notice = next((line for line in lines if "官方即時狀態尚有" in line), "")
        public_quota_status = {
            "source_url": task.fetch_url,
            "notice": first_notice,
            "available_notice": available_notice,
            "sold_out_count": len(sold_out),
            "available_count": len(available),
            "unknown_count": len(unknown),
            "unavailable": False,
        }
        document = Document(
            url=task.fetch_url,
            title=task.title_hint,
            text="\n".join(lines),
            links=[],
            content_hash=hashlib.sha256(serialized).hexdigest(),
            raw_json={"records": records, "public_quota_status": public_quota_status},
        )
        activity_task = Task(
            provider=task.provider,
            source_name=task.source_name,
            role="activity_detail",
            fetch_url=task.fetch_url,
            # The period-bearing official API URL is both stable and unique.
            # A shared quota homepage would collide when two periods overlap.
            display_url=task.fetch_url,
            title_hint=task.title_hint,
            adapter="pxmart_quota_activity",
            external_id=task.external_id,
            start_date=start_date,
            end_date=end_date,
        )
        self.activity_documents.append(CollectedDocument(activity_task, document, collected.fetched_at))
        self._set_discovery_result(task, 1)

    def _collect_famipay_listing(self, collected: CollectedDocument) -> None:
        task = collected.task
        data = collected.document.raw_json
        source = data.get("html", "") if isinstance(data, dict) else ""
        if not isinstance(source, str) or not source:
            self._set_discovery_result(task, 0)
            return
        chunks = re.split(r'<div\s+class=["\']card card--event["\']\s*>', source, flags=re.I)[1:]
        records: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for chunk in chunks:
            title_match = re.search(r'class=["\']card__title["\'][^>]*>(.*?)</h\d>', chunk, re.I | re.S)
            if not title_match:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1)))
            title = re.sub(r"\s+", " ", title).strip()
            if "famipay" not in title.casefold():
                continue
            date_match = re.search(r'class=["\']card__date["\'][^>]*>(.*?)</p>', chunk, re.I | re.S)
            summary_match = re.search(r'class=["\']card__text[^"\']*["\'][^>]*>(.*?)</p>', chunk, re.I | re.S)
            href_match = re.search(r'class=["\']card__cover-link["\'][^>]*href=["\']([^"\']+)', chunk, re.I)
            date_text = html.unescape(re.sub(r"<[^>]+>", " ", date_match.group(1))) if date_match else ""
            date_text = re.sub(r"\s+", " ", date_text).strip()
            summary = html.unescape(re.sub(r"<[^>]+>", " ", summary_match.group(1))) if summary_match else ""
            summary = re.sub(r"\s+", " ", summary).strip()
            key = (title, date_text)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "title": title,
                    "date": date_text,
                    "summary": summary,
                    # Kept only for auditability. It is intentionally not fetched
                    # or cited because many official cards point to a bank site.
                    "external_detail": html.unescape(href_match.group(1)) if href_match else "",
                }
            )

        included = 0
        for record in records[: task.max_links]:
            dates = re.search(
                r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s*-\s*(20\d{2})[/-](\d{1,2})[/-](\d{1,2})",
                record["date"],
            )
            start_date = ""
            end_date = ""
            if dates:
                candidate_start = f"{int(dates.group(1)):04d}-{int(dates.group(2)):02d}-{int(dates.group(3)):02d}"
                candidate_end = f"{int(dates.group(4)):04d}-{int(dates.group(5)):02d}-{int(dates.group(6)):02d}"
                try:
                    date.fromisoformat(candidate_start)
                    parsed_end = date.fromisoformat(candidate_end)
                except ValueError:
                    parsed_end = None
                if parsed_end and parsed_end < self.now.date():
                    continue
                if parsed_end:
                    start_date = candidate_start
                    end_date = candidate_end
            title = record["title"]
            identifier = hashlib.sha256(f"{title}\n{record['date']}".encode("utf-8")).hexdigest()[:16]
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            document = Document(
                url=task.fetch_url,
                title=title,
                text="\n".join(
                    value
                    for value in (
                        f"title: {title}",
                        f"活動期間：{record['date']}" if record["date"] else "",
                        f"活動摘要：{record['summary']}" if record["summary"] else "",
                    )
                    if value
                ),
                links=[],
                content_hash=hashlib.sha256(raw).hexdigest(),
                raw_json=record,
            )
            activity_task = Task(
                provider=task.provider,
                source_name=f"{task.source_name} → {title[:80]}",
                role="activity_detail",
                fetch_url=task.fetch_url,
                # Keep synthesized cards unique after canonical_url removes
                # fragments. The monitor_card query is stable, remains on the
                # official listing, and does not turn the external bank href
                # into evidence.
                display_url=(
                    f"{task.display_url}{'&' if '?' in task.display_url else '?'}"
                    f"monitor_card={identifier}#04"
                ),
                title_hint=title,
                adapter="famipay_official_card",
                external_id=f"famipay-card-{identifier}",
                start_date=start_date,
                end_date=end_date,
            )
            self.activity_documents.append(CollectedDocument(activity_task, document, collected.fetched_at))
            included += 1
        self._set_discovery_result(task, included, total_count=len(records))

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
        if task.adapter == "easywallet_listing":
            return self._easywallet_tasks(collected)
        if task.adapter == "ipass_listing":
            return self._ipass_tasks(collected)
        if task.adapter == "jkopay_listing":
            return self._jkopay_tasks(collected)
        if task.adapter == "pi_wordpress_listing":
            return self._pi_post_tasks(collected)
        if task.adapter == "pxmart_campaign_listing":
            return self._pxmart_campaign_tasks(collected)
        if task.adapter == "pxmart_quota_periods":
            return self._pxmart_quota_tasks(collected)
        if task.adapter == "pxmart_quota_detail":
            self._collect_pxmart_quota(collected)
            return []
        if task.adapter == "famipay_listing":
            self._collect_famipay_listing(collected)
            return []
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
            if task.role in {"activity_detail", "activity_probe", "status_page", "mixed_detail"}:
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
            discovery_state_updates=self._fullpay_discovery_state_updates(),
            discovery_scan_summary=self._fullpay_discovery_scan_summary(),
        )

    def _activity_from_document(self, collected: CollectedDocument) -> Activity:
        task = collected.task
        document = collected.document
        date_range = parse_date_range(document.text)
        title_date_range = parse_date_range(f"活動期間：{task.title_hint or document.title}")
        if (
            task.adapter == "jkopay_detail"
            and title_date_range.start
            and title_date_range.end
        ):
            # JkoPay detail bodies contain coupon redemption deadlines and
            # general terms that can extend beyond the advertised campaign.
            # The official page title states the campaign's own date range.
            date_range = DateRange(
                title_date_range.start,
                title_date_range.end,
                "high",
                title_date_range.excerpt,
            )
        elif date_range.confidence in {"none", "low"} and title_date_range.start and title_date_range.end:
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
        quota_evidence_excerpt = quota.evidence_excerpt
        quota_components = quota.components
        pxmart_quota_has_unknown = (
            task.adapter == "pxmart_quota_activity"
            and isinstance(public_quota_status, dict)
            and int(public_quota_status.get("unknown_count", 0)) > 0
        )
        if task.adapter == "pxmart_campaign_detail" and quota_status == "unknown_app_only":
            # The shared PX Pay/Fullpay campaign page contains a Fullpay-only
            # sentence that points to the Fullpay App. It must not override the
            # PX Pay status supplied by the separate official cardpoint API.
            quota_status = "not_marked_full"
            quota_evidence_complete = True
            quota_evidence_excerpt = ""
            quota_components = []
        if (
            quota_status == "not_marked_full"
            and isinstance(public_quota_status, dict)
            and public_quota_status.get("unavailable") is True
        ):
            quota_status = "unknown_source_failure"
            quota_evidence_complete = False
        elif (
            quota_status == "not_marked_full"
            and task.adapter == "pxmart_quota_activity"
            and isinstance(public_quota_status, dict)
            and int(public_quota_status.get("available_count", 0)) > 0
            and int(public_quota_status.get("sold_out_count", 0)) == 0
            and int(public_quota_status.get("unknown_count", 0)) == 0
        ):
            quota_status = "confirmed_available"
            quota_evidence_complete = True
        if pxmart_quota_has_unknown:
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
            "redirecting...",
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
        if quota_evidence_excerpt:
            quota_api_notice = (
                str(public_quota_status.get("notice", ""))
                if isinstance(public_quota_status, dict)
                else ""
            )
            from_quota_api = bool(quota_api_notice and quota_api_notice in quota_evidence_excerpt)
            evidence.append(
                Evidence(
                    source_url=(
                        str(public_quota_status.get("source_url", task.display_url))
                        if from_quota_api
                        else task.display_url
                    ),
                    excerpt=quota_evidence_excerpt,
                    observed_at=collected.fetched_at,
                    kind="activity_status_api" if from_quota_api else "activity_page",
                    content_hash=document.content_hash,
                )
            )
        available_notice = (
            str(public_quota_status.get("available_notice", ""))
            if isinstance(public_quota_status, dict)
            else ""
        )
        if available_notice:
            evidence.append(
                Evidence(
                    source_url=str(public_quota_status.get("source_url", task.display_url)),
                    excerpt=available_notice[:500],
                    observed_at=collected.fetched_at,
                    kind="activity_status_api",
                    content_hash=document.content_hash,
                )
            )
        review_required = (
            date_range.confidence in {"none", "low"}
            or lifecycle == "unknown"
            or "monitor_review_required: true" in document.text
            or quota_status == "unknown_source_failure"
            or pxmart_quota_has_unknown
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
            components=quota_components,
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
