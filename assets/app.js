/*
 * SPDX-FileCopyrightText: 2026 Erwan Gouriou
 * SPDX-License-Identifier: Apache-2.0
 */

"use strict";

const DAY_MS = 86400000;
const STALE_DAYS = 30;
const HISTORY_MONTHS = 13;
const AGE_BUCKETS = [
  ["0-7", 0, 7],
  ["8-14", 8, 14],
  ["15-30", 15, 30],
  ["31-60", 31, 60],
  ["61-90", 61, 90],
  ["91-180", 91, 180],
  [">180", 181, Infinity],
];
const REVIEW_STATES = [
  ["awaiting review", "awaiting"],
  ["changes requested", "changes"],
  ["reviewed, missing approval", "reviewed"],
  ["approved, not merged", "approved"],
];
// Boards/SoCs, Clock Control and Flash are broad labels that often ride along a more specific
// peripheral-driver label. In the backlog-by-area chart and its table filter, a PR carrying one
// of these plus a real peripheral-driver label only counts under that driver, not here. A PR
// carrying several of these three and nothing else keeps only the most specific one, in this
// priority order (broadest first): Boards/SoCs, then Clock Control, then Flash.
const EXCLUSIVE_AREAS = ["Boards/SoCs", "Clock Control", "Flash"];
const EXCLUSIVE_AREA_SET = new Set(EXCLUSIVE_AREAS);

const AREA_ROW_PX = 22;
const UNCLASSIFIED_AREA = "Unclassified";
const SITE = "https://erwango.github.io/stm32-pr-dashboard/";

const state = {
  open: [],
  areaFilter: null,
  areaStats: [],
  hardware: null,
  slugs: {},
  charts: {},
  now: Date.now(),
};

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function fetchJSON(url, optional) {
  const response = await fetch(url, { cache: "no-cache" });
  if (optional && response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`${url}: HTTP ${response.status}`);
  }
  return response.json();
}

function percentile(sorted, q) {
  if (sorted.length === 0) {
    return null;
  }
  const position = q * (sorted.length - 1);
  const low = Math.floor(position);
  const high = Math.min(low + 1, sorted.length - 1);
  return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
}

function ageDays(pr) {
  return (state.now - Date.parse(pr.labeled_at)) / DAY_MS;
}

function reviewState(pr) {
  if (pr.review_decision === "CHANGES_REQUESTED") {
    return "changes";
  }
  if (pr.review_decision === "APPROVED") {
    return "approved";
  }
  return pr.first_review_at ? "reviewed" : "awaiting";
}

function isStale(pr) {
  return (state.now - Date.parse(pr.last_activity_at)) / DAY_MS > STALE_DAYS;
}

