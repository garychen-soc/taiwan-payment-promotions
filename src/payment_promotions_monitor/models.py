from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


_DNS_ERROR_MARKERS = (
    "nodename nor servname provided",
    "name or service not known",
    "temporary failure in name resolution",
    "could not resolve host",
    "no address associated with hostname",
)


@dataclass(slots=True)
class Evidence:
    source_url: str
    excerpt: str
    observed_at: str
    kind: str = "activity_page"
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Activity:
    provider_id: str
    provider_name: str
    title: str
    url: str
    source_url: str
    external_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    lifecycle: str = "unknown"
    quota_status: str = "not_marked_full"
    quota_evidence_complete: bool = True
    review_required: bool = False
    date_confidence: str = "none"
    conditions_summary: str = ""
    fetched_at: str = ""
    content_hash: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence"] = [item.to_dict() for item in self.evidence]
        return result


@dataclass(slots=True)
class SourceAttempt:
    provider_id: str
    provider_name: str
    source_name: str
    role: str
    url: str
    ok: bool
    fetched_at: str
    status_code: int | None = None
    final_url: str | None = None
    error: str | None = None
    discovered_count: int = 0
    coverage_issue: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunResult:
    run_id: str
    mode: str
    started_at: str
    finished_at: str
    activities: list[Activity]
    attempts: list[SourceAttempt]
    crawl_limit_pending: list[dict[str, Any]] = field(default_factory=list)
    discovery_state_updates: dict[str, dict[str, int]] = field(default_factory=dict)
    discovery_scan_summary: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def coverage(self) -> dict[str, Any]:
        expected = len(self.attempts)
        succeeded = sum(1 for item in self.attempts if item.ok)
        failed = expected - succeeded
        discovery_issues = sum(1 for item in self.attempts if item.coverage_issue) + len(self.crawl_limit_pending)
        unavailable = expected > 0 and succeeded == 0
        failed_errors = [(item.error or "").lower() for item in self.attempts if not item.ok]
        systemic_dns_failure = unavailable and bool(failed_errors) and all(
            any(marker in error for marker in _DNS_ERROR_MARKERS) for error in failed_errors
        )
        transport_status = "unavailable" if unavailable else ("complete" if failed == 0 else "partial")
        return {
            "expected": expected,
            "succeeded": succeeded,
            "failed": failed,
            "rate": round(succeeded / expected, 4) if expected else 1.0,
            "transport_status": transport_status,
            "systemic_dns_failure": systemic_dns_failure,
            "discovery_issues": discovery_issues,
            "status": (
                "unavailable"
                if unavailable
                else ("complete" if failed == 0 and discovery_issues == 0 else "partial")
            ),
        }
