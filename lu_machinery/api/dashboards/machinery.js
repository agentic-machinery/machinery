"use strict";

(function initMachinery(root) {
  const API_TIMEOUT_MS = 8000;
  const REFRESH_MS = 30000;
  const OPTIONAL_WAIT_MS = 1500;
  const RETRY_LIMIT = 10;
  const THEME_KEY = "machinery-theme";
  const THEMES = ["auto", "light", "dark"];
  const CHANGELOG_URL = "https://github.com/learn-ukrainian/machinery/blob/main/CHANGELOG.md";

  // Single source of truth for machinery navigation. Planning (#584) joins this list when it lands.
  const NAV_PAGES = [
    {href: "/machinery", label: "Overview", matches: ["/", "/index.html", "/machinery", "/machinery/"]},
    {href: "/subscriptions", label: "Subscriptions", matches: ["/subscriptions", "/subscriptions.html"]},
    {href: "/fleet", label: "Fleet", matches: ["/fleet", "/fleet.html"]},
    {href: "/pull-requests", label: "Pull requests", matches: ["/pull-requests", "/pull-requests.html"]},
    {href: "/milestones", label: "Milestones", matches: ["/milestones", "/milestones.html"]},
    {href: "/model-stats", label: "Model statistics", matches: ["/model-stats", "/model-stats.html"]},
  ];

  const SITE_LINKS = [
    {href: "/", label: "Learn Ukrainian"},
    {href: "http://localhost:8768/", label: "KubeDojo"},
  ];

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  function safeHttpUrl(value) {
    return typeof value === "string" && /^https?:\/\//i.test(value) ? value : null;
  }

  // ---- Navigation & theme ----

  function currentPathname(explicitPath) {
    if (typeof explicitPath === "string" && explicitPath) return explicitPath;
    return typeof location === "object" && location && typeof location.pathname === "string"
      ? location.pathname : "/machinery";
  }

  function isActivePage(page, pathname) {
    return page.matches.includes(pathname);
  }

  function renderNavHtml(pathname) {
    const path = currentPathname(pathname);
    return NAV_PAGES.map(page => {
      const current = isActivePage(page, path) ? ' aria-current="page"' : "";
      return `<a href="${escapeHtml(page.href)}"${current}>${escapeHtml(page.label)}</a>`;
    }).join("");
  }

  function renderSitesHtml() {
    return SITE_LINKS.map(site => `<a href="${escapeHtml(site.href)}">${escapeHtml(site.label)}</a>`).join("");
  }

  function readStoredTheme() {
    try {
      const saved = typeof localStorage === "object" && localStorage ? localStorage.getItem(THEME_KEY) : null;
      return THEMES.includes(saved) ? saved : "auto";
    } catch {
      return "auto";
    }
  }

  function writeStoredTheme(mode) {
    try {
      if (typeof localStorage !== "object" || !localStorage) return;
      if (mode === "auto") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, mode);
    } catch {
      // Storage unavailable (private mode or restricted iframe); keep in-memory state.
    }
  }

  function applyTheme(mode) {
    const valid = THEMES.includes(mode) ? mode : "auto";
    const docEl = typeof document === "object" && document ? document.documentElement : null;
    if (docEl && docEl.dataset) {
      if (valid === "auto") delete docEl.dataset.theme;
      else docEl.dataset.theme = valid;
    }
    const button = typeof document === "object" && document && document.getElementById
      ? document.getElementById("theme-toggle") : null;
    if (button) {
      const label = valid === "auto" ? "Auto" : valid === "light" ? "Light" : "Dark";
      const next = THEMES[(THEMES.indexOf(valid) + 1) % THEMES.length];
      button.textContent = `Theme: ${label}`;
      button.dataset.themeMode = valid;
      button.setAttribute("aria-label", `Color theme: ${label}. Switch to ${next}`);
    }
    return valid;
  }

  function renderVersion(version) {
    if (typeof version !== "string" || !/^\d+(\.\d+)*[0-9A-Za-z.+!-]*$/.test(version)) {
      return "lu-machinery version unknown";
    }
    return `<a href="${CHANGELOG_URL}#v${encodeURIComponent(version)}" title="What changed in this release">lu-machinery ${escapeHtml(version)}</a>`;
  }

  function loadVersion() {
    if (typeof document !== "object" || !document || !document.getElementById) return;
    const header = document.getElementById("machinery-version");
    if (!header) return;
    header.innerHTML = renderVersion(header.dataset?.version);
  }

  function initChrome(options = {}) {
    if (typeof document !== "object" || !document || !document.getElementById) return;
    const nav = document.getElementById("machinery-nav");
    if (nav) nav.innerHTML = renderNavHtml(options.pathname);
    const sites = document.getElementById("machinery-sites");
    if (sites) sites.innerHTML = renderSitesHtml();
    let mode = applyTheme(readStoredTheme());
    const toggle = document.getElementById("theme-toggle");
    if (toggle && !toggle.dataset.bound && typeof toggle.addEventListener === "function") {
      toggle.dataset.bound = "1";
      toggle.addEventListener("click", () => {
        mode = THEMES[(THEMES.indexOf(mode) + 1) % THEMES.length];
        writeStoredTheme(mode);
        applyTheme(mode);
      });
    }
    loadVersion();
  }

  // ---- Fetch & formatting ----

  async function fetchJson(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
    try {
      const response = await fetch(url, {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
      let data;
      try { data = JSON.parse(await response.text()); } catch { throw new Error(`${url}: Invalid JSON`); }
      if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error(`${url}: Invalid JSON shape`);
      const headers = response.headers && typeof response.headers.get === "function" ? response.headers : null;
      const rawAge = headers ? headers.get("X-Snapshot-Age-S") : null;
      const age = rawAge === null ? null : Number(rawAge);
      const rawBound = headers ? headers.get("X-Snapshot-Stale-After-S") : null;
      const bound = rawBound === null ? null : Number(rawBound);
      const state = headers ? headers.get("X-Snapshot-State") : null;
      return {
        data,
        age: age !== null && Number.isFinite(age) && age >= 0 ? age : null,
        bound: bound !== null && Number.isFinite(bound) && bound > 0 ? bound : null,
        state,
        snapshot: true,
        receivedAt: Date.now(),
        url,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const moment = typeof value === "number" ? new Date(value < 1e11 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(moment.getTime()) ? null : moment;
  }

  function utc(value) {
    const moment = toDate(value);
    return moment ? moment.toISOString().slice(0, 16).replace("T", " ") + "Z" : "—";
  }

  function span(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
    const s = Math.abs(Math.round(seconds));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
  }

  function relative(value) {
    const moment = toDate(value);
    if (!moment) return "";
    const seconds = (moment.getTime() - Date.now()) / 1000;
    return seconds >= 0 ? `in ${span(seconds)}` : `${span(seconds)} ago`;
  }

  function ageSpan(value) {
    const moment = toDate(value);
    return moment ? span((Date.now() - moment.getTime()) / 1000) : null;
  }

  function validPct(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
  }

  function pct(value) {
    return validPct(value) ? `${Math.round(value)}%` : "—";
  }

  function bar(left) {
    if (!validPct(left)) return "";
    const color = left < 15 ? "var(--bad)" : left < 35 ? "var(--warn)" : "var(--ok)";
    return `<span class="bar"><span style="width:${left}%;background:${color}"></span></span>`;
  }

  // A cell carries its column name, so a stacked (card) layout can show it beside the value.
  // The first cell is the card's own heading, so it keeps no label.
  function labelled(cells, headers) {
    return cells.map((cell, index) => index && headers[index]
      ? cell.replace(/^<td/, `<td data-label="${escapeHtml(headers[index])}"`) : cell).join("");
  }

  function table(headers, rows, stacked = false) {
    if (!rows.length) return '<p class="muted">None.</p>';
    return `<table><thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>`
      + rows.map(cells => `<tr>${stacked ? labelled(cells, headers) : cells.join("")}</tr>`).join("") + "</tbody></table>";
  }

  const td = (html, cls = "") => `<td${cls ? ` class="${cls}"` : ""}>${html}</td>`;
  const text = (value, cls = "") => td(value === null || value === undefined || value === "" ? "—" : escapeHtml(value), cls);
  const unknown = reason => `<p class="muted">Unknown: ${escapeHtml(String(reason || "no reason given").replaceAll("_", " "))}.</p>`;
  const clockUtc = (moment = Date.now()) => new Date(moment).toISOString().slice(11, 19) + " UTC";

  function chip(href, count, label, cls = "") {
    return `<a class="chip${cls ? ` ${cls}` : ""}" href="${escapeHtml(href)}"><b>${escapeHtml(count)}</b> ${escapeHtml(label)}</a>`;
  }

  // ---- Shared domain helpers (subscriptions & PRs) ----

  function readingState(freshness, ageS, thresholdS) {
    const aged = typeof ageS === "number" && Number.isFinite(ageS) && ageS >= 0;
    const limit = typeof thresholdS === "number" && thresholdS > 0 ? thresholdS : 900;
    if (freshness === "stale_last_good") return "last_good";
    if (freshness === "stale" || ((freshness === "fresh") && aged && ageS > limit)) return "stale";
    if (freshness === "fresh") return aged ? "fresh" : "unknown";
    return freshness ? String(freshness) : "missing";
  }

  function freshnessCell(state, ageS) {
    const age = typeof ageS === "number" && Number.isFinite(ageS) ? `${span(ageS)} old` : "age unknown";
    if (state === "fresh") return text(`fresh, ${age}`, "ok");
    if (state === "last_good") return text(`Last good reading, ${age}`, "warn");
    if (state === "stale") return text(`STALE, ${age}`, "bad");
    if (state === "unknown") return text(`FRESHNESS UNKNOWN, ${age}`, "bad");
    return text(state === "missing" ? "NO READING" : state.toUpperCase(), "bad");
  }

  function pp(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${Math.round(Math.abs(value))} pp` : null;
  }

  function windowLeft(window) {
    if (!window || window.status === "none") return text("none", "muted");
    if (window.status !== "ok" || !validPct(window.left_pct)) return text("unknown", "muted");
    return td(`${bar(window.left_pct)}${escapeHtml(pct(window.left_pct))}`, "num");
  }

  function paceCell(pace) {
    if (!pace || pace.status !== "ok") return text("unknown", "muted");
    const head = pace.state === "on_pace" ? "on pace"
      : pace.state === "reserve" ? `${pp(pace.headroom_pct)} headroom` : `${pp(pace.headroom_pct)} deficit`;
    const tail = pace.lasts_to_reset === true ? "lasts to reset"
      : pace.lasts_to_reset === false ? (!toDate(pace.runs_out_at) ? "runs out before reset"
        : toDate(pace.runs_out_at).getTime() <= Date.now() ? `ran out ${utc(pace.runs_out_at)} (${relative(pace.runs_out_at)})`
        : `runs out ${utc(pace.runs_out_at)} (${relative(pace.runs_out_at)})`)
      : "last-to-reset unknown";
    // The routing status reads the same verdict: running out before the reset is bad at any headroom.
    return text(`${head} · ${tail}`, pace.lasts_to_reset === false ? "bad" : pace.state === "deficit" ? "warn" : "ok");
  }

  function readingCell(reading, staleAfter) {
    const state = readingState(reading?.status, reading?.age_s, staleAfter);
    if (state === "missing" || state === "unknown" && !reading?.read_at) {
      return text(`NO READING${reading?.reason_code ? ` (${String(reading.reason_code).replaceAll("_", " ")})` : ""}`, "bad");
    }
    return freshnessCell(state, reading?.age_s);
  }

  // Sign-in state joins the subscriptions row by account label ("Claude a").
  function signInCell(account, logins) {
    if (!logins) return text("unknown", "muted");
    if (logins.state === "pending") return text("checking…", "muted");
    const row = (Array.isArray(logins.accounts) ? logins.accounts : []).find(item => item.account === account);
    if (!row) return text("no sign-in reader", "muted");
    if (row.logged_in === false) return text("SIGNED OUT", "bad");
    if (row.logged_in !== true) return text(`UNKNOWN${row.reason_code ? ` (${String(row.reason_code).replaceAll("_", " ")})` : ""}`, "bad");
    const expires = toDate(row.expires_at);
    if (!expires) return text("signed in", "ok");
    const daysLeft = (expires.getTime() - Date.now()) / 86400000;
    return text(`signed in · ends ${relative(row.expires_at)}`, daysLeft < 3 ? "bad" : daysLeft < 7 ? "warn" : "ok");
  }

  function liveAccounts(subs) {
    return (Array.isArray(subs?.providers) ? subs.providers : [])
      .flatMap(provider => Array.isArray(provider.accounts) ? provider.accounts : []).filter(row => !row.plan?.ended);
  }

  function summarizeSubscriptions(subs, logins) {
    const accounts = liveAccounts(subs);
    let runsOut = 0, unknownCount = 0, deficitCount = 0, healthyCount = 0;
    const staleAfter = subs?.stale_after_s;
    for (const row of accounts) {
      const state = readingState(row.reading?.status, row.reading?.age_s, staleAfter);
      const pace = row.pace;
      const answered = state === "fresh" && pace && pace.status === "ok" && typeof pace.lasts_to_reset === "boolean";
      if (!answered) { unknownCount += 1; continue; }
      if (pace.lasts_to_reset === false) runsOut += 1;
      else if (pace.state === "deficit") deficitCount += 1;
      else healthyCount += 1;
    }
    const signedOut = (Array.isArray(logins?.accounts) ? logins.accounts : []).filter(row => row.logged_in === false).length;
    return {total: accounts.length, runsOut, unknownCount, deficitCount, healthyCount, signedOut};
  }

  function subscriptionChips(subs, logins, href = "/subscriptions") {
    if (!subs) return [];
    const {runsOut, unknownCount, signedOut} = summarizeSubscriptions(subs, logins);
    const noun = runsOut === 1 ? "account runs out before reset" : "accounts run out before reset";
    const out = [chip(href, runsOut, noun + (unknownCount ? `, ${unknownCount} unknown` : ""),
      runsOut ? "bad" : unknownCount ? "warn" : "")];
    if (signedOut) out.push(chip(href, signedOut, "signed out", "bad"));
    return out;
  }

  function ciLabel(state) {
    if (state === "success") return ["CI green", "ok"];
    if (state === "failure" || state === "error") return ["CI red", "bad"];
    if (state === "pending" || state === "expected") return ["CI running", "warn"];
    return ["no CI yet", "muted"];
  }

  function reviewLabel(state) {
    if (state === "success") return ["approved", "ok"];
    if (state === "failure" || state === "error") return ["changes / refused", "bad"];
    if (state === "pending") return ["pending", "warn"];
    return ["none", "muted"];
  }

  function prLink(repo, pr) {
    const label = `${repo}#${pr.number}`;
    const safe = safeHttpUrl(pr.url);
    return safe ? `<a href="${escapeHtml(safe)}">${escapeHtml(label)}</a>` : escapeHtml(label);
  }

  function repoCoverageIncomplete(repo) {
    if (!repo || repo.status !== "ok") return true;
    const list = Array.isArray(repo.pull_requests) ? repo.pull_requests : [];
    return typeof repo.open_total !== "number" || repo.open_total > list.length;
  }

  function prChips(prs, href = "/pull-requests") {
    if (!prs) return [];
    if (prs.status !== "ok") return [chip(href, "?", "PRs unknown", "warn")];
    const repos = Array.isArray(prs.repositories) ? prs.repositories : [];
    const open = repos.reduce((sum, repo) => sum + (typeof repo.open_total === "number" ? repo.open_total : 0), 0);
    const queued = repos.reduce((sum, repo) => sum + (repo.status === "ok" ? (repo.pull_requests || []).filter(pr => pr.queue).length : 0), 0);
    const partial = repos.some(repo => repo.status !== "ok");
    const truncated = repos.some(repo => repo.status === "ok" && repoCoverageIncomplete(repo));
    const notes = [];
    if (partial) notes.push("a repository is unknown");
    if (truncated) notes.push("coverage truncated");
    const queuedText = notes.length ? `${queued} queued observed` : `${queued} queued`;
    const suffix = notes.length ? ` (${notes.join(", ")})` : "";
    return [chip(href, open, `open PRs · ${queuedText}${suffix}`, notes.length ? "warn" : "")];
  }

  // ---- Freshness: one reading of "how old, and is it stale" per source ----
  //
  // The panel heading and the rows below it both come from the payload fields the API returns.
  // The X-Snapshot-* headers are only the fallback for a payload that carries no age of its own
  // (a gateway or cache between the page and the API can strip or rewrite headers, never the body).

  const finiteAge = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;

  function rowsFreshness(rows, bound) {
    const ages = rows.map(row => finiteAge(row.age)).filter(age => age !== null);
    // A row with no usable reading (missing, unknown) is not fresh, so it cannot leave the heading fresh.
    const flag = rows.some(row => row.state !== "fresh" && row.state !== "last_good") ? "stale"
      : rows.some(row => row.state === "last_good") ? "last_good" : ages.length ? "fresh" : null;
    return {age: ages.length ? Math.max(...ages) : null, bound, flag};
  }

  function payloadAge(url, data, receivedAt) {
    const since = value => {
      const moment = toDate(value);
      return moment ? Math.max(0, (receivedAt - moment.getTime()) / 1000) : null;
    };
    if (url === "/api/milestones") {
      return {age: finiteAge(data.age_seconds), bound: null, flag: typeof data.stale === "boolean" ? (data.stale ? "stale" : "fresh") : null};
    }
    if (url === "/api/runtime/subscriptions") {
      const bound = finiteAge(data.stale_after_s) || null;
      return rowsFreshness(liveAccounts(data).map(row => ({
        age: row.reading?.age_s,
        state: readingState(row.reading?.status, row.reading?.age_s, bound ?? undefined),
      })), bound);
    }
    if (url === "/api/state/routing-budget") {
      const bound = finiteAge(data.diagnostics?.stale_threshold_s) || null;
      const rows = Object.values(data.api_accounts || {}).map(row => ({
        age: row.age_s,
        state: readingState(row.freshness, row.age_s, bound ?? undefined),
      }));
      const base = rowsFreshness(rows, bound), snapshotAge = finiteAge(data.diagnostics?.data_age_s);
      const stale = data.diagnostics?.stale;
      return {age: base.age ?? snapshotAge, bound, flag: stale === true ? "stale" : base.flag ?? (stale === false ? "fresh" : null)};
    }
    if (url === "/api/runtime/logins") return {age: since(data.read_at), bound: null, flag: null};
    if (url === "/api/git/pull-requests") {
      const partial = (Array.isArray(data.repositories) ? data.repositories : []).some(repo => repo.status !== "ok");
      return {age: since(data.observed_at), bound: null, flag: partial ? "stale" : null};
    }
    if (url === "/api/ops/v1/alerts") return {age: since(data.observed_at), bound: finiteAge(data.stale_after_s) || null, flag: null};
    return null;
  }

  function payloadFreshness(url, data, receivedAt) {
    const own = payloadAge(url, data, receivedAt);
    const status = typeof data.status === "string" ? data.status : typeof data.state === "string" ? data.state : null;
    const repos = Array.isArray(data.repositories) ? data.repositories : [];
    const usable = repo => repo.status === "ok" || (Array.isArray(repo.milestones) && repo.milestones.length > 0);
    const nobody = status === "partial" && repos.length > 0 && !repos.some(usable);
    const failed = status !== null && status !== "ok" && status !== "partial";
    return own && {...own, unavailable: failed || nobody, partial: status === "partial"};
  }

  function freshnessOf(reading) {
    if (!reading || !reading.data) return {age: null, bound: null, flag: null, unavailable: true};
    const own = payloadFreshness(reading.url, reading.data, reading.receivedAt ?? Date.now());
    const headerBound = Number.isFinite(reading.bound) && reading.bound > 0 ? reading.bound : null;
    if (own && (own.age !== null || own.flag !== null || own.unavailable)) return own;
    const flag = reading.state === "stale" ? "stale" : null;
    return {age: reading.age ?? null, bound: headerBound, flag, unavailable: reading.state === "unavailable"};
  }

  function freshnessState(fresh, elapsed = 0) {
    if (fresh.unavailable) return "unavailable";
    if (fresh.flag === "stale") return "stale";
    const age = fresh.age === null ? null : fresh.age + elapsed;
    if (age !== null && fresh.bound !== null && age > fresh.bound) return "stale";
    if (fresh.flag === "last_good") return "last_good";
    if (fresh.partial) return "partial";
    if (age === null) return fresh.unavailable ? "unavailable" : "unknown";
    return fresh.flag === "fresh" || fresh.bound !== null ? "fresh" : "unknown";
  }

  const FRESHNESS_LABELS = {
    fresh: "fresh",
    stale: "STALE",
    last_good: "last good reading",
    partial: "partial data",
    unknown: "freshness unknown",
    unavailable: "unavailable",
  };

  const DEFAULT_SOURCE_LABELS = ["Usage", "API balances", "Sign-in"];

  function headingAge(readings, badOnFailure, attemptedAt = null, sourceLabels = DEFAULT_SOURCE_LABELS) {
    const sources = Array.isArray(readings) ? readings : [];
    const moment = sources[0]?.receivedAt ?? attemptedAt;
    const label = sources.length ? "Fetched" : "Last attempt";
    const fetched = moment ? `${label} at ${clockUtc(moment)}` : `${label} time unknown`;
    if (!sources.length) return {text: `${fetched} · unavailable · source age unknown`, bad: Boolean(badOnFailure)};
    let bad = false, caution = false;
    const labels = sources.map((reading, index) => {
      const fresh = freshnessOf(reading);
      const elapsed = reading?.receivedAt ? Math.max(0, Date.now() - reading.receivedAt) / 1000 : 0;
      const state = freshnessState(fresh, elapsed);
      bad ||= state === "stale" || state === "unavailable";
      caution ||= state === "last_good" || state === "unknown" || state === "partial";
      const prefix = sources.length > 1 ? `${(sourceLabels && sourceLabels[index]) || "Source"}: ` : "";
      const overdue = state === "stale" && freshnessState(fresh) !== "stale" ? " · refresh overdue" : "";
      return `${prefix}${FRESHNESS_LABELS[state]} · source age ${fresh.age === null ? "unknown" : `${Math.ceil(fresh.age)}s`}`
        + ` · bound ${fresh.bound === null ? "unknown" : `${fresh.bound}s`}${overdue}`;
    });
    return {text: `${fetched} · ${labels.join("; ")}`, bad, caution};
  }

  // ---- Panel error handling, announcements & runner ----

  function isRefusal(error) {
    const message = String(error && error.message ? error.message : error);
    const status = /HTTP (\d{3})/.exec(message);
    return Boolean(status) && (status[1] === "401" || status[1] === "403");
  }

  function failureSentence(error) {
    const message = String(error && error.message ? error.message : error);
    const status = /HTTP (\d{3})/.exec(message);
    if (status) {
      const code = Number(status[1]);
      if (code === 401 || code === 403) return "This panel couldn't load: the source refused the request.";
      if (code === 404) return "This panel couldn't load: the source was not found.";
      if (code >= 500) return "This panel couldn't load: the source failed.";
      return "This panel couldn't load: the source rejected the request.";
    }
    if (/abort|timeout|timed out/i.test(message)) return "This panel couldn't load: the source did not answer in time.";
    if (/JSON/i.test(message)) return "This panel couldn't load: the source returned data this page could not read.";
    return "This panel couldn't load.";
  }

  function panelError(panel, error) {
    const name = escapeHtml(panel.label || panel.id);
    const detail = escapeHtml(error && error.message ? error.message : error);
    return `<p class="error">${failureSentence(error)}</p>`
      + `<p><button type="button" class="retry" data-retry-panel="${escapeHtml(panel.id)}" aria-label="Retry ${name}">Retry</button></p>`
      + `<details><summary>Technical detail</summary><p class="muted">${detail}</p></details>`;
  }

  function panelUnavailable() {
    return '<p class="muted">This panel isn\'t available in this view.</p>';
  }

  function announce(panel, state) {
    if (panel.state === state) return;
    panel.state = state;
    const region = document.getElementById(`${panel.id}-status`);
    if (!region) return;
    const name = panel.label || panel.id;
    region.textContent = state === "loading" ? `Loading ${name}.`
      : state === "ready" ? `${name} loaded.`
      : state === "unavailable" ? `${name} isn't available in this view.`
      : `${name} couldn't load.`;
  }

  function updatePanelAge(panel) {
    const age = document.getElementById(`${panel.id}-age`);
    if (!age) return;
    const line = headingAge(panel.readings, panel.ok === false && !panel.refusal, panel.attemptedAt, panel.sourceLabels);
    age.textContent = line.text;
    age.className = line.bad ? "age bad" : line.caution ? "age warn" : "age";
  }

  function showRefresh(panels) {
    const status = document.getElementById("last-refresh");
    const retry = document.getElementById("refresh-retry");
    if (!status) return;
    const realFailure = panels.some(panel => panel.ok === false && panel.refusal !== true);
    const anyLoaded = panels.some(panel => panel.ok === true);
    if (!realFailure) {
      status.textContent = anyLoaded ? `Updated ${clockUtc()} · every 30 s` : `Nothing is available in this view, ${clockUtc()}`;
      if (retry) retry.hidden = true;
      return;
    }
    status.textContent = `Couldn't refresh, last attempt ${clockUtc()}`;
    if (retry) retry.hidden = false;
  }

  const DEFAULT_OPTIONAL = new Set(["/api/git/pull-requests", "/api/state/routing-budget", "/api/runtime/logins"]);

  function createPanelRunner(options) {
    const panels = Array.isArray(options?.panels) ? options.panels : [];
    const optionalUrls = options?.optionalUrls || DEFAULT_OPTIONAL;
    const renderChips = typeof options?.renderChips === "function" ? options.renderChips : null;
    const onAfterLoad = typeof options?.onAfterLoad === "function" ? options.onAfterLoad : null;
    const fetchImpl = typeof options?.fetchJson === "function" ? options.fetchJson : fetchJson;
    const latest = {};
    const inFlight = new Set();

    function readSource(url, index) {
      if (index === 0 || !optionalUrls.has(url)) return fetchImpl(url);
      const missing = {data: null, age: null, snapshot: false, url};
      return Promise.race([
        fetchImpl(url).catch(() => missing),
        new Promise(resolve => setTimeout(() => resolve(missing), OPTIONAL_WAIT_MS)),
      ]);
    }

    function updateChips() {
      if (!renderChips) return;
      const box = document.getElementById("chips");
      if (box) box.innerHTML = renderChips(latest);
    }

    async function loadPanel(panel) {
      if (inFlight.has(panel.id)) return;
      inFlight.add(panel.id);
      panel.attemptedAt = Date.now();
      const content = document.getElementById(`${panel.id}-content`);
      const age = document.getElementById(`${panel.id}-age`);
      if (age && !age.textContent) age.textContent = "Loading…";
      if (panel.state !== "ready") announce(panel, "loading");
      try {
        const readings = await Promise.all(panel.urls.map(readSource));
        const data = readings.map(reading => reading.data);
        if (content) content.innerHTML = panel.render(...data);
        latest[panel.id] = data[0];
        if (typeof panel.onData === "function") panel.onData(data, latest);
        if (onAfterLoad) onAfterLoad(panel, data, latest);
        const ageLine = headingAge(readings, false, panel.attemptedAt, panel.sourceLabels);
        panel.readings = readings;
        if (age) {
          age.textContent = ageLine.text;
          age.className = ageLine.bad ? "age bad" : ageLine.caution ? "age warn" : "age";
        }
        panel.ok = true;
        panel.refusal = false;
        announce(panel, "ready");
        if (panel.retry && panel.retry(...data)) {
          panel.retries = (panel.retries || 0) + 1;
          if (panel.retries <= RETRY_LIMIT) setTimeout(() => loadPanel(panel), 3000);
        } else {
          panel.retries = 0;
        }
      } catch (error) {
        const refused = isRefusal(error);
        if (content) content.innerHTML = refused ? panelUnavailable() : panelError(panel, error);
        const ageLine = headingAge(null, !refused, panel.attemptedAt, panel.sourceLabels);
        panel.readings = null;
        if (age) {
          age.textContent = ageLine.text;
          age.className = ageLine.bad ? "age bad" : "age";
        }
        latest[panel.id] = null;
        if (typeof panel.onError === "function") panel.onError(latest);
        panel.ok = false;
        panel.refusal = refused;
        announce(panel, refused ? "unavailable" : "error");
      } finally {
        inFlight.delete(panel.id);
      }
    }

    async function retryPanel(id) {
      const panel = panels.find(item => item.id === id);
      if (!panel) return;
      await loadPanel(panel);
      updateChips();
      showRefresh(panels);
    }

    async function refreshAll() {
      loadVersion();
      await Promise.all(panels.map(loadPanel));
      updateChips();
      showRefresh(panels);
    }

    function bindEvents() {
      if (typeof document !== "object" || !document) return;
      const retryBtn = document.getElementById("refresh-retry");
      if (retryBtn && !retryBtn.dataset?.bound && typeof retryBtn.addEventListener === "function") {
        if (retryBtn.dataset) retryBtn.dataset.bound = "1";
        retryBtn.addEventListener("click", () => { refreshAll(); });
      }
      if (typeof document.addEventListener === "function" && !document._machineryRetryBound) {
        document._machineryRetryBound = true;
        document.addEventListener("click", event => {
          const target = event.target && typeof event.target.closest === "function"
            ? event.target.closest("button[data-retry-panel]") : null;
          if (target) retryPanel(target.dataset.retryPanel);
        });
      }
    }

    function start() {
      initChrome();
      bindEvents();
      refreshAll();
      setInterval(refreshAll, REFRESH_MS);
      // This clock advances even if a refresh stalls or the network disappears. It makes no requests.
      setInterval(() => panels.filter(panel => panel.ok !== undefined).forEach(updatePanelAge), 1000);
    }

    return {panels, latest, loadPanel, retryPanel, refreshAll, start};
  }

  const Machinery = {
    API_TIMEOUT_MS,
    REFRESH_MS,
    OPTIONAL_WAIT_MS,
    RETRY_LIMIT,
    CHANGELOG_URL,
    NAV_PAGES,
    SITE_LINKS,
    FRESHNESS_LABELS,
    DEFAULT_OPTIONAL,
    escapeHtml,
    safeHttpUrl,
    renderNavHtml,
    renderSitesHtml,
    applyTheme,
    renderVersion,
    loadVersion,
    initChrome,
    fetchJson,
    toDate,
    utc,
    span,
    relative,
    ageSpan,
    validPct,
    pct,
    bar,
    labelled,
    table,
    td,
    text,
    unknown,
    clockUtc,
    chip,
    readingState,
    freshnessCell,
    pp,
    windowLeft,
    paceCell,
    readingCell,
    signInCell,
    liveAccounts,
    summarizeSubscriptions,
    subscriptionChips,
    ciLabel,
    reviewLabel,
    prLink,
    repoCoverageIncomplete,
    prChips,
    finiteAge,
    rowsFreshness,
    payloadAge,
    payloadFreshness,
    freshnessOf,
    freshnessState,
    headingAge,
    isRefusal,
    failureSentence,
    panelError,
    panelUnavailable,
    announce,
    updatePanelAge,
    showRefresh,
    createPanelRunner,
  };

  if (typeof root === "object" && root) root.Machinery = Machinery;
  if (typeof module !== "undefined" && module.exports) module.exports = Machinery;
})(typeof globalThis !== "undefined" ? globalThis : this);
