"use strict";

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

const el = (tag, className, textContent) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent != null) node.textContent = textContent;
  return node;
};

/** Compact numbers the way a trading screen does: keep precision where it matters. */
function formatPrice(value) {
  if (value == null) return "—";
  const abs = Math.abs(value);
  const decimals = abs >= 1000 ? 0 : abs >= 10 ? 2 : 4;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function relativeTime(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** 12-24 point sparkline. Context only, so it carries no axis or labels. */
function sparkline(points, direction) {
  const width = 76;
  const height = 22;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-hidden", "true");
  if (!points || points.length < 2) return svg;

  const low = Math.min(...points);
  const high = Math.max(...points);
  const span = high - low || 1;
  const step = width / (points.length - 1);
  const path = points
    .map((value, i) => {
      const x = i * step;
      const y = height - 2 - ((value - low) / span) * (height - 4);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", path);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", `var(--${direction})`);
  line.setAttribute("stroke-width", "1.5");
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("stroke-linecap", "round");
  svg.appendChild(line);
  return svg;
}

function quoteTile(quote) {
  const change = quote.change_percent;
  const direction = change == null ? "flat" : change > 0.005 ? "up" : change < -0.005 ? "down" : "flat";
  const arrow = direction === "up" ? "▲" : direction === "down" ? "▼" : "▬";

  const tile = el("div", "tile");
  tile.setAttribute("role", "listitem");
  tile.appendChild(el("div", "label", quote.name || quote.symbol));

  const value = el("div", "value", formatPrice(quote.price));
  if (quote.currency) value.title = `${quote.price} ${quote.currency}`;
  tile.appendChild(value);

  const row = el("div", "row");
  const delta = el(
    "span",
    `delta ${direction}`,
    change == null ? "—" : `${arrow} ${change >= 0 ? "+" : ""}${change.toFixed(2)}%`
  );
  // Screen readers get the direction in words, not as a glyph.
  delta.setAttribute(
    "aria-label",
    change == null ? "change unavailable" : `${direction === "down" ? "down" : "up"} ${Math.abs(change).toFixed(2)} percent`
  );
  row.appendChild(delta);
  row.appendChild(sparkline(quote.sparkline, direction));
  tile.appendChild(row);
  return tile;
}

let branches = [];
let activeBranch = null;

/** Very small markdown subset: bold, bullets, line breaks. Text is escaped first. */
function renderBrief(text) {
  const escaped = text
    .replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c])
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^\s*[-*]\s+/gm, "• ");
  return escaped;
}

function cylinder(branch, index) {
  const button = el("button", "cylinder");
  button.type = "button";
  button.setAttribute("role", "tab");
  button.setAttribute("aria-selected", String(branch.slug === activeBranch));
  button.dataset.slug = branch.slug;
  // A cylinder fires when it has an update to show.
  if (branch.brief) button.classList.add("fired");
  else button.classList.add("idle");

  button.appendChild(el("span", "spark-plug"));
  const bore = el("span", "bore");
  bore.appendChild(el("span", "piston"));
  button.appendChild(bore);

  const label = el("span", "cyl-label");
  label.appendChild(el("b", null, branch.name));
  label.appendChild(el("span", null, branch.tagline || `cylinder ${index + 1}`));
  button.appendChild(label);

  button.title = branch.brief
    ? `${branch.name} — updated ${branch.brief_date}`
    : `${branch.name} — no update yet`;
  button.onclick = () => selectBranch(branch.slug);
  return button;
}

async function loadBranches(keepSelection = true) {
  try {
    const data = await api("/api/branches");
    branches = data.branches;
    if (!keepSelection || !branches.some((b) => b.slug === activeBranch)) {
      activeBranch = null;
    }

    const engine = $("engine");
    engine.innerHTML = "";
    branches.forEach((branch, i) => engine.appendChild(cylinder(branch, i)));

    const withBrief = branches.filter((b) => b.brief);
    $("brief-meta").textContent = withBrief.length
      ? `${withBrief.length}/${branches.length} branches updated · ${withBrief[0].brief_date}`
      : "no update yet today";

    if (activeBranch) renderBranch(activeBranch);
  } catch (err) {
    $("brief-meta").textContent = `unavailable: ${err.message}`;
  }
}

