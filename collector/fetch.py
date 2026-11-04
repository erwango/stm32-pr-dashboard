# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

"""Fetch STM32 pull requests from zephyrproject-rtos/zephyr into data/prs.json.

The REST issues endpoint lists the PRs carrying the STM32 label (no 1000 result
cap, unlike the search API). A GraphQL pass then hydrates each PR with its
labeling event and review data.

Usage:
    python -m collector.fetch [--dry-run] [--output data/prs.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = "zephyrproject-rtos/zephyr"
LABEL = "platform: STM32"
AREA_PREFIX = "area: "
UNCLASSIFIED = "Unclassified"
SCHEMA_VERSION = 1
BATCH_SIZE = 10
MAX_ATTEMPTS = 4
# The next incremental run asks for changes since (start of this run - margin),
# so a PR updated while a run was in flight is never missed.
WATERMARK_MARGIN = timedelta(hours=1)
# The first run only reaches back this far (12 months of history plus a month of slack).
BACKFILL_WINDOW = timedelta(days=395)
API_URL = "https://api.github.com"

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class CorpusShrinkError(RuntimeError):
    """Raised when a write would reduce the number of records on disk."""


class GitHubClient:
    """Minimal GitHub REST and GraphQL client on top of urllib."""

    def __init__(self, token: str, api_url: str = API_URL) -> None:
        self._api_url = api_url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stm32-pr-dashboard",
        }

    def _request(self, url: str, body: bytes | None = None, raw: bool = False) -> tuple[Any, str]:
        headers = self._headers
        if raw:
            headers = {**headers, "Accept": "application/vnd.github.raw+json"}
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    content = response.read().decode() if raw else json.load(response)
                    return content, response.headers.get("Link", "")
            except urllib.error.HTTPError as err:
                retryable = err.code >= 500
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == MAX_ATTEMPTS - 1:
                    raise
            time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def rest_issues(
        self, repo: str, since: datetime | None = None, state: str = "all"
    ) -> Iterator[dict[str, Any]]:
        """Yield the pull requests carrying the STM32 label."""
        params = {"labels": LABEL, "state": state, "per_page": "100"}
        if since is None:
            params.update(sort="created", direction="asc")
        else:
            params.update(sort="updated", direction="asc", since=format_ts(since))
        url: str | None = f"{self._api_url}/repos/{repo}/issues?{urllib.parse.urlencode(params)}"
        while url:
            items, link = self._request(url)
            for item in items:
                if "pull_request" in item:
                    yield item
            match = _NEXT_LINK.search(link)
            url = match.group(1) if match else None

    def get_text(self, repo: str, path: str, ref: str) -> str:
        """Return the content of a file of `repo` at `ref`."""
        quoted = urllib.parse.quote(path)
        url = f"{self._api_url}/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}"
        text, _ = self._request(url, raw=True)
        return text

    def graphql(self, query: str) -> dict[str, Any]:
        body = json.dumps({"query": query}).encode()
        payload, _ = self._request(f"{self._api_url}/graphql", body)
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]


def format_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_PR_FIELDS = """
  number title url state createdAt closedAt mergedAt updatedAt reviewDecision
  labels(first: 50) { nodes { name } }
  timelineItems(itemTypes: [LABELED_EVENT], first: 100) {
    nodes { ... on LabeledEvent { createdAt label { name } } }
  }
  reviews(first: 1, states: [COMMENTED, APPROVED, CHANGES_REQUESTED, DISMISSED]) {
    nodes { state submittedAt }
  }
