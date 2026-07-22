from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .fetch import canonical_url
from .models import Activity, RunResult


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    external_id TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    lifecycle TEXT NOT NULL,
    quota_status TEXT NOT NULL,
    quota_evidence_complete INTEGER NOT NULL,
    review_required INTEGER NOT NULL,
    date_confidence TEXT NOT NULL,
    conditions_summary TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_lifecycle ON activities(lifecycle);
CREATE INDEX IF NOT EXISTS idx_activities_provider ON activities(provider_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    run_status TEXT NOT NULL,
    expected_sources INTEGER NOT NULL,
    succeeded_sources INTEGER NOT NULL,
    failed_sources INTEGER NOT NULL,
    activity_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_attempts (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    source_name TEXT NOT NULL,
    role TEXT NOT NULL,
    url TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status_code INTEGER,
    final_url TEXT,
    error TEXT,
    fetched_at TEXT NOT NULL,
    discovered_count INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def activity_id(activity: Activity) -> str:
        identity = f"id:{activity.external_id}" if activity.external_id else canonical_url(activity.url)
        value = f"{activity.provider_id}\0{identity}".encode()
        return hashlib.sha256(value).hexdigest()[:24]

    def merge_persistent_status(self, activities: list[Activity]) -> None:
        """Keep confirmed quota evidence until an explicit reopening is observed.

        Latest-news pages are finite lists, so an older sold-out announcement can
        disappear even though the promotion did not reopen.  Absence in a later
        crawl is therefore not evidence for downgrading a confirmed state.
        """
        rank = {"partial_sold_out": 1, "sold_out": 2}
        for activity in activities:
            row = self.connection.execute(
                "SELECT payload_json FROM activities WHERE activity_id = ?",
                (self.activity_id(activity),),
            ).fetchone()
            if not row:
                continue
            previous = activity_from_dict(json.loads(row["payload_json"]))
            previous_rank = rank.get(previous.quota_status, 0)
            if previous_rank and activity.quota_status in {
                "not_marked_full",
                "unknown_app_only",
                "unknown_source_failure",
            }:
                activity.quota_status = previous.quota_status
                activity.quota_evidence_complete = previous.quota_evidence_complete
            elif previous.quota_status == "unknown_app_only" and activity.quota_status == "not_marked_full":
                activity.quota_status = "unknown_app_only"
                activity.quota_evidence_complete = False

            evidence_keys = {
                (item.source_url, item.excerpt)
                for item in activity.evidence
            }
            for item in previous.evidence:
                key = (item.source_url, item.excerpt)
                if key not in evidence_keys:
                    activity.evidence.insert(0, item)
                    evidence_keys.add(key)
            for component in previous.components:
                if component not in activity.components:
                    activity.components.insert(0, component)

    def upsert_activity(self, activity: Activity) -> None:
        activity_id = self.activity_id(activity)
        payload = json.dumps(activity.to_dict(), ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            """
            INSERT INTO activities (
                activity_id, provider_id, provider_name, external_id, title, url, source_url,
                start_date, end_date, lifecycle, quota_status, quota_evidence_complete,
                review_required, date_confidence, conditions_summary, fetched_at, content_hash,
                payload_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                provider_name=excluded.provider_name,
                external_id=COALESCE(excluded.external_id, activities.external_id),
                title=excluded.title,
                url=excluded.url,
                source_url=excluded.source_url,
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                lifecycle=excluded.lifecycle,
                quota_status=excluded.quota_status,
                quota_evidence_complete=excluded.quota_evidence_complete,
                review_required=excluded.review_required,
                date_confidence=excluded.date_confidence,
                conditions_summary=excluded.conditions_summary,
                fetched_at=excluded.fetched_at,
                content_hash=excluded.content_hash,
                payload_json=excluded.payload_json,
                last_seen_at=excluded.last_seen_at
            """,
            (
                activity_id,
                activity.provider_id,
                activity.provider_name,
                activity.external_id,
                activity.title,
                canonical_url(activity.url),
                activity.source_url,
                activity.start_date,
                activity.end_date,
                activity.lifecycle,
                activity.quota_status,
                int(activity.quota_evidence_complete),
                int(activity.review_required),
                activity.date_confidence,
                activity.conditions_summary,
                activity.fetched_at,
                activity.content_hash,
                payload,
                activity.fetched_at,
                activity.fetched_at,
            ),
        )

    def save_run(self, run: RunResult) -> None:
        coverage = run.coverage
        with self.connection:
            for activity in run.activities:
                self.upsert_activity(activity)
            self.connection.execute(
                """
                INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.mode,
                    run.started_at,
                    run.finished_at,
                    coverage["status"],
                    coverage["expected"],
                    coverage["succeeded"],
                    coverage["failed"],
                    len(run.activities),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO source_attempts (
                    run_id, provider_id, provider_name, source_name, role, url, ok,
                    status_code, final_url, error, fetched_at, discovered_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run.run_id,
                        item.provider_id,
                        item.provider_name,
                        item.source_name,
                        item.role,
                        item.url,
                        int(item.ok),
                        item.status_code,
                        item.final_url,
                        item.error,
                        item.fetched_at,
                        item.discovered_count,
                    )
                    for item in run.attempts
                ],
            )

    def load_current(self) -> list[Activity]:
        rows = self.connection.execute("SELECT payload_json FROM activities ORDER BY provider_name, title").fetchall()
        return [activity_from_dict(json.loads(row["payload_json"])) for row in rows]

    def load_recheck_targets(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT provider_id, provider_name, external_id, url, source_url, start_date, end_date
            FROM activities
            WHERE lifecycle IN ('active', 'upcoming', 'unknown')
            ORDER BY provider_id, url
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def status_snapshot(self) -> dict[str, dict[str, str]]:
        rows = self.connection.execute(
            "SELECT activity_id, quota_status, lifecycle, title, url FROM activities"
        ).fetchall()
        return {row["activity_id"]: dict(row) for row in rows}

    def has_activities(self) -> bool:
        row = self.connection.execute("SELECT EXISTS(SELECT 1 FROM activities LIMIT 1)").fetchone()
        return bool(row[0])


def activity_from_dict(data: dict[str, Any]) -> Activity:
    from .models import Evidence

    evidence = [Evidence(**item) for item in data.pop("evidence", [])]
    return Activity(**data, evidence=evidence)
