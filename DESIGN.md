<!--
SPDX-FileCopyrightText: 2026 Erwan Gouriou
SPDX-License-Identifier: Apache-2.0
-->

# STM32 PR dashboard: design

Status: design frozen, implementation not started. Refine on usage.

Repository: `erwango/stm32-pr-dashboard` (public). Pages site:
`https://erwango.github.io/stm32-pr-dashboard/`. Nothing in this project goes
into the Zephyr tree.

## Goal

A GitHub Pages dashboard that monitors STM32 pull requests in
`zephyrproject-rtos/zephyr`:

- show how the number of open STM32 PRs evolves over time;
- show the review backlog per area (SPI, I2C, Bluetooth, ...);
- provide a comment text, pasted by hand on STM32 PRs, that links to the
  dashboard and shows the area backlog, to encourage submitters to review PRs
  already submitted in their area.

Audience: everyone (public repo, public page).

## Decisions

| Topic | Decision |
|---|---|
| Population | PRs in `zephyrproject-rtos/zephyr` carrying the label `platform: STM32` |
| Area | Each `area: *` label on the PR, as applied by the Zephyr assigner |
| Breakdown | Area only. No author, no reviewer, no leaderboard ("no finger pointing") |
| Age clock | From the `platform: STM32` labeling event, not from PR creation |
| History | Rebuilt retroactively, 12 months, daily points |
| Refresh | Every 6 hours (cron) plus manual dispatch |
| Comment | Posted manually (copy-paste). Per-area text variants, one badge per area |
| Freshness | shields.io endpoint badge (self-refreshing, Camo-cached so it lags) |
| Bot | Not now. The hidden marker in the comment keeps the door open |
| Stack | Python collector, REST walk plus GraphQL hydration, JSON committed to the repo, static `index.html` plus Chart.js, deployed with GitHub Actions `deploy-pages` |
| Headers | SPDX, `Copyright (c) 2026 Erwan Gouriou`, Apache-2.0, actions pinned by SHA |

## Rules (published on the page)

- Population: PRs in `zephyrproject-rtos/zephyr` labelled `platform: STM32`.
- Drafts are absent by construction. The upstream assigner workflow
  (`.github/workflows/assigner.yml`) has `if: github.event.pull_request.draft ==
  false`, so a draft PR carries no labels. A PR enters the dataset when it is
  labelled, i.e. when it is ready for review.
- Area = each `area: *` label on the PR. A PR with three area labels counts in
  all three, so the sum over areas exceeds the PR count. A PR with no `area:`
  label is shown as `Unclassified`.
