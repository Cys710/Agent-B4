const state = {
  messages: [],
  lastPayload: null,
  activeTab: "summary",
};

const el = {
  newChatBtn: document.querySelector("#newChatBtn"),
  conversationId: document.querySelector("#conversationId"),
  llmMode: document.querySelector("#llmMode"),
  toolset: document.querySelector("#toolset"),
  enablePlan: document.querySelector("#enablePlan"),
  useGlobalMemory: document.querySelector("#useGlobalMemory"),
  memoryRoot: document.querySelector("#memoryRoot"),
  refreshMemoryRootsBtn: document.querySelector("#refreshMemoryRootsBtn"),
  newMemoryRootGroup: document.querySelector("#newMemoryRootGroup"),
  newMemoryRoot: document.querySelector("#newMemoryRoot"),
  memoryMode: document.querySelector("#memoryMode"),
  memoryTopK: document.querySelector("#memoryTopK"),
  maxTurns: document.querySelector("#maxTurns"),
  selectedMemory: document.querySelector("#selectedMemory"),
  saveMemory: document.querySelector("#saveMemory"),
  historyList: document.querySelector("#historyList"),
  clearHistoryBtn: document.querySelector("#clearHistoryBtn"),
  statusText: document.querySelector("#statusText"),
  latencyText: document.querySelector("#latencyText"),
  turnText: document.querySelector("#turnText"),
  chatMessages: document.querySelector("#chatMessages"),
  chatForm: document.querySelector("#chatForm"),
  messageInput: document.querySelector("#messageInput"),
  sendBtn: document.querySelector("#sendBtn"),
  summaryView: document.querySelector("#summaryView"),
  jsonView: document.querySelector("#jsonView"),
};

function prettyJson(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  }
  return payload;
}

function renderMemoryRootOptions(memoryRoots, preferredValue) {
  const current = preferredValue || el.memoryRoot.value;
  el.memoryRoot.innerHTML = "";
  for (const root of memoryRoots || []) {
    const option = document.createElement("option");
    option.value = root.name;
    option.textContent = root.name;
    el.memoryRoot.appendChild(option);
  }
  const newOption = document.createElement("option");
  newOption.value = "__new__";
  newOption.textContent = "新建 memory 文件夹";
  el.memoryRoot.appendChild(newOption);
  if (current && [...el.memoryRoot.options].some((item) => item.value === current)) {
    el.memoryRoot.value = current;
  }
  el.newMemoryRootGroup.hidden = el.memoryRoot.value !== "__new__";
}

async function refreshMemoryRoots(preferredValue) {
  const config = await fetchJson("/api/config");
  renderMemoryRootOptions(config.memory_roots || [], preferredValue);
  if (config.history) renderHistory(config.history || []);
  return config;
}

function getOptions() {
  const isNewMemoryRoot = el.memoryRoot.value === "__new__";
  return {
    llm_mode: el.llmMode.value,
    toolset: el.toolset.value,
    enable_plan: el.enablePlan.checked,
    use_global_memory: el.useGlobalMemory.checked,
    memory_root_mode: isNewMemoryRoot ? "new" : "existing",
    memory_root: isNewMemoryRoot ? "" : el.memoryRoot.value,
    new_memory_root: isNewMemoryRoot ? el.newMemoryRoot.value : "",
    memory_mode: el.memoryMode.value,
    memory_top_k: Number(el.memoryTopK.value || 3),
    max_turns: Number(el.maxTurns.value || 3),
    selected_memory_ids: el.selectedMemory.value,
    save_memory: el.saveMemory.value,
  };
}

function setBusy(isBusy) {
  el.sendBtn.disabled = isBusy;
  el.messageInput.disabled = isBusy;
  el.sendBtn.textContent = isBusy ? "运行中" : "发送";
  if (isBusy) {
    el.statusText.textContent = "Running";
  }
}

