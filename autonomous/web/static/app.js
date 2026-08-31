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

async function loadMarkets() {
  try {
    const { quotes, headlines } = await api("/api/markets");

    const tiles = $("quotes");
    tiles.innerHTML = "";
    if (!quotes.length) {
      tiles.appendChild(el("p", "empty", "Waiting for the first poll…"));
    } else {
      for (const quote of quotes) tiles.appendChild(quoteTile(quote));
      $("markets-updated").textContent = `updated ${relativeTime(quotes[0].observed_at)}`;
    }

    const list = $("headlines");
    list.innerHTML = "";
    if (!headlines.length) {
      list.appendChild(el("li", "empty", "No market headlines collected yet."));
    }
    for (const item of headlines) {
      const li = el("li");
      const link = el("a", null, item.title);
      if (item.url) {
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
      }
      li.appendChild(link);

      const meta = el("span", "meta");
      meta.appendChild(
        document.createTextNode(
          [item.source, relativeTime(item.observed_at)].filter(Boolean).join(" · ")
        )
      );
      const ask = el("button", "link-button", "ask about this");
      ask.type = "button";
      ask.onclick = () => {
        $("goal").value = `About this headline: "${item.title}"${item.url ? ` (${item.url})` : ""}\n\nWhat happened, and why does it matter for the market?`;
        $("goal").focus();
        $("goal").scrollIntoView({ behavior: "smooth", block: "center" });
      };
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(ask);
      li.appendChild(meta);
      list.appendChild(li);
    }
  } catch (err) {
    $("markets-updated").textContent = `unavailable: ${err.message}`;
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

    const filter = $("source-filter");
    if (filter.options.length === 1) {
      for (const watcher of status.watchers) filter.add(new Option(watcher.name, watcher.name));
    }
  } catch (err) {
    $("health-dot").className = "dot bad";
    $("provider-chip").textContent = `offline: ${err.message}`;
  }
}

async function loadRules() {
  try {
    const data = await api("/api/rules");
    const list = $("rules");
    list.innerHTML = "";

    if (!data.rules.length) {
      $("rules-summary").textContent =
        "No rules configured. Copy rules.example.json to rules.json to have the agent react on its own.";
      return;
    }
    const left = data.budget_per_day - data.budget_used;
    $("rules-summary").textContent = data.enabled
      ? `${data.rules.length} rule(s) · ${data.budget_used}/${data.budget_per_day} automatic runs used today${left === 0 ? " — budget spent" : ""}`
      : "Rules are switched off (RULES_ENABLED=false).";

    for (const rule of data.rules) {
      const li = el("li");
      li.appendChild(el("span", null, rule.name));
      li.appendChild(el("span", "meta", `cooldown ${rule.cooldown_minutes}m`));
      li.title = rule.goal;
      list.appendChild(li);
    }
  } catch (err) {
    $("rules-summary").textContent = `rules unavailable: ${err.message}`;
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

async function loadFeed() {
  const source = $("source-filter").value;
  const query = source ? `?limit=80&source=${encodeURIComponent(source)}` : "?limit=80";
  const items = await api(`/api/observations${query}`);
  const list = $("feed");
  list.innerHTML = "";
  if (!items.length) {
    list.appendChild(el("li", "empty", "Nothing yet. Watchers write here as they poll."));
    return;
  }
  for (const item of items) {
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
    li.appendChild(el("span", "meta", `${item.source} · ${relativeTime(item.created_at)}`));
    list.appendChild(li);
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

$("source-filter").addEventListener("change", loadFeed);
$("refresh-markets").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    await api("/api/watchers/markets/poll", { method: "POST" });
    await Promise.all([loadMarkets(), loadStatus(), loadFeed()]);
  } catch (err) {
    $("markets-updated").textContent = `refresh failed: ${err.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
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

  stream.addEventListener("run.started", () => {
    loadRuns();
    loadRules();  // an automatic run consumes budget
  });
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
    if (data.new_observations > 0) {
      loadFeed();
      if (data.watcher === "markets") loadMarkets();
    }
    loadStatus();
  });
  stream.addEventListener("watcher.failed", loadStatus);
}

loadStatus();
loadMarkets();
loadRuns();
loadRules();
loadFeed();
connectStream();

// Slow fallbacks in case the stream is cut by a proxy that buffers SSE.
setInterval(loadStatus, 60000);
setInterval(loadMarkets, 300000);
setInterval(loadFeed, 300000);
