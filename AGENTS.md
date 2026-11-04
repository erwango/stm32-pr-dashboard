<!--
SPDX-FileCopyrightText: 2026 Erwan Gouriou
SPDX-License-Identifier: Apache-2.0
-->

# Instructions for coding agents

This repository hosts the STM32 PR dashboard for `zephyrproject-rtos/zephyr`.
Read [DESIGN.md](DESIGN.md) first: it holds every decision, the counting rules
and the commit plan, and it is the source of truth. Do not re-ask questions it
already answers.

- Implement the commit plan in order; one logical change per commit, each
  building and passing tests on its own.
- Python, `ruff`-clean. New files carry SPDX headers
  (`Copyright (c) 2026 Erwan Gouriou`, `Apache-2.0`). Pin GitHub Actions by SHA.
- Never add `Signed-off-by:` or `Co-authored-by:`; add one
  `Assisted-by: Copilot:<model>` trailer to commits you help write.
- Never delete files under `badges/`; their URLs are pasted in live PRs.
- Never write a truncated `data/prs.json`: temp file, record count must not
  shrink, then move.
- No people in the data or the page: no author, reviewer or leaderboard.
- Do not create, push or change settings of the GitHub repo without being asked.
- This is not the Zephyr tree: Zephyr's `AGENTS.md` and checkpatch rules do not
  apply here.
