const state = {
  demos: [],
  selected: null,
  filter: "all",
  lastResultText: "",
};

const el = {
  totalDemos: document.querySelector("#totalDemos"),
  demoCount: document.querySelector("#demoCount"),
  demoList: document.querySelector("#demoList"),
  selectedLabel: document.querySelector("#selectedLabel"),
  selectedDescription: document.querySelector("#selectedDescription"),
  selectedCategory: document.querySelector("#selectedCategory"),
  jsonInput: document.querySelector("#jsonInput"),
  inputHint: document.querySelector("#inputHint"),
  runBtn: document.querySelector("#runBtn"),
  sampleBtn: document.querySelector("#sampleBtn"),
  formatBtn: document.querySelector("#formatBtn"),
  resultSummary: document.querySelector("#resultSummary"),
  resultView: document.querySelector("#resultView"),
  artifactList: document.querySelector("#artifactList"),
  outputDir: document.querySelector("#outputDir"),
  historyList: document.querySelector("#historyList"),
  lastStatus: document.querySelector("#lastStatus"),
  lastLatency: document.querySelector("#lastLatency"),
  copyBtn: document.querySelector("#copyBtn"),
  refreshHistoryBtn: document.querySelector("#refreshHistoryBtn"),
  clearHistoryBtn: document.querySelector("#clearHistoryBtn"),
};

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function setBusy(isBusy) {
  el.runBtn.disabled = isBusy;
  el.runBtn.textContent = isBusy ? "运行中..." : "运行演示";
}

function setHint(text, tone = "neutral") {
  el.inputHint.textContent = text;
  el.inputHint.className = tone === "error" ? "status-error" : tone === "success" ? "status-success" : "";
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  }
  return payload;
}

function renderDemos() {
  const visible = state.demos.filter((demo) => state.filter === "all" || demo.module === state.filter);
  el.demoList.innerHTML = "";
  for (const demo of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `demo-item${state.selected?.id === demo.id ? " active" : ""}`;
    button.dataset.module = demo.module;
    button.innerHTML = `
      <span>
        <strong>${demo.label}</strong>
        <small>${demo.id}<br>${demo.description}</small>
      </span>
      <span class="mini-badge">${demo.module}</span>
    `;
    button.addEventListener("click", () => selectDemo(demo.id));
    el.demoList.appendChild(button);
  }
}

function renderSelected() {
  if (!state.selected) return;
  el.selectedLabel.textContent = state.selected.label;
  el.selectedDescription.textContent = state.selected.description;
  el.selectedCategory.textContent = `${state.selected.module} · ${state.selected.category}`;
  renderDemos();
}

