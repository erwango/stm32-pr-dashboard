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
  ["reviewed, no decision", "reviewed"],
  ["changes requested", "changes"],
  ["approved, not merged", "approved"],
];

const SITE = "https://erwango.github.io/stm32-pr-dashboard/";

const state = { open: [], areaFilter: null, slugs: {}, charts: {}, now: Date.now() };

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
      if (!byArea.has(area)) {
        byArea.set(area, []);
      }
      byArea.get(area).push(ageDays(pr));
    }
  }
  return [...byArea.entries()]
    .map(([area, ages]) => ({
      area,
      count: ages.length,
      median: percentile(ages.sort((a, b) => a - b), 0.5),
    }))
    .sort((a, b) => b.count - a.count || a.area.localeCompare(b.area));
}

function renderStats(open) {
  const ages = open.map(ageDays).sort((a, b) => a - b);
  setText("stat-open", String(open.length));
  setText("stat-p50", fmt(percentile(ages, 0.5)));
  setText("stat-p90", fmt(percentile(ages, 0.9)));
  setText("stat-awaiting", String(open.filter((pr) => reviewState(pr) === "awaiting").length));
  setText("stat-stale", String(open.filter(isStale).length));
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
        { label: "Opened (labelled)", data: labels.map((k) => weeks.get(k).opened), backgroundColor: css("--accent") },
        { label: "Merged", data: labels.map((k) => weeks.get(k).merged), backgroundColor: css("--accent-2") },
      ],
    },
    options: {
      maintainAspectRatio: false,
      scales: { x: { ticks: { maxTicksLimit: 12 } }, y: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  });
}

function renderAreaChart(stats) {
  makeChart("chart-areas", {
    type: "bar",
    data: {
      labels: stats.map((s) => `${s.area} (median ${fmt(s.median)} d)`),
      datasets: [{ label: "Open PRs", data: stats.map((s) => s.count), backgroundColor: css("--accent") }],
    },
    options: {
      indexAxis: "y",
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, ticks: { precision: 0 } } },
      onClick: (event, _elements, chart) => {
        const hits = chart.getElementsAtEventForMode(event, "nearest", { intersect: true }, true);
        if (hits.length > 0) {
          setFilter(stats[hits[0].index].area);
        }
      },
      onHover: (event, elements) => {
        event.native.target.style.cursor = elements.length > 0 ? "pointer" : "default";
      },
    },
  });
}

function renderAgeChart(open) {
  const counts = AGE_BUCKETS.map(([, low, high]) =>
    open.filter((pr) => {
      const age = Math.floor(ageDays(pr));
      return age >= low && age <= high;
    }).length);
  makeChart("chart-age", {
    type: "bar",
    data: {
      labels: AGE_BUCKETS.map(([label]) => `${label} d`),
      datasets: [{ label: "Open PRs", data: counts, backgroundColor: css("--accent") }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
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
        backgroundColor: [css("--accent-2"), css("--muted"), css("--warn"), css("--accent")],
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
    .filter((pr) => !state.areaFilter || pr.areas.includes(state.areaFilter))
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

    const areas = document.createElement("td");
    for (const area of pr.areas) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = area;
      areas.appendChild(tag);
    }
    tr.appendChild(areas);

    cell(tr, String(Math.floor(ageDays(pr))), isStale(pr) ? "num stale" : "num");
    const label = REVIEW_STATES.find(([, key]) => key === reviewState(pr))[0];
    cell(tr, label);
    body.appendChild(tr);
  }
}

function setFilter(area) {
  state.areaFilter = area;
  document.getElementById("filter").hidden = area === null;
  setText("filter-area", area || "");
  renderTable();
  if (area !== null) {
    selectSnippetArea(area);
    document.getElementById("filter").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

/* The marker lets a future bot find and edit this comment instead of duplicating it. */
function snippetText(area, slug) {
  const badge = `https://img.shields.io/endpoint?url=${SITE}badges/area/${slug}.json`;
  return [
    `**STM32 \u00b7 ${area} review queue**`,
    `![${area} review queue](${badge})`,
    "",
    "ST-related PRs are reviewed by a small group, so this queue is what stands",
    "between this PR and a merge. The fastest way to shorten it is to spend 15",
    "minutes reviewing one of the PRs ahead of yours:",
    `[STM32 PR dashboard](${SITE})`,
    "",
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
  const stats = areaStats(state.open);
  renderStats(state.open);
  renderAreaChart(stats);
  renderSnippets(stats);
  renderAgeChart(state.open);
  renderReviewChart(state.open);
  renderTable();

  const rows = await loadHistory(new Date(state.now));
  renderOpenChart(rows, state.open.length);
  renderFlowChart(rows);
}

main().catch((error) => showError(`Failed to load data: ${error.message}`));