function renderChat() {
  el.chatMessages.innerHTML = "";
  if (!state.messages.length) {
    el.chatMessages.innerHTML = `
      <div class="empty-state">
        <strong>输入问题开始完整链路演示</strong><br>
        建议用 mock 模式先验收流程；打开 Plan 或 Memory 后，右侧会显示对应 trace、plan 文件和 selected memory。
      </div>
    `;
    return;
  }
  for (const message of state.messages) {
    const item = document.createElement("div");
    item.className = `message ${message.role}`;
    item.innerHTML = `
      <div class="avatar">${message.role === "user" ? "U" : "A"}</div>
      <div class="bubble">${escapeHtml(message.content)}</div>
    `;
    el.chatMessages.appendChild(item);
  }
  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
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
      <span><strong>${escapeHtml(record.conversation_id || "-")}</strong><small>${escapeHtml(record.output_dir || "-")}</small></span>
      <span class="${statusClass}">${escapeHtml(record.status || "-")}</span>
    `;
    el.historyList.appendChild(row);
  }
}

function summaryCard(label, value, cls = "") {
  return `<div class="summary-card"><strong class="${cls}">${escapeHtml(value ?? "-")}</strong><span>${escapeHtml(label)}</span></div>`;
}

function renderSummary(payload) {
  const summary = payload?.summary || {};
  const statusClass = ["success", "partial"].includes(summary.status) ? "status-success" : "status-error";
  el.summaryView.hidden = false;
  el.jsonView.hidden = true;
  el.summaryView.innerHTML = `
    <div class="summary-grid">
      ${summaryCard("conversation", summary.conversation_id)}
      ${summaryCard("status", summary.status, statusClass)}
      ${summaryCard("llm mode", summary.llm_mode)}
      ${summaryCard("plan", summary.plan)}
      ${summaryCard("memory", summary.memory_mode)}
      ${summaryCard("memory folder", summary.memory_folder)}
      ${summaryCard("tool rounds", summary.tool_rounds_used)}
      ${summaryCard("llm calls", summary.llm_call_count)}
      ${summaryCard("elapsed", summary.elapsed_ms == null ? "-" : `${summary.elapsed_ms}ms`)}
    </div>
  `;
}

function renderArtifacts(payload) {
  const artifacts = payload?.artifacts || [];
  el.summaryView.hidden = false;
  el.jsonView.hidden = true;
  if (!artifacts.length) {
    el.summaryView.innerHTML = '<div class="artifact-row"><span>暂无文件</span></div>';
    return;
  }
  el.summaryView.innerHTML = `<div class="artifact-list">${artifacts.map((artifact) => `
    <div class="artifact-row">
      <span>${escapeHtml(artifact.kind || "file")}</span>
      <code title="${escapeHtml(artifact.path || "-")}">${escapeHtml(artifact.path || "-")}</code>
      <strong class="${artifact.exists ? "status-success" : "status-error"}">${artifact.exists ? "ready" : "missing"}</strong>
    </div>
  `).join("")}</div>`;
}

function renderJson(value) {
  el.summaryView.hidden = true;
  el.jsonView.hidden = false;
  el.jsonView.textContent = prettyJson(value);
}

function renderDebug() {
  const payload = state.lastPayload;
  if (!payload) {
    renderSummary({summary: {status: "Ready"}});
    return;
  }
  if (state.activeTab === "summary") renderSummary(payload);
  if (state.activeTab === "trace") renderJson(payload.trace);
  if (state.activeTab === "messages") renderJson(payload.messages);
  if (state.activeTab === "memory") renderJson(payload.selected_memory);
  if (state.activeTab === "artifacts") renderArtifacts(payload);
}

function updateTopStatus(payload) {
  const summary = payload?.summary || {};
  el.statusText.textContent = summary.status || "Ready";
  el.statusText.className = ["success", "partial"].includes(summary.status) ? "status-success" : summary.status ? "status-error" : "";
  el.latencyText.textContent = summary.elapsed_ms == null ? "-" : `${summary.elapsed_ms}ms`;
  el.turnText.textContent = String(summary.total_turns ?? state.messages.filter((item) => item.role === "user").length);
}

async function sendMessage(event) {
  event.preventDefault();
  const text = el.messageInput.value.trim();
  if (!text) return;
  state.messages.push({role: "user", content: text});
  el.messageInput.value = "";
  renderChat();
  setBusy(true);
  try {
    const payload = await fetchJson("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        conversation_id: el.conversationId.value.trim() || "webui_full_demo",
        messages: state.messages,
        options: getOptions(),
      }),
    });
    state.lastPayload = payload;
    state.messages.push({role: "assistant", content: payload.assistant_message || ""});
    if (payload.summary?.memory_folder) {
      await refreshMemoryRoots(payload.summary.memory_folder);
    }
    updateTopStatus(payload);
    renderHistory(payload.history || []);
    renderChat();
    renderDebug();
  } catch (error) {
    state.messages.push({role: "assistant", content: `运行失败：${error.message}`});
    el.statusText.textContent = "error";
    el.statusText.className = "status-error";
    renderChat();
  } finally {
    setBusy(false);
    el.messageInput.focus();
  }
}

function newChat() {
  state.messages = [];
  state.lastPayload = null;
  el.statusText.textContent = "Ready";
  el.statusText.className = "";
  el.latencyText.textContent = "-";
  el.turnText.textContent = "0";
  renderChat();
  renderDebug();
}

async function clearHistory() {
  const payload = await fetchJson("/api/clear_history", {method: "POST"});
  renderHistory(payload.records || []);
}

async function init() {
  const config = await fetchJson("/api/config");
  const defaults = config.defaults || {};
  el.conversationId.value = defaults.conversation_id || "webui_full_demo";
  el.llmMode.value = defaults.llm_mode || "mock";
  el.toolset.value = defaults.toolset || "basic_tools";
  el.memoryMode.value = defaults.memory_mode || "off";
  renderMemoryRootOptions(config.memory_roots || [], defaults.memory_root);
  el.memoryTopK.value = defaults.memory_top_k || 3;
  el.maxTurns.value = defaults.max_turns || 3;
  el.enablePlan.checked = Boolean(defaults.enable_plan);
  el.saveMemory.value = defaults.save_memory || "none";
  renderHistory(config.history || []);
  renderChat();
  renderDebug();

  el.chatForm.addEventListener("submit", sendMessage);
  el.newChatBtn.addEventListener("click", newChat);
  el.clearHistoryBtn.addEventListener("click", clearHistory);
  el.refreshMemoryRootsBtn.addEventListener("click", () => refreshMemoryRoots());
  el.memoryRoot.addEventListener("change", () => {
    el.newMemoryRootGroup.hidden = el.memoryRoot.value !== "__new__";
    if (el.memoryRoot.value === "__new__" && !el.newMemoryRoot.value) {
      el.newMemoryRoot.value = "memory_new_demo";
    }
  });
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.activeTab = button.dataset.tab;
      renderDebug();
    });
  });
  el.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      el.chatForm.requestSubmit();
    }
  });
}

init().catch((error) => {
  el.statusText.textContent = "error";
  el.statusText.className = "status-error";
  el.summaryView.textContent = error.message;
});