function selectBranch(slug) {
  activeBranch = activeBranch === slug ? null : slug;
  for (const button of document.querySelectorAll(".cylinder")) {
    button.setAttribute("aria-selected", String(button.dataset.slug === activeBranch));
  }
  if (!activeBranch) {
    $("branch-board").hidden = true;
    return;
  }
  renderBranch(activeBranch);
}

async function renderBranch(slug) {
  const branch = branches.find((b) => b.slug === slug);
  if (!branch) return;

  $("branch-board").hidden = false;
  $("branch-heading").textContent = branch.name;
  $("branch-meta").textContent = branch.brief_date
    ? `updated ${branch.brief_date} · from ${branch.brief_sources} sources`
    : "waiting for the first brief";

  const box = $("branch-brief");
  box.innerHTML = branch.brief
    ? renderBrief(branch.brief)
    : '<span class="placeholder">No update yet. The brief runs shortly after start, ' +
      'or press Regenerate.</span>';

  // Quotes and sources come from what the watcher collected for this branch.
  const [quotes, sources] = await Promise.all([
    branch.symbols ? api(`/api/observations?limit=120&source=${encodeURIComponent(slug)}`) : [],
    api(`/api/observations?limit=25&source=${encodeURIComponent(slug)}`),
  ]);

  const tiles = $("branch-quotes");
  tiles.innerHTML = "";
  const seen = new Set();
  for (const item of quotes) {
    const data = item.data || {};
    if (data.kind !== "quote" || seen.has(data.symbol)) continue;
    seen.add(data.symbol);
    tiles.appendChild(quoteTile(data));
  }
  tiles.hidden = seen.size === 0;
  tiles.previousElementSibling.hidden = seen.size === 0;

  const list = $("branch-sources");
  list.innerHTML = "";
  const headlines = sources.filter((item) => (item.data || {}).kind !== "quote");
  if (!headlines.length) {
    list.appendChild(el("li", "empty", "Nothing collected yet."));
  }
  for (const item of headlines.slice(0, 12)) {
    const li = el("li");
    if (item.url) {
      const link = el("a", null, item.title);
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      li.appendChild(link);
    } else {
      li.appendChild(el("span", null, item.title));
    }
    li.appendChild(el("span", "meta", relativeTime(item.created_at)));
    list.appendChild(li);
  }
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    $("provider-chip").textContent = `${status.provider}/${status.model}`;
    $("provider-chip").className = `chip ${status.provider_ready ? "on" : "err"}`;
    if (!status.provider_ready) $("provider-chip").title = "No API key set for this provider";

    const chips = $("watcher-chips");
    chips.innerHTML = "";
    let healthy = status.provider_ready;
    for (const watcher of status.watchers) {
      const state = !watcher.enabled ? "off" : watcher.last_error ? "err" : "on";
      if (state === "err") healthy = false;
      const chip = el("span", `chip ${state}`, watcher.name);
      chip.title = !watcher.enabled
        ? `${watcher.name}: not configured`
        : watcher.last_error
          ? `${watcher.name}: ${watcher.last_error}`
          : `${watcher.name}: ${watcher.new_observations} collected, last polled ${relativeTime(watcher.last_poll) || "never"}`;
      chips.appendChild(chip);
    }
    $("health-dot").className = `dot ${healthy ? "ok" : "bad"}`;

  } catch (err) {
    $("health-dot").className = "dot bad";
    $("provider-chip").textContent = `offline: ${err.message}`;
  }
}

async function loadRuns() {
  const runs = await api("/api/runs?limit=15");
  const list = $("runs");
  list.innerHTML = "";
  if (!runs.length) {
    list.appendChild(el("li", "empty", "No runs yet."));
    return;
  }
  for (const run of runs) {
    const li = el("li", "click");
    li.appendChild(el("span", `badge ${run.status}`, run.status));
    if (run.trigger) {
      const auto = el("span", "badge auto", `↻ ${run.trigger}`);
      auto.title = "Started automatically by this rule";
      li.appendChild(document.createTextNode(" "));
      li.appendChild(auto);
    }
    li.appendChild(document.createTextNode(" "));
    li.appendChild(el("span", null, run.goal.slice(0, 90)));
    li.appendChild(el("span", "meta", relativeTime(run.created_at)));
    li.onclick = () => showRun(run.id);
    list.appendChild(li);
  }
}

