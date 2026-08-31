const $ = (id) => document.getElementById(id);
const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
};
const text = (value) => (value == null ? "" : String(value));

let activeRun = null;
let pollTimer = null;

async function loadStatus() {
  try {
    const s = await api("/api/status");
    const ready = s.provider_ready ? "" : " — no API key set";
    const watchers = s.watchers
      .map((w) => `${w.name}: ${w.enabled ? (w.last_error ? "error" : "on") : "off"}`)
      .join(" · ");
    $("status").textContent =
      `${s.provider}/${s.model}${ready} · ${s.tools.length} tools · ${watchers}`;
    const filter = $("source-filter");
    if (filter.options.length === 1) {
      for (const w of s.watchers) {
        filter.add(new Option(w.name, w.name));
      }
    }
    $("watcher-summary").textContent = s.watchers
      .filter((w) => w.enabled)
      .map((w) => `${w.name}: ${w.new_observations} new, last ${w.last_poll || "never"}`)
      .join(" · ");
  } catch (err) {
    $("status").textContent = `status unavailable: ${err.message}`;
  }
}

async function loadRuns() {
  const runs = await api("/api/runs?limit=25");
  const list = $("runs");
  list.innerHTML = "";
  for (const run of runs) {
    const li = document.createElement("li");
    li.className = "click";
    li.innerHTML =
      `<span class="badge ${run.status}">${run.status}</span> ` +
      `<strong></strong> <span class="muted"></span>`;
    li.querySelector("strong").textContent = run.goal.slice(0, 110);
    li.querySelector(".muted").textContent = run.created_at;
    li.onclick = () => showRun(run.id);
    list.appendChild(li);
  }
}

async function showRun(runId) {
  activeRun = runId;
  const run = await api(`/api/runs/${runId}`);
  const box = $("run-detail");
  box.hidden = false;
  const parts = [
    `<div class="row"><span class="badge ${run.status}">${run.status}</span>` +
      `<span class="muted">run #${run.id} · ${run.provider}/${run.model}</span></div>`,
  ];
  for (const step of run.steps) {
    const label = step.name ? `${step.kind} · ${step.name}` : step.kind;
    parts.push(
      `<div class="step ${step.kind}"><div class="k">${label}</div>` +
        `<pre>${escapeHtml(text(step.content))}</pre></div>`
    );
  }
  if (run.error) {
    parts.push(`<div class="step failed"><div class="k">error</div>` +
      `<pre>${escapeHtml(run.error)}</pre></div>`);
  }
  box.innerHTML = parts.join("");

  clearTimeout(pollTimer);
  if (run.status === "running" && activeRun === runId) {
    pollTimer = setTimeout(() => showRun(runId), 1500);
  } else {
    loadRuns();
  }
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

async function loadFeed() {
  const source = $("source-filter").value;
  const query = source ? `?limit=100&source=${encodeURIComponent(source)}` : "?limit=100";
  const items = await api(`/api/observations${query}`);
  const list = $("feed");
  list.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Nothing yet. Watchers write here as they poll.";
    list.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const title = item.url
      ? `<a href="${encodeURI(item.url)}" target="_blank" rel="noreferrer noopener"></a>`
      : "<span></span>";
    li.innerHTML = `<span class="muted">${item.created_at} · ${escapeHtml(item.source)}</span><br>${title}`;
    li.querySelector("a, span:last-child").textContent = item.title;
    list.appendChild(li);
  }
}

$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const goal = $("goal").value.trim();
  if (!goal) return;
  const button = $("submit");
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    const run = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    $("goal").value = "";
    await loadRuns();
    showRun(run.id);
  } catch (err) {
    alert(`Could not start the run: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Run";
  }
});

$("source-filter").addEventListener("change", loadFeed);

loadStatus();
loadRuns();
loadFeed();
setInterval(loadStatus, 15000);
setInterval(loadFeed, 20000);