- Age is measured from the `platform: STM32` labeling event ("waiting for ST
  review"), which survives long draft periods.
- No people: no author, reviewer or leaderboard. The unit is the area.

Footnotes to show on the page:

- Upstream `scripts/ci/set_assignees.py` caps labels at `MAX_LABELS = 10`,
  ranked by how much of the PR each area accounts for. Very wide PRs lose
  their tail labels, so area counts are slightly lossy at the edge.
- A PR whose STM32 label was removed before it closed is not returned by a
  label-filtered listing, so historical membership is marginally undercounted.
  Not measurable, stated rather than fixed.

## Facts verified in the Zephyr tree

- `MAINTAINERS.yml` has three STM32 areas (`STM32 Platforms`, `(MPU)`,
  `(Wireless)`), all sharing the single label `platform: STM32`. The label
  therefore cannot give a per-peripheral split; the `area: *` labels do.
- Board and SoC enablement PRs are not "unclassified": the area `Boards/SoCs`
  (label `area: Boards/SoCs`) covers `boards/` and `soc/`. That bucket will
  probably be the largest and is coarse. Accepted: if it dominates, the chart is
  the argument for finer labels upstream. No code needed.
- Area labels are applied by `scripts/ci/set_assignees.py` through the assigner
  workflow, on `opened`, `synchronize`, `reopened`, `ready_for_review`.

## Why the comment cannot say "you are number X"

A pasted comment is static markdown; the only self-refreshing element is an
image, and its URL is fixed text. Identical text on every PR gives an identical
badge. A per-PR rank needs a bot (deferred). Option chosen: one comment variant
per area, each with that area's badge ("SPI: 11 open, median 45 d"). The
dashboard offers a copy button per area, and the variant list is generated from
the areas that currently have open PRs, so it maintains itself.

## Data

`data/prs.json`: one record per STM32 PR ever seen.

```
number, title, url, state (open|merged|closed),
created_at, labeled_at, closed_at, merged_at,
areas: ["SPI", "Boards/SoCs"],
review_decision, first_review_at, last_activity_at
```

- `labeled_at` is the first `LABELED_EVENT` for `platform: STM32`.
- Records for closed PRs are immutable once written, which caps incremental cost.

`data/history/YYYY-MM.json`: daily aggregates, append-only (a past day is never
recomputed, so the series cannot shift silently).

```
date, open_total, open_by_area{}, opened, merged, closed,
age_p50, age_p90, age_max, stale_30d,
awaiting_review, changes_requested, approved_unmerged
```

`badges/overall.json` and `badges/area/<slug>.json`: shields endpoint payloads,
`cacheSeconds: 300`.

- Slug from the label: `area: Bluetooth Host` -> `bluetooth-host`,
  `area: Boards/SoCs` -> `boards-socs`.
- A slug collision fails the run (never silently overwrite).
- Badge files are never deleted: the URL is pasted in live PRs. An area that
  drops to zero renders `0 open`, it never returns 404.

## Collector

API choice: GitHub's search API caps at 1000 results, which 12 months of STM32
PRs would exceed. Do not use it.

- Backfill (one-off, `workflow_dispatch`): `GET
  /repos/zephyrproject-rtos/zephyr/issues?labels=platform:+STM32&state=all&sort=created&direction=asc`.
  The issues endpoint has no 1000 cap and returns PRs. Then one GraphQL pass
  for `labeled_at` and review data, ~25 PRs per batch.
- Incremental (every 6 h): same endpoint with `sort=updated&since=<watermark>`,
  stop at the first record older than the watermark. Hydrate open PRs plus
  newly-closed ones only.
- Window membership: a PR is in the 12-month window if `[labeled_at, closed_at)`
  intersects it, not if it was created in it.
- Failure mode 1: partial API failure must never write a truncated corpus.
  Write to a temp file, assert the record count has not shrunk, then move and
  commit.
- Failure mode 2: scheduled runs are best-effort (delayed or skipped).
  Everything derives from timestamps, never from run count, so a missed run
  self-heals. Committing `data/` each run also keeps the cron from being
  auto-disabled after 60 days of repo inactivity.
- Auth: `GITHUB_TOKEN` is enough (public repo, read only on the Zephyr side).

## Page

Static `index.html` plus Chart.js reading the JSON. No build step.

- Open STM32 PR count over 12 months (headline); opened vs merged per week.
- Backlog by area: horizontal bars sorted by count, annotated with median age;
  click filters the table and reveals that area's snippet.
- Open PRs, oldest first: number, title, areas, age, review state.
- Age distribution; review-state split (awaiting review / changes requested /
  approved-not-merged); stale = no activity for more than 30 days.
- Table filter: a label selector above the PR table (kept in sync with the area
  chart bars and the clickable area tags in each row), with a row count.
- Per-area copy box with a copy button.
- Deep links: `<site>/SPI` opens the page filtered on that area. Pages serves
  `404.html` for unknown paths and `assets/deeplink.js` forwards the path to
  `?area=SPI`; `app.js` matches a label or a slug, ignoring case, and ignores
  unknown names. `BASE` in `deeplink.js` and the asset paths in `404.html`
  hard-code `/stm32-pr-dashboard/`, so they must change if the repo is renamed.
- Visible Rules panel and a `Last updated` stamp (the page is authoritative over
  the badges).

## Comment template (per area)

```
**STM32 · <Area> review queue**
![<Area> review queue](https://img.shields.io/endpoint?url=https://erwango.github.io/stm32-pr-dashboard/badges/area/<slug>.json)

ST-related PRs are reviewed by a small group, so this queue is what stands between this PR and a merge. The fastest way to shorten it is to spend 15 minutes reviewing one of the PRs ahead of yours:
[Open STM32 PRs labelled "area: <Area>"](https://github.com/zephyrproject-rtos/zephyr/pulls?q=is%3Apr+state%3Aopen+label%3A%22area%3A+<Area>%22+label%3A%22platform%3A+STM32%22) · [STM32 PR dashboard](https://erwango.github.io/stm32-pr-dashboard/)

<!-- stm32-pr-dashboard: area=<slug> -->
```

Keep the hidden marker even while pasting by hand: a future bot would key on it
to edit rather than duplicate. Tone: shared queue, help drain it; never "your PR
is deprioritised".

The paragraph is one unwrapped line on purpose (it is pasted into a rich text
comment box). The first link is the Zephyr pull request list filtered by the
area label plus `platform: STM32`; for the `Unclassified` bucket, which has no
label to filter on, it lists all open STM32 PRs.

Paste on new PRs going forward. Do not sweep the ~90 already-open PRs; bulk
pasting reads as spam.

## Repository layout

```
.github/workflows/collect.yml     cron + dispatch, commits data/ and badges/
.github/workflows/pages.yml       deploy-pages from main
collector/  fetch.py  aggregate.py  badges.py
data/prs.json  data/history/
badges/overall.json  badges/area/*.json
index.html  assets/app.js  assets/style.css
README.md  LICENSE  DESIGN.md  AGENTS.md
```

## Commit plan

Each commit must stand alone (builds, tests pass).

1. `collector: fetch STM32 PRs from GitHub`: REST walk, GraphQL hydration,
   schema, `--dry-run`, atomic write with the count-never-shrinks guard.
2. `collector: aggregate daily backlog history`: pure function `prs.json` ->
   `data/history/*.json`, append-only, unit-tested on a fixture (include a PR
   spanning a month boundary and one still open).
3. `ci: collect and commit data on a 6-hourly schedule`: workflow, atomic-write
   guard, actions pinned by SHA.
4. `web: backlog dashboard page`: HTML/JS/CSS, Pages deploy.
5. `web: per-area badges and comment snippets`: `badges/*`, copy boxes.

Python, `ruff`-clean.

## One-time repo setup (by the user)

- `gh repo create erwango/stm32-pr-dashboard --public` (this folder is the clone).
- Settings > Actions > Workflow permissions: Read and write (the collector
  commits `data/`).
- Settings > Pages > Source: GitHub Actions.

## Order of operations after the code lands

1. Run the backfill by dispatch; inspect `prs.json` by hand before trusting it.
2. Let the cron produce two or three cycles.
3. Check the area distribution. If `area: Boards/SoCs` swallows most of the
   backlog, argue for finer labels upstream.
4. Only then start pasting snippets, on new PRs.

## Risks

| Risk | Handling |
|---|---|
| `area: Boards/SoCs` dominates and is too coarse | Accepted; chart is the argument for finer labels |
| Badge lags the dashboard | Camo cache; page authoritative, `Last updated` shown |
| Badge URL 404s after an area empties | Badge files never deleted, render `0 open` |
| Slug collision merges two areas | Run fails on collision |
| Truncated corpus from partial fetch | Count-never-shrinks assertion before commit |
| Bulk pasting reads as spam | New PRs only |
| Drafts invisible | Stated in Rules |
| Scheduled run skew | Idempotent, timestamp-derived |

Standing caution: the badges live in comments that outlive interest in this
project. `badges/**` and the Pages site must keep resolving.