let activeRun = null;
let runTimer = null;

async function showRun(runId) {
  activeRun = runId;
  const run = await api(`/api/runs/${runId}`);
  if (activeRun !== runId) return;

  const box = $("run-detail");
  box.hidden = false;
  box.innerHTML = "";

  const head = el("div", "form-row");
  head.appendChild(el("span", `badge ${run.status}`, run.status));
  head.appendChild(el("span", "hint", `run #${run.id} · ${run.provider}/${run.model}`));
  box.appendChild(head);

  for (const step of run.steps) {
    const div = el("div", `step ${step.kind}`);
    div.appendChild(el("div", "k", step.name ? `${step.kind} · ${step.name}` : step.kind));
    div.appendChild(el("pre", null, step.content || ""));
    box.appendChild(div);
  }
  if (run.error) {
    const div = el("div", "step error");
    div.appendChild(el("div", "k", "error"));
    div.appendChild(el("pre", null, run.error));
    box.appendChild(div);
  }

  clearTimeout(runTimer);
  if (run.status === "running") {
    // The stream drives updates; this is only a safety net if it is down.
    runTimer = setTimeout(() => showRun(runId), 5000);
  } else {
    loadRuns();
  }
}

$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const goal = $("goal").value.trim();
  if (!goal) return;
  const button = $("submit");
  button.disabled = true;
  $("run-hint").textContent = "starting…";
  try {
    const run = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    $("goal").value = "";
    $("run-hint").textContent = "";
    await loadRuns();
    showRun(run.id);
  } catch (err) {
    $("run-hint").textContent = `could not start: ${err.message}`;
  } finally {
    button.disabled = false;
  }
});

// Ctrl/Cmd+Enter submits, so a goal can be sent without leaving the textarea.
$("goal").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    $("run-form").requestSubmit();
  }
});

$("refresh-brief").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Working…";
  $("brief-meta").textContent = "generating…";
  try {
    const result = await api("/api/brief/run", { method: "POST" });
    await loadBranches();
    $("brief-meta").textContent =
      `${result.sections.length} branch(es) updated · budget ${result.calls_budget} calls`;
  } catch (err) {
    $("brief-meta").textContent = `could not generate: ${err.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Regenerate";
  }
});

/* --- live updates ------------------------------------------------------
   The server pushes run steps and watcher polls over SSE. Polling stays as a
   fallback, but at a slow interval - the stream is the primary path. */
let stream = null;
let streamRetry = 1000;

function setLive(state, detail) {
  const dot = $("live-dot");
  dot.className = `dot ${state}`;
  dot.title = detail || (state === "ok" ? "Live" : "Reconnecting…");
}

function connectStream() {
  if (stream) stream.close();
  stream = new EventSource("/api/stream");

  stream.onopen = () => {
    streamRetry = 1000;
    setLive("ok", "Live");
  };

  stream.onerror = () => {
    setLive("bad", "Disconnected — retrying");
    stream.close();
    // Back off to a minute so a stopped server is not hammered.
    streamRetry = Math.min(streamRetry * 2, 60000);
    setTimeout(connectStream, streamRetry);
  };

  stream.addEventListener("run.started", loadRuns);
  stream.addEventListener("run.step", (event) => {
    const data = JSON.parse(event.data);
    if (data.run_id === activeRun) showRun(data.run_id);
  });
  stream.addEventListener("run.finished", (event) => {
    const data = JSON.parse(event.data);
    if (data.run_id === activeRun) showRun(data.run_id);
    loadRuns();
  });
  stream.addEventListener("watcher.polled", (event) => {
    const data = JSON.parse(event.data);
    if (data.new_observations > 0 && data.watcher === activeBranch) renderBranch(activeBranch);
    loadStatus();
  });
  stream.addEventListener("brief.updated", () => loadBranches());
  stream.addEventListener("watcher.failed", loadStatus);
}

loadStatus();
loadBranches();
loadRuns();
connectStream();

// Slow fallbacks in case the stream is cut by a proxy that buffers SSE.
setInterval(loadStatus, 60000);
setInterval(() => loadBranches(), 300000);