function fmt(value) {
  return value === null ? "-" : String(Math.round(value));
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function showError(message) {
  const element = document.getElementById("error");
  element.textContent = message;
  element.hidden = false;
}

/* The corpus comes from the GitHub API but titles are user-controlled:
 * everything is inserted with textContent, and links are limited to github.com. */
function safeUrl(url) {
  return typeof url === "string" && url.startsWith("https://github.com/") ? url : null;
}

function areaStats(open) {
  const byArea = new Map();
  for (const pr of open) {
    for (const area of pr.areas) {
      if (!prMatchesArea(pr, area)) {
        continue;
      }
      if (!byArea.has(area)) {
        // states counts each review state for the area, so the backlog chart can stack by it.
        byArea.set(area, { ages: [], states: Object.fromEntries(REVIEW_STATES.map(([, key]) => [key, 0])) });
      }
      const entry = byArea.get(area);
      entry.ages.push(ageDays(pr));
      entry.states[reviewState(pr)] += 1;
    }
  }
  return [...byArea.entries()]
    .map(([area, { ages, states }]) => ({ area, count: ages.length, states }))
    .sort((a, b) => b.count - a.count || a.area.localeCompare(b.area));
}

/* data/areas.json lists the hardware labels (peripheral drivers, Boards/SoCs), lowercase.
 * Without it, every label counts as hardware and the chart is unrestricted. */
function isHardwareArea(area) {
  return state.hardware === null || state.hardware.has(area.toLowerCase());
}

/* Among a PR's hardware areas that are in EXCLUSIVE_AREAS, the one counted: the most specific
 * (last in priority order), but only if no other, non-exclusive hardware area is present (that
 * always wins outright). Returns null when no exclusive area should count for this PR. */
function exclusiveWinner(pr) {
  const hardwareAreas = pr.areas.filter(isHardwareArea);
  if (hardwareAreas.some((area) => !EXCLUSIVE_AREA_SET.has(area))) {
    return null;
  }
  const present = EXCLUSIVE_AREAS.filter((area) => hardwareAreas.includes(area));
  return present.length > 0 ? present[present.length - 1] : null;
}

/* Shared by the backlog-by-area counts and its table filter, so both always agree. */
function prMatchesArea(pr, area) {
  if (!pr.areas.includes(area)) {
    return false;
  }
  return !EXCLUSIVE_AREA_SET.has(area) || exclusiveWinner(pr) === area;
}

function renderStats(open) {
  const ages = open.map(ageDays).sort((a, b) => a - b);
  setText("stat-open", String(open.length));
  setText("stat-p50", fmt(percentile(ages, 0.5)));
  setText("stat-awaiting", String(open.filter((pr) => reviewState(pr) === "awaiting").length));
  setText(
    "stat-approval",
    String(open.filter((pr) => ["awaiting", "reviewed"].includes(reviewState(pr))).length),
  );
  setText("stat-stale", String(open.filter(isStale).length));
  const staleBefore = new Date(state.now - STALE_DAYS * DAY_MS).toISOString().slice(0, 19) + "Z";
  linkCard("card-open", []);
  linkCard("card-approval", ["review:required"]);
  linkCard("card-stale", [`updated:<${staleBefore}`]);
}

function makeChart(id, config) {
  if (state.charts[id]) {
    state.charts[id].destroy();
  }
  state.charts[id] = new Chart(document.getElementById(id), config);
  return state.charts[id];
}

function renderOpenChart(rows, liveCount) {
  const points = rows.map((row) => ({ x: row.date, y: row.open_total }));
  const today = new Date(state.now).toISOString().slice(0, 10);
  if (!points.some((point) => point.x === today)) {
    points.push({ x: today, y: liveCount });
  }
  makeChart("chart-open", {
    type: "line",
    data: {
      labels: points.map((point) => point.x),
      datasets: [{
        label: "Open PRs",
        data: points.map((point) => point.y),
        borderColor: css("--accent"),
        backgroundColor: css("--accent"),
        pointRadius: 0,
        tension: 0.2,
      }],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 12 } }, y: { beginAtZero: true } },
    },
  });
}

function weekStart(isoDate) {
  const date = new Date(`${isoDate}T00:00:00Z`);
  const offset = (date.getUTCDay() + 6) % 7;
  return new Date(date.getTime() - offset * DAY_MS).toISOString().slice(0, 10);
}

function renderFlowChart(rows) {
  const weeks = new Map();
  for (const row of rows) {
    const key = weekStart(row.date);
    const week = weeks.get(key) || { opened: 0, merged: 0 };
    week.opened += row.opened;
    week.merged += row.merged;
    weeks.set(key, week);
  }
  const labels = [...weeks.keys()].sort();
  makeChart("chart-flow", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Opened (non-draft)", data: labels.map((k) => weeks.get(k).opened), backgroundColor: css("--accent") },
        { label: "Merged", data: labels.map((k) => weeks.get(k).merged), backgroundColor: css("--neutral") },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: {
          callbacks: { title: (items) => `Week starting Monday ${items[0].label}` },
        },
      },
      scales: {
        // Same sparse, horizontal date axis as the open PRs chart.
        x: { title: { display: true, text: "Week starting (Monday)" }, ticks: { maxTicksLimit: 12 } },
        y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: "PRs per week" } },
      },
    },
  });
}

