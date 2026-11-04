# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

import json
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from collector import fetch


def node(number, state="OPEN", labels=("platform: STM32", "area: SPI"), **over):
    base = {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/zephyrproject-rtos/zephyr/pull/{number}",
        "state": state,
        "createdAt": "2026-01-01T00:00:00Z",
        "closedAt": None,
        "mergedAt": None,
        "updatedAt": "2026-01-10T00:00:00Z",
        "reviewDecision": None,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "timelineItems": {
            "nodes": [
                {"createdAt": "2026-01-05T00:00:00Z", "label": {"name": "platform: STM32"}},
                {"createdAt": "2026-01-03T00:00:00Z", "label": {"name": "platform: STM32"}},
                {"createdAt": "2026-01-02T00:00:00Z", "label": {"name": "area: SPI"}},
            ]
        },
        "reviews": {
            "nodes": [
                {"state": "PENDING", "submittedAt": None},
                {"state": "APPROVED", "submittedAt": "2026-01-08T00:00:00Z"},
                {"state": "COMMENTED", "submittedAt": "2026-01-06T00:00:00Z"},
            ]
        },
    }
    base.update(over)
    return base


class FakeClient:
    def __init__(self, listed, nodes):
        self.listed = listed
        self.nodes = {n["number"]: n for n in nodes}
        self.since = "unset"
        self.calls = []
        self.queries = []

    def rest_issues(self, repo, since=None, state="all"):
        self.since = since
        self.calls.append((since, state))
        return iter({"number": n, "pull_request": {}} for n in self.listed)

    def graphql(self, query):
        self.queries.append(query)
        numbers = [int(x) for x in re.findall(r"pr(\d+):", query)]
        return {"repository": {f"pr{n}": self.nodes.get(n) for n in numbers}}


class ToRecordTest(unittest.TestCase):
    def test_fields(self):
        rec = fetch.to_record(node(7, labels=("platform: STM32", "area: SPI", "area: Boards/SoCs")))
        self.assertEqual(rec["state"], "open")
        self.assertEqual(rec["areas"], ["Boards/SoCs", "SPI"])
        self.assertEqual(rec["labeled_at"], "2026-01-03T00:00:00Z")
        self.assertEqual(rec["first_review_at"], "2026-01-06T00:00:00Z")
        self.assertEqual(rec["last_activity_at"], "2026-01-10T00:00:00Z")

    def test_unclassified_and_merged(self):
        rec = fetch.to_record(node(8, state="MERGED", labels=("platform: STM32",)))
        self.assertEqual(rec["areas"], [fetch.UNCLASSIFIED])
        self.assertEqual(rec["state"], "merged")

    def test_missing_label_event_falls_back_to_created(self):
        n = node(9)
        n["timelineItems"]["nodes"] = []
        n["reviews"]["nodes"] = []
        rec = fetch.to_record(n)
        self.assertEqual(rec["labeled_at"], n["createdAt"])
        self.assertIsNone(rec["first_review_at"])


class SelectMergeTest(unittest.TestCase):
    def test_select(self):
        existing = {
            1: {"state": "open"},
            2: {"state": "merged"},
            3: {"state": "closed"},
        }
        self.assertEqual(fetch.select_to_hydrate([2, 3, 4], existing), [1, 4])

    def test_closed_records_are_immutable(self):
        old = {1: {"number": 1, "state": "merged", "title": "old"}}
        fresh = {1: {"number": 1, "state": "merged", "title": "new"}}
        self.assertEqual(fetch.merge_records(old, fresh)[0]["title"], "old")

    def test_open_record_replaced_and_sorted(self):
        old = {5: {"number": 5, "state": "open", "title": "old"}}
        fresh = {
            5: {"number": 5, "state": "merged", "title": "new"},
            2: {"number": 2, "state": "open"},
        }
        merged = fetch.merge_records(old, fresh)
        self.assertEqual([r["number"] for r in merged], [2, 5])
        self.assertEqual(merged[1]["state"], "merged")


class CollectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "data" / "prs.json"
        self.now = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    def test_backfill_then_incremental(self):
        client = FakeClient([1, 2], [node(1), node(2)])
        doc = fetch.collect(client, self.output, now=self.now)
        # Backfill is bounded to the window, plus every open PR however idle.
        self.assertEqual(
            client.calls,
            [(self.now - fetch.BACKFILL_WINDOW, "all"), (None, "open")],
        )
        self.assertEqual([r["number"] for r in doc["prs"]], [1, 2])
        self.assertEqual(doc["watermark"], "2026-02-01T11:00:00Z")
        self.assertEqual(json.loads(self.output.read_text())["prs"], doc["prs"])

        # PR 1 merges, PR 3 appears; PR 2 stays open and is refreshed anyway.
        client = FakeClient([1, 3], [node(1, state="MERGED"), node(2), node(3)])
        doc = fetch.collect(client, self.output, now=self.now)
        self.assertEqual(client.since, datetime(2026, 2, 1, 11, 0, tzinfo=UTC))
        states = {r["number"]: r["state"] for r in doc["prs"]}
        self.assertEqual(states, {1: "merged", 2: "open", 3: "open"})

        # Closed PR 1 is no longer fetched.
        client = FakeClient([1], [node(1, state="CLOSED")])
        fetch.collect(client, self.output, now=self.now)
        fetched = {int(x) for q in client.queries for x in re.findall(r"pr(\d+):", q)}
        self.assertNotIn(1, fetched)

    def test_dry_run_writes_nothing(self):
        fetch.collect(FakeClient([1], [node(1)]), self.output, dry_run=True, now=self.now)
        self.assertFalse(self.output.exists())

    def test_batches(self):
        numbers = list(range(1, fetch.BATCH_SIZE + 6))
        client = FakeClient(numbers, [node(n) for n in numbers])
        doc = fetch.collect(client, self.output, now=self.now)
        self.assertEqual(len(client.queries), 2)
        self.assertEqual(len(doc["prs"]), len(numbers))

    def test_shrink_guard_keeps_file(self):
        fetch.collect(FakeClient([1, 2], [node(1), node(2)]), self.output, now=self.now)
        before = self.output.read_text()
        doc = {"schema": 1, "generated_at": "x", "watermark": "x", "prs": [{"number": 1}]}
        with self.assertRaises(fetch.CorpusShrinkError):
            fetch.write_corpus(self.output, doc)
        self.assertEqual(self.output.read_text(), before)
        self.assertEqual([p.name for p in self.output.parent.iterdir()], ["prs.json"])

    def test_failed_fetch_leaves_file_untouched(self):
        fetch.collect(FakeClient([1], [node(1)]), self.output, now=self.now)
        before = self.output.read_text()

        class Broken(FakeClient):
            def graphql(self, query):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            fetch.collect(Broken([1], []), self.output, now=self.now)
        self.assertEqual(self.output.read_text(), before)


if __name__ == "__main__":
    unittest.main()
