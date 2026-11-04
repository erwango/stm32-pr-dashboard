# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from collector import aggregate


def pr(number, areas, labeled, closed=None, merged=None, review=None, decision=None, activity=None):
    return {
        "number": number,
        "areas": areas,
        "labeled_at": labeled,
        "closed_at": closed,
        "merged_at": merged,
        "first_review_at": review,
        "review_decision": decision,
        "last_activity_at": activity or labeled,
    }


PRS = [
    # Spans the January/February boundary, merged on Feb 2.
    pr(1, ["SPI"], "2026-01-30T10:00:00Z", "2026-02-02T09:00:00Z", "2026-02-02T09:00:00Z"),
    # Still open, reviewed on Feb 2, changes requested.
    pr(
        2,
        ["SPI", "I2C"],
        "2026-02-01T12:00:00Z",
        review="2026-02-02T08:00:00Z",
        decision="CHANGES_REQUESTED",
        activity="2026-02-02T08:00:00Z",
    ),
    # Closed unmerged exactly at midnight: still open at the end of Jan 31.
    pr(3, ["I2C"], "2026-01-31T08:00:00Z", "2026-02-01T00:00:00Z"),
    # Old and silent: still open, never reviewed, stale.
    pr(4, ["Unclassified"], "2025-12-01T00:00:00Z", activity="2025-12-20T00:00:00Z"),
]


def corpus(generated_at="2026-02-04T06:00:00Z"):
    return {"schema": 1, "generated_at": generated_at, "watermark": generated_at, "prs": PRS}


def rows_by_date(rows):
    return {row["date"]: row for row in rows}


class PercentileTest(unittest.TestCase):
    def test_percentile(self):
        self.assertIsNone(aggregate.percentile([], 0.5))
        self.assertEqual(aggregate.percentile([4.0], 0.9), 4.0)
        self.assertEqual(aggregate.percentile([1.0, 3.0], 0.5), 2.0)
        self.assertAlmostEqual(aggregate.percentile([0.0, 10.0, 20.0], 0.9), 18.0)


class DayRowsTest(unittest.TestCase):
    def setUp(self):
        rows = aggregate.aggregate_days(
            PRS, date(2026, 1, 31), date(2026, 2, 3), snapshot_day=date(2026, 2, 3)
        )
        self.rows = rows_by_date(rows)

    def test_month_boundary_and_midnight_close(self):
        row = self.rows["2026-01-31"]
        self.assertEqual(row["open_total"], 3)
        self.assertEqual(row["open_by_area"], {"I2C": 1, "SPI": 1, "Unclassified": 1})
        self.assertEqual((row["opened"], row["merged"], row["closed"]), (1, 0, 0))
        self.assertEqual(row["awaiting_review"], 3)

    def test_events_on_day(self):
        row = self.rows["2026-02-01"]
        self.assertEqual(row["open_total"], 3)
        self.assertEqual((row["opened"], row["merged"], row["closed"]), (1, 0, 1))
        row = self.rows["2026-02-02"]
        self.assertEqual(row["open_total"], 2)
        self.assertEqual((row["opened"], row["merged"], row["closed"]), (0, 1, 0))
        self.assertEqual(row["awaiting_review"], 1)

    def test_multi_area_pr_counts_in_each_area(self):
        row = self.rows["2026-02-01"]
        self.assertEqual(row["open_by_area"], {"I2C": 1, "SPI": 2, "Unclassified": 1})

    def test_ages(self):
        row = self.rows["2026-02-03"]
        self.assertAlmostEqual(row["age_max"], 65.0, delta=0.05)
        self.assertAlmostEqual(row["age_p50"], 33.75, delta=0.05)
        self.assertAlmostEqual(row["age_p90"], 58.75, delta=0.05)

    def test_snapshot_fields_only_on_snapshot_day(self):
        for date_, row in self.rows.items():
            if date_ != "2026-02-03":
                for field in aggregate.SNAPSHOT_FIELDS:
                    self.assertIsNone(row[field], (date_, field))
        row = self.rows["2026-02-03"]
        self.assertEqual(row["changes_requested"], 1)
        self.assertEqual(row["approved_unmerged"], 0)
        self.assertEqual(row["stale_30d"], 1)

    def test_empty_day(self):
        row = aggregate.aggregate_days([], date(2026, 1, 1), date(2026, 1, 1))[0]
        self.assertEqual(row["open_total"], 0)
        self.assertEqual(row["open_by_area"], {})
        self.assertIsNone(row["age_p50"])
        self.assertIsNone(row["age_max"])


class UpdateHistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.history = Path(tmp.name) / "history"

    def read(self, month):
        return json.loads((self.history / f"{month}.json").read_text())["days"]

    def test_backfill_splits_months_and_skips_incomplete_day(self):
        added = aggregate.update_history(corpus(), self.history, days=20)
        self.assertEqual(added[0], "2026-01-15")
        self.assertEqual(added[-1], "2026-02-03")  # Feb 4 is not complete yet
        self.assertEqual(len(added), 20)
        self.assertEqual(len(self.read("2026-01")), 17)
        self.assertEqual(len(self.read("2026-02")), 3)
        self.assertEqual(
            sorted(p.name for p in self.history.iterdir()), ["2026-01.json", "2026-02.json"]
        )

    def test_idempotent(self):
        aggregate.update_history(corpus(), self.history, days=20)
        before = {p.name: p.read_text() for p in self.history.iterdir()}
        self.assertEqual(aggregate.update_history(corpus(), self.history, days=20), [])
        self.assertEqual({p.name: p.read_text() for p in self.history.iterdir()}, before)

    def test_append_only(self):
        aggregate.update_history(corpus(), self.history, days=20)
        february = self.read("2026-02")
        # Same corpus later, with PR 2 now merged: past days must not move.
        later = corpus("2026-02-06T06:00:00Z")
        later["prs"] = [dict(p) for p in PRS]
        later["prs"][1].update(closed_at="2026-02-04T10:00:00Z", merged_at="2026-02-04T10:00:00Z")
        added = aggregate.update_history(later, self.history)
        self.assertEqual(added, ["2026-02-04", "2026-02-05"])
        updated = self.read("2026-02")
        self.assertEqual(updated[: len(february)], february)
        rows = rows_by_date(updated)
        self.assertEqual(rows["2026-02-04"]["merged"], 1)
        self.assertIsNone(rows["2026-02-04"]["stale_30d"])
        self.assertEqual(rows["2026-02-05"]["stale_30d"], 1)

    def test_stale_corpus_adds_nothing(self):
        aggregate.update_history(corpus(), self.history, days=20)
        self.assertEqual(aggregate.update_history(corpus("2026-02-04T23:00:00Z"), self.history), [])


if __name__ == "__main__":
    unittest.main()
