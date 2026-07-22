from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path

from .dates import parse_now
from .discovery import Crawler
from .report import build_payload, write_reports
from .storage import Store


ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Monitor official Taiwan payment promotion pages")
    result.add_argument("--mode", choices=("full", "status"), default="full")
    result.add_argument("--config", type=Path, default=ROOT / "config" / "sources.json")
    result.add_argument("--db", type=Path, default=ROOT / "data" / "monitor.sqlite3")
    result.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    result.add_argument("--now", help="ISO timestamp, mainly for repeatable validation")
    result.add_argument("--timeout", type=float, default=20.0)
    return result


def _load_config(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("schema_version") != 1 or not isinstance(config.get("providers"), list):
        raise ValueError("Unsupported or invalid source registry")
    return config


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = _load_config(args.config)
    now = parse_now(args.now, str(config.get("timezone", "Asia/Taipei")))
    args.db.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.db.with_suffix(args.db.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "another run holds the lock"}, ensure_ascii=False))
            return 3

        with Store(args.db) as store:
            previous_statuses = store.status_snapshot()
            mode = args.mode
            fallback = False
            if mode == "status" and not store.has_activities():
                mode = "full"
                fallback = True
            targets = store.load_recheck_targets() if mode == "status" else []
            crawler = Crawler(config, now, timeout=args.timeout)
            run = crawler.collect(mode, targets)
            store.merge_persistent_status(run.activities)
            changes = []
            for activity in run.activities:
                activity_id = store.activity_id(activity)
                previous = previous_statuses.get(activity_id)
                if previous and previous["quota_status"] != activity.quota_status:
                    changes.append(
                        {
                            "provider_id": activity.provider_id,
                            "provider_name": activity.provider_name,
                            "title": activity.title,
                            "url": activity.url,
                            "from": previous["quota_status"],
                            "to": activity.quota_status,
                            "observed_at": activity.fetched_at,
                        }
                    )
            store.save_run(run)
            activities = store.load_current()
            payload = build_payload(run, activities, now, config, changes)
            if fallback:
                payload["run"]["requested_mode"] = "status"
                payload["run"]["fallback_reason"] = "資料庫尚無活動，先執行完整掃描"
            json_path, markdown_path = write_reports(payload, args.output_dir, mode)

    print(
        json.dumps(
            {
                "status": payload["run"]["coverage"]["status"],
                "mode": mode,
                "coverage": payload["run"]["coverage"],
                "summary": payload["summary"],
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    # A run where every official request failed must not look successful to a
    # scheduler, otherwise a stale database can be rebuilt and published as if
    # it had just been refreshed.
    if payload["run"]["coverage"]["transport_status"] == "unavailable":
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
