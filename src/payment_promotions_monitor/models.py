from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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

    @property
    def coverage(self) -> dict[str, Any]:
        expected = len(self.attempts)
        succeeded = sum(1 for item in self.attempts if item.ok)
        failed = expected - succeeded
        discovery_issues = sum(1 for item in self.attempts if item.coverage_issue)
        return {
            "expected": expected,
            "succeeded": succeeded,
            "failed": failed,
            "rate": round(succeeded / expected, 4) if expected else 1.0,
            "transport_status": "complete" if failed == 0 else "partial",
            "discovery_issues": discovery_issues,
            "status": "complete" if failed == 0 and discovery_issues == 0 else "partial",
        }
