/*
 * SPDX-FileCopyrightText: 2026 Erwan Gouriou
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * GitHub Pages serves 404.html for every path that is not a file, so
 * https://erwango.github.io/stm32-pr-dashboard/SPI lands here. Forward it to
 * the dashboard with the path as the area to filter on; app.js resolves it.
 */
(function () {
  "use strict";
  const BASE = "/stm32-pr-dashboard/";
  const path = window.location.pathname;
  if (!path.startsWith(BASE)) {
    return;
  }
  let area = path.slice(BASE.length).replace(/\/+$/, "");
  try {
    area = decodeURIComponent(area);
  } catch {
    area = "";
  }
  const query = area ? `?area=${encodeURIComponent(area)}` : "";
  window.location.replace(BASE + query);
})();