async function loadSample() {
  if (!state.selected) return;
  try {
    const payload = await fetchJson(`/api/sample?id=${encodeURIComponent(state.selected.id)}`);
    el.jsonInput.value = prettyJson(payload.input);
    setHint(`${state.selected.id} sample loaded`, "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

function selectDemo(id) {
  const demo = state.demos.find((item) => item.id === id);
  if (!demo) return;
  state.selected = demo;
  renderSelected();
  loadSample();
}

function parseInput() {
  try {
    const value = JSON.parse(el.jsonInput.value);
    if (!value || Array.isArray(value) || typeof value !== "object") {
      throw new Error("顶层必须是 JSON object");
    }
    return value;
  } catch (error) {
    throw new Error(`JSON 输入无效：${error.message}`);
  }
}

function renderSummary(payload) {
  const artifacts = payload.artifacts || [];
  const statusClass = ["success", "partial"].includes(payload.status) ? "status-success" : "status-error";
  el.resultSummary.innerHTML = `
    <div><span>${payload.module || "-"}</span><small>Module</small></div>
    <div><span>${payload.operation || "-"}</span><small>Operation</small></div>
    <div><span>${artifacts.length}</span><small>Artifacts</small></div>
  `;
  el.lastStatus.textContent = payload.status || "ok";
  el.lastStatus.className = statusClass;
  el.lastLatency.textContent = payload.latency_ms == null ? "-" : `${payload.latency_ms}ms`;
}

function renderArtifacts(artifacts) {
  el.artifactList.innerHTML = "";
  if (!artifacts || !artifacts.length) {
    el.artifactList.innerHTML = '<div class="artifact-row"><span>暂无产物</span></div>';
    return;
  }
  for (const artifact of artifacts) {
    const row = document.createElement("div");
    row.className = "artifact-row";
    row.innerHTML = `
      <span>${artifact.kind || "file"}</span>
      <code>${artifact.path || "-"}</code>
      <em class="${artifact.exists ? "status-success" : "status-error"}">${artifact.exists ? "ready" : "missing"}</em>
    `;
    el.artifactList.appendChild(row);
  }
}

function renderHistory(records) {
  el.historyList.innerHTML = "";
  if (!records || !records.length) {
    el.historyList.innerHTML = '<div class="history-row"><span>暂无运行记录</span></div>';
    return;
  }
  for (const record of records.slice().reverse()) {
    const row = document.createElement("div");
    const statusClass = ["success", "partial"].includes(record.status) ? "status-success" : "status-error";
    row.className = "history-row";
    row.innerHTML = `
      <span>${record.module || "-"}</span>
      <span>${record.operation || "-"}</span>
      <span class="${statusClass}">${record.status || "-"}</span>
      <span>${record.latency_ms ?? "-"}ms</span>
    `;
    el.historyList.appendChild(row);
  }
}

function renderResult(payload) {
  state.lastResultText = prettyJson(payload);
  el.resultView.textContent = state.lastResultText;
  renderSummary(payload);
  renderArtifacts(payload.artifacts || []);
  if (payload.output_dir) el.outputDir.textContent = payload.output_dir;
  if (payload.history) renderHistory(payload.history);
}

function formatInput() {
  try {
    el.jsonInput.value = prettyJson(parseInput());
    setHint("JSON formatted", "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

async function runDemo() {
  let input;
  try {
    input = parseInput();
  } catch (error) {
    setHint(error.message, "error");
    return;
  }
  setBusy(true);
  setHint("running...");
  try {
    const payload = await fetchJson("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({demo_id: state.selected?.id, input}),
    });
    renderResult(payload);
    setHint(`run completed: ${payload.operation}`, ["success", "partial"].includes(payload.status) ? "success" : "error");
  } catch (error) {
    const payload = {error: {message: error.message}};
    state.lastResultText = prettyJson(payload);
    el.resultView.textContent = state.lastResultText;
    el.lastStatus.textContent = "error";
    el.lastStatus.className = "status-error";
    setHint(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function refreshHistory() {
  try {
    const payload = await fetchJson("/api/history");
    renderHistory(payload.records || []);
  } catch {
    renderHistory([]);
  }
}

async function clearHistory() {
  if (!window.confirm("确定清空 WebUI 运行日志吗？")) return;
  try {
    const payload = await fetchJson("/api/clear_history", {method: "POST"});
    renderHistory(payload.records || []);
    setHint(`cleared ${payload.cleared_records ?? 0} log records`, "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

async function copyResult() {
  if (!state.lastResultText) return;
  try {
    await navigator.clipboard.writeText(state.lastResultText);
    setHint("result copied", "success");
  } catch {
    setHint("copy failed", "error");
  }
}

async function init() {
  const payload = await fetchJson("/api/demos");
  state.demos = payload.demos || [];
  el.totalDemos.textContent = String(payload.counts?.total ?? state.demos.length);
  el.demoCount.textContent = `${payload.counts?.b3 ?? 0} B3 / ${payload.counts?.b5 ?? 0} B5`;
  document.querySelectorAll(".filter").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.filter = button.dataset.filter;
      renderDemos();
    });
  });
  el.sampleBtn.addEventListener("click", loadSample);
  el.formatBtn.addEventListener("click", formatInput);
  el.runBtn.addEventListener("click", runDemo);
  el.copyBtn.addEventListener("click", copyResult);
  el.refreshHistoryBtn.addEventListener("click", refreshHistory);
  el.clearHistoryBtn.addEventListener("click", clearHistory);
  renderDemos();
  if (state.demos.length) selectDemo(state.demos[0].id);
  refreshHistory();
}

init().catch((error) => {
  setHint(error.message, "error");
  el.resultView.textContent = prettyJson({error: error.message});
});