function renderAreaChart(stats) {
  state.areaStats = stats;
  // One fixed-height row per label, plus room for the review-state legend, so every
  // label is drawn and none is skipped.
  document.getElementById("chart-areas-box").style.height = `${stats.length * AREA_ROW_PX + 90}px`;
  const colors = reviewStateColors();
  // Stacked per review state, same labels/colors/legend as the review-state chart.
  // Selection is shown as a border (not a background swap) so it never clashes with a state color.
  const datasets = REVIEW_STATES.map(([label, key], index) => ({
    label,
    data: stats.map((s) => s.states[key]),
    backgroundColor: colors[index],
    borderColor: css("--fg"),
    borderWidth: stats.map((s) => (s.area === state.areaFilter ? 2 : 0)),
  }));
  makeChart("chart-areas", {
    type: "bar",
    data: {
      // A star marks the exclusive areas (see EXCLUSIVE_AREAS); the tooltip explains it on hover.
      labels: stats.map((s) => (EXCLUSIVE_AREA_SET.has(s.area) ? `${s.area} (*)` : s.area)),
      datasets,
    },
    options: {
      indexAxis: "y",
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: {
            footer: (items) => {
              const area = stats[items[0]?.dataIndex]?.area;
              return area && EXCLUSIVE_AREA_SET.has(area)
                ? ["Excludes PRs that also carry another hardware-area label."]
                : [];
            },
          },
        },
      },
      scales: {
        // On top: with one row per label the chart runs tall, so the scale would
        // otherwise sit far below the visible bars once the page is scrolled.
        x: { position: "top", stacked: true, beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: "Open PRs carrying the label" } },
        y: { stacked: true, ticks: { autoSkip: false }, title: { display: true, text: "area: label" } },
      },
      // A click anywhere on a row (bar or label band) selects that label.
      onClick: (event, _elements, chart) => {
        const hits = chart.getElementsAtEventForMode(
          event, "nearest", { axis: "y", intersect: false }, true);
        if (hits.length > 0) {
          const area = stats[hits[0].index].area;
          // Deferred: the filter redraws this very chart, which must not happen mid-event.
          setTimeout(() => setFilter(area === state.areaFilter ? null : area, true), 0);
        }
      },
      onHover: (event, elements) => {
        event.native.target.style.cursor = elements.length > 0 ? "pointer" : "default";
      },
    },
  });
}

// Colors follow REVIEW_STATES' order (awaiting, changes requested, reviewed, approved): shared
// between the age and review-state charts so a state always carries the same color.
function reviewStateColors() {
  return [css("--accent"), css("--neutral"), css("--highlight"), css("--sand")];
}

function renderAgeChart(open) {
  const colors = reviewStateColors();
  // Stacked per review state, same labels/colors/legend as the review-state chart.
  const datasets = REVIEW_STATES.map(([label, key], index) => ({
    label,
    backgroundColor: colors[index],
    data: AGE_BUCKETS.map(([, low, high]) =>
      open.filter((pr) => {
        const age = Math.floor(ageDays(pr));
        return age >= low && age <= high && reviewState(pr) === key;
      }).length),
  }));
  makeChart("chart-age", {
    type: "bar",
    data: {
      labels: AGE_BUCKETS.map(([label]) => `${label} d`),
      datasets,
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } },
      scales: {
        x: { stacked: true },
        y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
      },
    },
  });
}

function renderReviewChart(open) {
  const counts = REVIEW_STATES.map(([, key]) => open.filter((pr) => reviewState(pr) === key).length);
  makeChart("chart-review", {
    type: "doughnut",
    data: {
      labels: REVIEW_STATES.map(([label]) => label),
      datasets: [{
        data: counts,
        backgroundColor: reviewStateColors(),
        borderColor: css("--bg"),
      }],
    },
    options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
  });
}

function cell(row, text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) {
    td.className = className;
  }
  row.appendChild(td);
  return td;
}

function renderTable() {
  const body = document.getElementById("prs");
  body.replaceChildren();
  const rows = state.open
    .filter((pr) => !state.areaFilter || prMatchesArea(pr, state.areaFilter))
    .sort((a, b) => Date.parse(a.labeled_at) - Date.parse(b.labeled_at));

  for (const pr of rows) {
    const tr = document.createElement("tr");
    const link = safeUrl(pr.url);
    if (link) {
      const td = document.createElement("td");
      const a = document.createElement("a");
      a.href = link;
      a.textContent = `#${pr.number}`;
      a.rel = "noopener noreferrer";
      td.appendChild(a);
      tr.appendChild(td);
    } else {
      cell(tr, `#${pr.number}`);
    }
    cell(tr, pr.title);

    const author = document.createElement("td");
    const profile = pr.author ? safeUrl(`https://github.com/${encodeURIComponent(pr.author)}`) : null;
    if (profile) {
      const a = document.createElement("a");
      a.href = profile;
      a.textContent = pr.author;
      a.rel = "noopener noreferrer";
      author.appendChild(a);
    } else {
      author.textContent = pr.author || "-";
    }
    tr.appendChild(author);
    const areas = document.createElement("td");
    for (const area of pr.areas) {
      const tag = document.createElement("button");
      tag.type = "button";
      tag.className = "tag";
      tag.textContent = area;
      tag.title = `Filter by area: ${area}`;
      tag.addEventListener("click", () => setFilter(area));
      areas.appendChild(tag);
    }
    tr.appendChild(areas);

    const age = cell(tr, String(Math.floor(ageDays(pr))), isStale(pr) ? "num stale" : "num");
    if (isStale(pr)) {
      const idle = Math.floor((state.now - Date.parse(pr.last_activity_at)) / DAY_MS);
      age.title = `No activity for ${idle} days`;
    }
    const label = REVIEW_STATES.find(([, key]) => key === reviewState(pr))[0];
    cell(tr, label);
    body.appendChild(tr);
  }
  setText("filter-count", `${rows.length} of ${state.open.length} open PRs`);
}

