const state = {
  skills: [],
  selected: null,
  filter: "all",
  lastResultText: "",
};

const el = {
  totalSkills: document.querySelector("#totalSkills"),
  skillCount: document.querySelector("#skillCount"),
  skillList: document.querySelector("#skillList"),
  selectedLabel: document.querySelector("#selectedLabel"),
  selectedDescription: document.querySelector("#selectedDescription"),
  selectedCategory: document.querySelector("#selectedCategory"),
  jsonInput: document.querySelector("#jsonInput"),
  inputHint: document.querySelector("#inputHint"),
  runBtn: document.querySelector("#runBtn"),
  normalSampleBtn: document.querySelector("#normalSampleBtn"),
  errorSampleBtn: document.querySelector("#errorSampleBtn"),
  liveSampleBtn: document.querySelector("#liveSampleBtn"),
  formatBtn: document.querySelector("#formatBtn"),
  resultView: document.querySelector("#resultView"),
  resultSummary: document.querySelector("#resultSummary"),
  lastStatus: document.querySelector("#lastStatus"),
  lastLatency: document.querySelector("#lastLatency"),
  copyBtn: document.querySelector("#copyBtn"),
  refreshHistoryBtn: document.querySelector("#refreshHistoryBtn"),
  clearHistoryBtn: document.querySelector("#clearHistoryBtn"),
  historyList: document.querySelector("#historyList"),
  outputDir: document.querySelector("#outputDir"),
};

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function setBusy(isBusy) {
  el.runBtn.disabled = isBusy;
  el.runBtn.textContent = isBusy ? "执行中..." : "执行 Skill";
}

function setHint(text, tone = "neutral") {
  el.inputHint.textContent = text;
  el.inputHint.className = tone === "error" ? "status-error" : tone === "success" ? "status-success" : "";
}

function renderSkills() {
  const visible = state.skills.filter((skill) => state.filter === "all" || skill.category === state.filter);
  el.skillList.innerHTML = "";
  for (const skill of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `skill-item${state.selected?.name === skill.name ? " active" : ""}`;
    button.dataset.category = skill.category;
    button.innerHTML = `
      <span>
        <strong>${skill.label}</strong>
        <small>${skill.name}<br>${skill.description}</small>
      </span>
      <span class="mini-badge">${skill.category}</span>
    `;
    button.addEventListener("click", () => selectSkill(skill.name));
    el.skillList.appendChild(button);
  }
}

function renderSelected() {
  if (!state.selected) return;
  el.selectedLabel.textContent = state.selected.label;
  el.selectedDescription.textContent = state.selected.description;
  el.selectedCategory.textContent = state.selected.category;
  el.liveSampleBtn.hidden = !state.selected.live;
  renderSkills();
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const message = payload?.error?.message || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function loadSample(kind = "normal") {
  if (!state.selected) return;
  try {
    const payload = await fetchJson(`/api/sample?skill=${encodeURIComponent(state.selected.name)}&kind=${kind}`);
    el.jsonInput.value = prettyJson(payload.input);
    setHint(`${state.selected.name} / ${kind} sample loaded`, "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

function selectSkill(name) {
  const skill = state.skills.find((item) => item.name === name);
  if (!skill) return;
  state.selected = skill;
  renderSelected();
  loadSample("normal");
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

function renderSummary(result) {
  const statusClass = result.status === "success" ? "status-success" : "status-error";
  const errorCode = result.error?.code || "-";
  el.resultSummary.innerHTML = `
    <div>
      <span>${result.skill_name}</span>
      <small>Skill</small>
    </div>
    <div>
      <span class="${statusClass}">${result.status}</span>
      <small>Status</small>
    </div>
    <div>
      <span>${errorCode}</span>
      <small>Error Code</small>
    </div>
  `;
  el.lastStatus.textContent = result.status;
  el.lastStatus.className = statusClass;
  el.lastLatency.textContent = `${result.latency_ms}ms`;
}

function renderResult(payload) {
  const result = payload.result || payload;
  state.lastResultText = prettyJson(payload);
  el.resultView.textContent = state.lastResultText;
  renderSummary(result);
  if (payload.output_dir) {
    el.outputDir.textContent = payload.output_dir;
  }
  if (payload.history) {
    renderHistory(payload.history);
  }
}

function renderHistory(records) {
  el.historyList.innerHTML = "";
  if (!records.length) {
    el.historyList.innerHTML = '<div class="history-row"><span>暂无运行记录</span></div>';
    return;
  }
  for (const record of records.slice().reverse()) {
    const row = document.createElement("div");
    const statusClass = record.status === "success" ? "status-success" : "status-error";
    row.className = "history-row";
    row.innerHTML = `
      <span title="${record.skill_name || ""}">${record.skill_name || "-"}</span>
      <span class="${statusClass}">${record.status || "-"}</span>
      <span>${record.latency_ms ?? "-"}ms</span>
    `;
    el.historyList.appendChild(row);
  }
}

async function refreshHistory() {
  try {
    const payload = await fetchJson("/api/history");
    renderHistory(payload.records || []);
  } catch (error) {
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

async function runSkill() {
  if (!state.selected) return;
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
      body: JSON.stringify({skill: state.selected.name, input}),
    });
    renderResult(payload);
    setHint(`result saved: ${payload.result_path}`, payload.result.status === "success" ? "success" : "error");
  } catch (error) {
    const payload = {error: {message: error.message}};
    state.lastResultText = prettyJson(payload);
    el.resultView.textContent = state.lastResultText;
    setHint(error.message, "error");
    el.lastStatus.textContent = "error";
    el.lastStatus.className = "status-error";
  } finally {
    setBusy(false);
  }
}

function formatInput() {
  try {
    const input = parseInput();
    el.jsonInput.value = prettyJson(input);
    setHint("JSON formatted", "success");
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
  const payload = await fetchJson("/api/skills");
  state.skills = payload.skills || [];
  el.totalSkills.textContent = String(payload.counts?.total ?? state.skills.length);
  el.skillCount.textContent = `${payload.counts?.basic ?? 0} 基础 / ${payload.counts?.extended ?? 0} 扩展`;
  document.querySelectorAll(".filter").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.filter = button.dataset.filter;
      renderSkills();
    });
  });
  el.normalSampleBtn.addEventListener("click", () => loadSample("normal"));
  el.errorSampleBtn.addEventListener("click", () => loadSample("error"));
  el.liveSampleBtn.addEventListener("click", () => loadSample("live"));
  el.formatBtn.addEventListener("click", formatInput);
  el.runBtn.addEventListener("click", runSkill);
  el.copyBtn.addEventListener("click", copyResult);
  el.refreshHistoryBtn.addEventListener("click", refreshHistory);
  el.clearHistoryBtn.addEventListener("click", clearHistory);
  renderSkills();
  if (state.skills.length) {
    selectSkill(state.skills[0].name);
  }
  refreshHistory();
}

init().catch((error) => {
  setHint(error.message, "error");
  el.resultView.textContent = prettyJson({error: error.message});
});