"""


def build_query(repo: str, numbers: list[int]) -> str:
    owner, name = repo.split("/")
    aliases = "\n".join(f"pr{n}: pullRequest(number: {int(n)}) {{{_PR_FIELDS}}}" for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{\n{aliases}\n}} }}'


def to_record(node: dict[str, Any]) -> dict[str, Any]:
    """Convert a GraphQL pull request node to a corpus record."""
    names = [label["name"] for label in node["labels"]["nodes"]]
    areas = sorted({n[len(AREA_PREFIX) :] for n in names if n.startswith(AREA_PREFIX)})
    label_times = [
        event["createdAt"]
        for event in node["timelineItems"]["nodes"]
        if event.get("label", {}).get("name") == LABEL
    ]
    review_times = [
        review["submittedAt"]
        for review in node["reviews"]["nodes"]
        if review["state"] != "PENDING" and review["submittedAt"]
    ]
    return {
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "state": node["state"].lower(),
        "created_at": node["createdAt"],
        "labeled_at": min(label_times) if label_times else node["createdAt"],
        "closed_at": node["closedAt"],
        "merged_at": node["mergedAt"],
        "areas": areas or [UNCLASSIFIED],
        "review_decision": node["reviewDecision"],
        "first_review_at": min(review_times) if review_times else None,
        "last_activity_at": node["updatedAt"],
    }


def fetch_nodes(client: GitHubClient, repo: str, batch: list[int]) -> dict[str, Any]:
    """Query a batch of PRs. A failing batch is bisected so a bad PR is isolated and named."""
    try:
        return client.graphql(build_query(repo, batch))["repository"]
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
        if len(batch) == 1:
            print(f"error: PR #{batch[0]} cannot be fetched: {err}", file=sys.stderr)
            raise
        print(f"warning: batch of {len(batch)} failed ({err}), splitting", file=sys.stderr)
        half = len(batch) // 2
        nodes = fetch_nodes(client, repo, batch[:half])
        nodes.update(fetch_nodes(client, repo, batch[half:]))
        return nodes


def hydrate(client: GitHubClient, repo: str, numbers: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Fetch full records for the given PR numbers, in batches."""
    pending = sorted(set(numbers))
    records: dict[int, dict[str, Any]] = {}
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        data = fetch_nodes(client, repo, batch)
        print(f"hydrated {start + len(batch)}/{len(pending)}", file=sys.stderr, flush=True)
        for number in batch:
            node = data.get(f"pr{number}")
            if node is None:
                print(f"warning: PR #{number} not returned by GraphQL, skipped", file=sys.stderr)
                continue
            records[number] = to_record(node)
    return records


def select_to_hydrate(listed: Iterable[int], existing: dict[int, dict[str, Any]]) -> list[int]:
    """Pick the PRs to (re)fetch: open ones, and listed ones not yet closed on disk.

    Records of closed or merged PRs are immutable once written. Open PRs in the
    corpus are always refreshed: a PR whose label was removed no longer shows in
    a label-filtered listing, but its record must still be updated.
    """
    wanted = {n for n in listed if existing.get(n, {}).get("state", "open") == "open"}
    wanted |= {n for n, record in existing.items() if record["state"] == "open"}
    return sorted(wanted)


def merge_records(
    existing: dict[int, dict[str, Any]], fresh: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = dict(existing)
    for number, record in fresh.items():
        if merged.get(number, {}).get("state", "open") == "open":
            merged[number] = record
    return [merged[number] for number in sorted(merged)]


def load_corpus(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_corpus(path: Path, doc: dict[str, Any]) -> None:
    """Atomically write the corpus, refusing to shrink it.

    The record count of the file on disk is the floor: a partial fetch that
    would drop records raises before anything is replaced.
    """
    previous = load_corpus(path)
    count = len(doc["prs"])
    if previous is not None and count < len(previous["prs"]):
        raise CorpusShrinkError(f"refusing to write {count} records over {len(previous['prs'])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".prs-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        if len(json.loads(tmp.read_text(encoding="utf-8"))["prs"]) != count:
            raise RuntimeError("temp file record count mismatch")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def collect(
    client: GitHubClient,
    output: Path,
    repo: str = REPO,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one collection cycle and return the resulting corpus document."""
    now = now or datetime.now(UTC)
    previous = load_corpus(output)
    existing = {r["number"]: r for r in previous["prs"]} if previous else {}
    since = parse_ts(previous["watermark"]) if previous else None

    if since is None:
        # Backfill. The label has ~6000 PRs over its lifetime; only the window matters.
        # A PR closed inside the window was updated inside it, so the `updated` filter
        # finds it. Open PRs may be idle for longer, so they are listed separately.
        cutoff = now - BACKFILL_WINDOW
        listed = [item["number"] for item in client.rest_issues(repo, cutoff)]
        listed += [item["number"] for item in client.rest_issues(repo, state="open")]
    else:
        listed = [item["number"] for item in client.rest_issues(repo, since)]
    listed = sorted(set(listed))
    print(f"listed {len(listed)} PRs", file=sys.stderr, flush=True)
    to_fetch = select_to_hydrate(listed, existing)
    fresh = hydrate(client, repo, to_fetch)

    doc = {
        "schema": SCHEMA_VERSION,
        "generated_at": format_ts(now),
        "watermark": format_ts(now - WATERMARK_MARGIN),
        "prs": merge_records(existing, fresh),
    }
    print(
        f"listed {len(listed)}, hydrated {len(fresh)}, "
        f"corpus {len(existing)} -> {len(doc['prs'])} records",
        file=sys.stderr,
    )
    if not dry_run:
        write_corpus(output, doc)
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path("data/prs.json"))
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--dry-run", action="store_true", help="fetch but do not write")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("error: set GITHUB_TOKEN (or GH_TOKEN)", file=sys.stderr)
        return 2
    collect(GitHubClient(token), args.output, repo=args.repo, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