/* The table filter: every label with open PRs, kept in sync with the chart and the tags. */
function renderFilterSelect(stats) {
  const select = document.getElementById("filter-select");
  select.replaceChildren(new Option("All labels", ""));
  for (const s of stats) {
    select.appendChild(new Option(`${s.area} (${s.count})`, s.area));
  }
  select.addEventListener("change", () => setFilter(select.value || null));
}

/* Shareable link to the selected label's page, shown under the filter. */
function renderFilterLink(area) {
  const url = area === null ? null : areaPageUrl(area);
  document.getElementById("filter-link").hidden = url === null;
  setText("filter-copy-status", "");
  if (url !== null) {
    const link = document.getElementById("filter-url");
    link.href = url;
    link.textContent = url;
  }
}

/* Copies the link, or the review queue comment for the selected label (same text as the box above). */
async function copyFilterText(comment) {
  const text = comment
    ? document.getElementById("snippet-text").value
    : document.getElementById("filter-url").href;
  try {
    await navigator.clipboard.writeText(text);
    setText("filter-copy-status", "Copied");
  } catch {
    setText("filter-copy-status", "Copy failed, select the text and press Ctrl+C");
  }
}

function setFilter(area, scroll = false) {
  state.areaFilter = area;
  document.getElementById("filter-select").value = area ?? "";
  document.getElementById("filter-clear").hidden = area === null;
  renderFilterLink(area);
  renderTable();
  renderAreaChart(state.areaStats);
  if (area !== null) {
    selectSnippetArea(area);
    if (scroll) {
      document.getElementById("filter").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

/* Zephyr pull request search: open PRs with the STM32 label plus extra search terms. The query
 * is encoded by URLSearchParams (quotes, colons, parentheses), so the URL is safe in Markdown. */
function zephyrSearchUrl(extraTerms) {
  const terms = ["is:pr", "state:open", ...extraTerms, 'label:"platform: STM32"'];
  const query = new URLSearchParams({ q: terms.join(" ") });
  return `https://github.com/zephyrproject-rtos/zephyr/pulls?${query}`;
}

/* Summary card turned into a link to the same list on GitHub. */
function linkCard(id, extraTerms) {
  const card = document.getElementById(id);
  card.href = zephyrSearchUrl(extraTerms);
  card.title = "Open this list on GitHub";
}

/* Zephyr pull request list: open PRs with the STM32 label and, when the PR has one, the area label. */
function zephyrFilterUrl(area) {
  return zephyrSearchUrl(area === UNCLASSIFIED_AREA ? [] : [`label:"area: ${area}"`]);
}

/* Unclassified is not an upstream label, so it gets no "area:" prefix. */
function areaTitle(area) {
  return area === UNCLASSIFIED_AREA ? area : `area: ${area}`;
}

/* Address of the page opened on one label (see 404.html). The label itself when it is plain
 * (/SPI), otherwise its slug (/boards-socs), so the path never needs escaping. */
function areaPageUrl(area) {
  const plain = /^[A-Za-z0-9-]+$/.test(area);
  const name = plain ? area : state.slugs[area];
  return name ? `${SITE}${encodeURIComponent(name)}` : null;
}

/* The marker lets a future bot find and edit this comment instead of duplicating it. */
function snippetText(area, slug) {
  const badge = `https://img.shields.io/endpoint?url=${SITE}badges/area/${slug}.json`;
  const overallBadge = `https://img.shields.io/endpoint?url=${SITE}badges/overall.json`;
  const listText = area === UNCLASSIFIED_AREA
    ? "Open STM32 PRs"
    : `Open STM32 PRs labelled "area: ${area}"`;
  const title = areaTitle(area);
  return [
    `**STM32 \u00b7 ${title} open PR**`,
    `[![STM32 open PRs](${overallBadge})](${SITE}) [![${title} open PR](${badge})](${areaPageUrl(area) || SITE})`,
    "",
    "Reviews on ST-related PRs are limited by team bandwidth, and the same team reviews every STM32 PR whatever its area, so the overall queue matters as much as the area one. The fastest way to get your PR merged is to help the team and spend 15 minutes reviewing one of the PRs ahead of yours. Any help in testing will also be welcome:",
    `[${listText}](${zephyrFilterUrl(area)})`,
    "",
    ...(area === UNCLASSIFIED_AREA
      ? []
      : [`[All open STM32 PRs](${zephyrFilterUrl(UNCLASSIFIED_AREA)})`, ""]),
    `<!-- stm32-pr-dashboard: area=${slug} -->`,
    "",
  ].join("\n");
}

function selectSnippetArea(area) {
  const select = document.getElementById("snippet-area");
  if (!state.slugs[area]) {
    return;
  }
  select.value = area;
  document.getElementById("snippet-text").value = snippetText(area, state.slugs[area]);
  setText("snippet-status", "");
}

/* One variant per area that currently has open PRs, so the list maintains itself. */
function renderSnippets(stats) {
  const areas = stats.map((s) => s.area).filter((area) => state.slugs[area]);
  if (areas.length === 0) {
    return;
  }
  const select = document.getElementById("snippet-area");
  select.replaceChildren();
  for (const area of areas) {
    const option = document.createElement("option");
    option.value = area;
    option.textContent = area;
    select.appendChild(option);
  }
  select.addEventListener("change", () => selectSnippetArea(select.value));
  document.getElementById("snippet-copy").addEventListener("click", async () => {
    const text = document.getElementById("snippet-text");
    try {
      await navigator.clipboard.writeText(text.value);
      setText("snippet-status", "Copied");
    } catch {
      text.select();
      setText("snippet-status", "Press Ctrl+C to copy");
    }
  });
  document.getElementById("snippet").hidden = false;
  selectSnippetArea(areas[0]);
}

/* Deep link: ?area=SPI (what 404.html turns /SPI into). Matches a label or its slug, ignoring case.
 * The area is filtered and the page opens on the "Review queue comment" section for it;
 * without ?area= the page is left at the top. */
function applyDeepLink(stats) {
  const wanted = new URLSearchParams(window.location.search).get("area");
  if (!wanted) {
    return;
  }
  const key = wanted.trim().toLowerCase();
  const match = stats.find(
    (s) => s.area.toLowerCase() === key || state.slugs[s.area] === key,
  );
  if (match) {
    setFilter(match.area);
    const section = document.getElementById("snippet");
    if (!section.hidden) {
      requestAnimationFrame(() => section.scrollIntoView({ block: "start" }));
    }
  }
}

async function loadHistory(now) {
  const requests = [];
  for (let i = HISTORY_MONTHS - 1; i >= 0; i--) {
    const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
    const month = date.toISOString().slice(0, 7);
    requests.push(fetchJSON(`data/history/${month}.json`, true));
  }
  const months = await Promise.all(requests);
  const rows = months.flatMap((month) => (month ? month.days : []));
  const cutoff = new Date(state.now - 365 * DAY_MS).toISOString().slice(0, 10);
  return rows.filter((row) => row.date >= cutoff);
}

async function main() {
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--border");

  document.getElementById("filter-clear").addEventListener("click", () => setFilter(null));
  document.getElementById("filter-copy-url").addEventListener("click", () => copyFilterText(false));
  document.getElementById("filter-copy-comment").addEventListener("click", () => copyFilterText(true));

  const corpus = await fetchJSON("data/prs.json", true);
  if (corpus === null) {
    setText("updated", "no data yet");
    showError("No data has been collected yet.");
    return;
  }
  setText("updated", new Date(corpus.generated_at).toUTCString());

  state.open = corpus.prs.filter((pr) => pr.state === "open");
  const index = await fetchJSON("badges/index.json", true);
  state.slugs = index ? index.areas : {};
  const areas = await fetchJSON("data/areas.json", true);
  state.hardware = areas ? new Set(areas.hardware) : null;
  const stats = areaStats(state.open);
  renderStats(state.open);
  renderAreaChart(stats.filter((s) => isHardwareArea(s.area)));
  renderFilterSelect(stats);
  renderSnippets(stats);
  renderAgeChart(state.open);
  renderReviewChart(state.open);
  renderTable();
  applyDeepLink(stats);

  const rows = await loadHistory(new Date(state.now));
  renderOpenChart(rows, state.open.length);
  renderFlowChart(rows);
}

main().catch((error) => showError(`Failed to load data: ${error.message}`));
