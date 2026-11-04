# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

"""Generate shields.io endpoint badges from data/prs.json.

Output:
    badges/overall.json        all open STM32 PRs
    badges/area/<slug>.json    one per area
    badges/index.json          area label -> slug, read by the page

Badge URLs are pasted in live PR comments, so badge files are never deleted.
An area that has no open PR any more renders "0 open"; it never returns 404.
Two different area labels mapping to the same slug fail the run.

Usage:
    python -m collector.badges [--prs data/prs.json] [--badges badges]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from collector.aggregate import percentile
from collector.fetch import parse_ts

CACHE_SECONDS = 300
# Upper bounds (days) of the median age, from the best colour to the worst.
COLOR_STEPS = ((14, "brightgreen"), (30, "green"), (60, "yellow"), (90, "orange"))
WORST_COLOR = "red"


class SlugCollisionError(RuntimeError):
    """Raised when two area labels would share one badge file."""


def slugify(area: str) -> str:
    """`Bluetooth Host` -> `bluetooth-host`, `Boards/SoCs` -> `boards-socs`."""
    slug = re.sub(r"[^a-z0-9]+", "-", area.lower()).strip("-")
    if not slug:
        raise ValueError(f"area label {area!r} has no usable slug")
    return slug


def color_for(median_age: float | None) -> str:
    if median_age is None:
        return "brightgreen"
    for limit, color in COLOR_STEPS:
        if median_age <= limit:
            return color
    return WORST_COLOR


def payload(label: str, ages: list[float]) -> dict[str, Any]:
    ages = sorted(ages)
    median = percentile(ages, 0.5)
    message = f"{len(ages)} open"
    if median is not None:
        message += f", median {round(median)} d"
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color_for(median),
        "cacheSeconds": CACHE_SECONDS,
    }


def area_slugs(areas: set[str]) -> dict[str, str]:
    slugs: dict[str, str] = {}
    owner: dict[str, str] = {}
    for area in sorted(areas):
        slug = slugify(area)
        if slug in owner:
            raise SlugCollisionError(f"{owner[slug]!r} and {area!r} both map to {slug!r}")
        owner[slug] = area
        slugs[area] = slug
    return slugs


def build_badges(corpus: dict[str, Any], existing_labels: dict[str, str]) -> dict[str, Any]:
    """Return {relative path: JSON document} for every badge file to write.

    `existing_labels` maps slug -> label of badge files already on disk, so an
    area that vanished from the corpus is still rendered as "0 open".
    """
    as_of = parse_ts(corpus["generated_at"])
    prs = corpus["prs"]
    slugs = area_slugs({area for pr in prs for area in pr["areas"]})
    labels = {slug: area for area, slug in slugs.items()}
    for slug, label in existing_labels.items():
        if slug not in labels:
            labels[slug] = label
            slugs[label] = slug

    ages_by_slug: dict[str, list[float]] = {slug: [] for slug in labels}
    overall: list[float] = []
    for pr in prs:
        if pr["state"] != "open":
            continue
        age = (as_of - parse_ts(pr["labeled_at"])).total_seconds() / 86400
        overall.append(age)
        for area in pr["areas"]:
            ages_by_slug[slugs[area]].append(age)

    files: dict[str, Any] = {"overall.json": payload("STM32 PRs", overall)}
    for slug, label in labels.items():
        files[f"area/{slug}.json"] = payload(label, ages_by_slug[slug])
    files["index.json"] = {"schema": 1, "areas": dict(sorted(slugs.items()))}
    return files


def read_existing_labels(badges_dir: Path) -> dict[str, str]:
    labels = {}
    for path in sorted((badges_dir / "area").glob("*.json")):
        labels[path.stem] = json.loads(path.read_text(encoding="utf-8"))["label"]
    return labels


def write_if_changed(path: Path, doc: Any) -> bool:
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".badge-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True


def update_badges(corpus: dict[str, Any], badges_dir: Path) -> list[str]:
    """Write the badge files. Returns the relative paths that changed."""
    files = build_badges(corpus, read_existing_labels(badges_dir))
    return [rel for rel, doc in files.items() if write_if_changed(badges_dir / rel, doc)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prs", type=Path, default=Path("data/prs.json"))
    parser.add_argument("--badges", type=Path, default=Path("badges"))
    args = parser.parse_args(argv)

    corpus = json.loads(args.prs.read_text(encoding="utf-8"))
    changed = update_badges(corpus, args.badges)
    print(f"{len(changed)} badge file(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
