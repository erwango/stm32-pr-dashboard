# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

"""Aggregate data/prs.json into append-only daily history (data/history/YYYY-MM.json).

A day D covers [D 00:00, D+1 00:00) UTC and is only written once it is
complete according to the corpus' own `generated_at`, so a stale corpus never
freezes an incomplete day. A day already on disk is never recomputed.

Counting rules:
- open at the end of day D: labeled_at < D+1 and (closed_at is None or closed_at >= D+1);
- opened / merged / closed: events falling inside D; `closed` excludes merged PRs;
- age: from labeled_at to the end of D, in days;
- awaiting_review: open PRs with no review submitted before the end of D.

The corpus only stores the current review decision and last activity, so the
fields that need them (changes_requested, approved_unmerged, stale_30d) cannot
be rebuilt for the past. They are recorded for the newest day of a run only,
when the corpus describes the state at its end, and are null otherwise.

Usage:
    python -m collector.aggregate [--prs data/prs.json] [--history data/history] [--days 365]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from collector.fetch import parse_ts

SCHEMA_VERSION = 1
STALE_AFTER = timedelta(days=30)
SNAPSHOT_FIELDS = ("changes_requested", "approved_unmerged", "stale_30d")


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile of an ascending list, q in [0, 1]."""
    if not sorted_values:
        return None
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _opt(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


def _prepare(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "areas": pr["areas"],
            "labeled": parse_ts(pr["labeled_at"]),
            "closed": _opt(pr["closed_at"]),
            "merged": _opt(pr["merged_at"]),
            "first_review": _opt(pr["first_review_at"]),
            "activity": _opt(pr["last_activity_at"]),
            "decision": pr["review_decision"],
        }
        for pr in prs
    ]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def day_row(prs: list[dict[str, Any]], day: date, snapshot: bool) -> dict[str, Any]:
    """Compute one history row. `prs` comes from `_prepare`."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)

    open_prs = [
        p for p in prs if p["labeled"] < end and (p["closed"] is None or p["closed"] >= end)
    ]
    by_area: dict[str, int] = {}
    for pr in open_prs:
        for area in pr["areas"]:
            by_area[area] = by_area.get(area, 0) + 1

    ages = sorted((end - p["labeled"]).total_seconds() / 86400 for p in open_prs)
    reviewed = [p for p in open_prs if p["first_review"] is not None and p["first_review"] < end]

    row: dict[str, Any] = {
        "date": day.isoformat(),
        "open_total": len(open_prs),
        "open_by_area": dict(sorted(by_area.items())),
        "opened": sum(1 for p in prs if start <= p["labeled"] < end),
        "merged": sum(1 for p in prs if p["merged"] and start <= p["merged"] < end),
        "closed": sum(
            1 for p in prs if not p["merged"] and p["closed"] and start <= p["closed"] < end
        ),
        "age_p50": _rounded(percentile(ages, 0.5)),
        "age_p90": _rounded(percentile(ages, 0.9)),
        "age_max": _rounded(ages[-1] if ages else None),
        "awaiting_review": len(open_prs) - len(reviewed),
    }
    snapshot_values = dict.fromkeys(SNAPSHOT_FIELDS)
    if snapshot:
        snapshot_values = {
            "changes_requested": sum(1 for p in open_prs if p["decision"] == "CHANGES_REQUESTED"),
            "approved_unmerged": sum(1 for p in open_prs if p["decision"] == "APPROVED"),
            "stale_30d": sum(
                1 for p in open_prs if p["activity"] and end - p["activity"] > STALE_AFTER
            ),
        }
    row.update(snapshot_values)
    return row


def aggregate_days(
    prs: list[dict[str, Any]], start: date, end: date, snapshot_day: date | None = None
) -> list[dict[str, Any]]:
    """Rows for every day in [start, end], inclusive."""
    prepared = _prepare(prs)
    rows = []
    day = start
    while day <= end:
        rows.append(day_row(prepared, day, snapshot=day == snapshot_day))
        day += timedelta(days=1)
    return rows


def load_history(history_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Month key (YYYY-MM) -> rows already on disk."""
    months = {}
    for path in sorted(history_dir.glob("????-??.json")):
        months[path.stem] = json.loads(path.read_text(encoding="utf-8"))["days"]
    return months


def write_month(path: Path, days: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".history-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA_VERSION, "days": days}, handle, indent=1)
            handle.write("\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def update_history(corpus: dict[str, Any], history_dir: Path, days: int = 365) -> list[str]:
    """Append the missing complete days to the history. Returns the dates added."""
    as_of = parse_ts(corpus["generated_at"])
    last_complete = as_of.date() - timedelta(days=1)

    existing = load_history(history_dir)
    known = [row["date"] for rows in existing.values() for row in rows]
    if known:
        start = date.fromisoformat(max(known)) + timedelta(days=1)
    else:
        start = as_of.date() - timedelta(days=days)
    if start > last_complete:
        return []

    new_rows = aggregate_days(corpus["prs"], start, last_complete, snapshot_day=last_complete)
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in new_rows:
        by_month.setdefault(row["date"][:7], []).append(row)
    for month, rows in by_month.items():
        # Existing rows are kept untouched; the new ones are strictly later.
        write_month(history_dir / f"{month}.json", existing.get(month, []) + rows)
    return [row["date"] for row in new_rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prs", type=Path, default=Path("data/prs.json"))
    parser.add_argument("--history", type=Path, default=Path("data/history"))
    parser.add_argument("--days", type=int, default=365, help="backfill window when empty")
    args = parser.parse_args(argv)

    corpus = json.loads(args.prs.read_text(encoding="utf-8"))
    added = update_history(corpus, args.history, args.days)
    print(f"added {len(added)} day(s)" + (f": {added[0]}..{added[-1]}" if added else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
