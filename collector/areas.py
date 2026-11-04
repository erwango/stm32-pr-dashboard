# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

"""Work out which `area: *` labels denote hardware, into data/areas.json.

The dashboard counts PRs per area label, but only some labels name a piece of
hardware (a peripheral driver, or boards and SoCs); `Tests`, `Samples`,
`Documentation` or `native port` do not. Zephyr's MAINTAINERS.yml already ties
every `area: *` label to the files it covers, so the classification is derived
from it rather than kept as a hand-written list that would go stale.

A MAINTAINERS.yml area is a hardware area when:
  - its title starts with `Drivers:`, or
  - it owns a whole peripheral driver directory (`drivers/<name>/`, or
    `include/zephyr/drivers/<name>.h` or `.../<name>/`), or
  - it owns `boards/` and `soc/` as whole trees.

A label is hardware when at least half of the areas carrying it are hardware
areas: `area: Bluetooth` (1 hardware area out of 8) and `area: Networking`
(1 out of 16) stay out, `area: Ethernet` (1 out of 2) stays in. `West project:`
areas are third-party modules and are ignored.

Usage:
    python -m collector.areas [--source MAINTAINERS.yml] [--output data/areas.json]

Without --source the file is fetched from the Zephyr repository. This step is
auxiliary: on any failure the previous file is kept and the run still succeeds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from collector.fetch import AREA_PREFIX, REPO, GitHubClient

MAINTAINERS_PATH = "MAINTAINERS.yml"
MAINTAINERS_REF = "main"
SCHEMA_VERSION = 1
SOURCE = f"{REPO}/{MAINTAINERS_PATH}"
DRIVERS_PREFIX = "Drivers:"
MODULE_PREFIX = "West project:"
BOARD_TREES = {"boards/", "soc/"}
# A whole peripheral driver directory or header, not a glob or a single file.
_DRIVER_PATH = re.compile(r"^(?:drivers/[^/*]+/|include/zephyr/drivers/[^/*]+(?:/|\.h))$")
# A shrinking result larger than this fraction of the previous one is suspicious.
MIN_RETAINED = 0.5

_FIELD = re.compile(r"^  ([a-z][a-z-]*):(.*)$")
_ITEM = re.compile(r"^    - +(.*?)\s*$")


@dataclass
class Area:
    title: str
    labels: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _title(line: str) -> str:
    if line.startswith('"'):
        return line[1 : line.index('"', 1)]
    return line.rstrip().removesuffix(":")


def parse_maintainers(text: str) -> list[Area]:
    """Extract title, labels and files of every area.

    MAINTAINERS.yml is a flat mapping of area title to a few fields. Only the
    `labels` and `files` lists matter here, so a line scanner is enough and
    avoids a YAML dependency. People are not read.
    """
    areas: list[Area] = []
    current: Area | None = None
    active: list[str] | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if not line[0].isspace():
            current = Area(_title(line))
            areas.append(current)
            active = None
            continue
        if current is None:
            continue
        match = _FIELD.match(line)
        if match:
            key = match.group(1)
            active = {"labels": current.labels, "files": current.files}.get(key)
            continue
        match = _ITEM.match(line)
        if match and active is not None:
            active.append(_unquote(match.group(1)))
    return areas


def is_hardware_area(area: Area) -> bool:
    if area.title.startswith(DRIVERS_PREFIX):
        return True
    files = set(area.files)
    return BOARD_TREES <= files or any(_DRIVER_PATH.match(path) for path in files)


def hardware_labels(areas: list[Area]) -> list[str]:
    """Return the lowercase names of the hardware `area: *` labels, sorted.

    Matching is case-insensitive: GitHub treats label names that way and the
    label spelling in MAINTAINERS.yml differs from the live one in places.
    """
    carrying: dict[str, list[bool]] = {}
    for area in areas:
        if area.title.startswith(MODULE_PREFIX):
            continue
        hardware = is_hardware_area(area)
        for label in area.labels:
            if label.startswith(AREA_PREFIX):
                carrying.setdefault(label[len(AREA_PREFIX) :].lower(), []).append(hardware)
    return sorted(name for name, flags in carrying.items() if sum(flags) * 2 >= len(flags))


def load_previous(path: Path) -> list[str] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["hardware"]
    except (OSError, ValueError, KeyError):
        return None


def write_atomic(path: Path, labels: list[str]) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "source": SOURCE, "hardware": labels}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def update(text: str, output: Path) -> bool:
    """Classify `text` and write `output`; return False if the result was rejected."""
    labels = hardware_labels(parse_maintainers(text))
    previous = load_previous(output)
    if not labels or (previous and len(labels) < len(previous) * MIN_RETAINED):
        print(
            f"::warning::areas: {len(labels)} hardware labels from MAINTAINERS.yml, "
            f"previous had {len(previous or [])}; keeping the previous file",
            file=sys.stderr,
        )
        return False
    if labels != previous:
        write_atomic(output, labels)
    print(f"areas: {len(labels)} hardware labels")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="read this MAINTAINERS.yml instead of fetching")
    parser.add_argument("--output", type=Path, default=Path("data/areas.json"))
    args = parser.parse_args(argv)
    try:
        if args.source:
            text = args.source.read_text(encoding="utf-8")
        else:
            client = GitHubClient(os.environ["GITHUB_TOKEN"])
            text = client.get_text(REPO, MAINTAINERS_PATH, MAINTAINERS_REF)
        update(text, args.output)
    except Exception as err:  # auxiliary step: never fail the collection
        print(f"::warning::areas: {err}; keeping the previous file", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
