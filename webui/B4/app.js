const state = {
  config: null,
  report: null,
  history: [],
  lastDetail: null,
};

const scenarioHelp = {
  single_generate: {
    title: "基础 AIMessage",
    subtitle: "读取 model.yaml，绑定 tools_schema，接收 messages，输出标准 AIMessage。",
    metrics: [
      ["model config", "读取 model.yaml 中的 backend、model path、generation 配置"],
      ["tool binding", "本轮开始时接收 tools_schema，并传给 B4 生成流程"],
      ["AIMessage", "输出必须二选一：content 非空且 tool_calls 为空，或 content 为空且 tool_calls 非空"],
    ],
    detail: {
      output: "运行后右侧会展示 ai_message、schema_metadata、raw_model_output.json 路径和 ai_message.json 路径。",
      calculation: "这里不计算成功率，只展示 B4 是否成功解析模型原始输出，以及本轮生成了几个 tool_calls。",
    },
  },
  plan_execute: {
    title: "Plan-and-Execute",
    subtitle: "先生成 plan，再选择工具或专家模型执行 task，最后合成 AIMessage。",
    metrics: [
      ["plan", "是否生成并解析出可执行计划"],
      ["tasks", "task_results 中 status=success 的数量 / task 总数"],
      ["final", "最终是否合成标准 AIMessage"],
    ],
    detail: {
      output: "运行后会展示 plan、task_results、selected_tool / selected_model 和 plan.json 路径。",
      calculation: "任务成功率 = 成功 task 数 / task_results 总数。模型切换信息看每个 task 的 selected_model 字段。",
    },
  },
  schema_compare: {
    title: "Schema Passing 对比",
    subtitle: "同一 messages/tools_schema 下，对比 prompt_injection 与 native_tools。",
    metrics: [
      ["prompt", "把 tools_schema 明文拼进 prompt"],
      ["native", "通过 tokenizer.apply_chat_template(..., tools=...) 传入"],
      ["delta", "native input tokens - prompt input tokens"],
    ],
    detail: {
      output: "运行后会展示两种 schema_passing 的 status、tool_call_names、token 和 fallback 信息。",
      calculation: "same_tool_call_names 表示两种方式选出的工具名序列是否完全一致；token delta 为 native 输入 token 减 prompt 输入 token。",
    },
  },
  batch_eval: {
    title: "多模型批量评估",
    subtitle: "用 20 个样例比较不同模型系列的工具调用成功率与 token 使用量。",
    metrics: [
      ["parse", "result.status == success 的 case 数 / 总 case 数"],
      ["tool", "期望工具名是否都被模型选中"],
      ["call", "期望工具调用是否匹配，exact_args=false 时允许 predicted 多带参数"],
    ],
    detail: {
      output: "运行后展示每个模型的 parse/tool/call 成功率、平均 token 和 case matrix。",
      calculation: "avg_input/output/total_tokens 是每个模型所有 case 的 schema_metadata token 取平均值。",
    },
  },
};

const scenarioMessages = {
  single_generate: "data/messages/b4_2messages_with_multi_tool.json",
  plan_execute: "data/messages/b4_plan_execute_light_text_messages.json",
  schema_compare: "data/messages/b4_1messages_multi_tool_no_tool.json",
};

const scenarioOutputDirs = {
  single_generate: "outputs/B4_GUI/part1",
  plan_execute: "outputs/B4_GUI/part2",
  schema_compare: "outputs/B4_GUI/part3",
  batch_eval: "outputs/B4_GUI/part4",
};

const el = {
  modelCount: document.querySelector("#modelCount"),
  caseCount: document.querySelector("#caseCount"),
  lastStatus: document.querySelector("#lastStatus"),
  schemaBadge: document.querySelector("#schemaBadge"),
  operation: document.querySelector("#operation"),
  messagesPath: document.querySelector("#messagesPath"),
  modelConfigPath: document.querySelector("#modelConfigPath"),
  messagesPathWrap: document.querySelector("#messagesPathWrap"),
  modelConfigWrap: document.querySelector("#modelConfigWrap"),
  batchConfigWrap: document.querySelector("#batchConfigWrap"),
  batchConfigPath: document.querySelector("#batchConfigPath"),
  outdir: document.querySelector("#outdir"),
  outdirWrap: document.querySelector("#outdirWrap"),
  schemaMode: document.querySelector("#schemaMode"),
  schemaModeWrap: document.querySelector("#schemaModeWrap"),
  toolsConfigPath: document.querySelector("#toolsConfigPath"),
  toolsSchemaPath: document.querySelector("#toolsSchemaPath"),
  toolsConfigWrap: document.querySelector("#toolsConfigWrap"),
  toolsSchemaWrap: document.querySelector("#toolsSchemaWrap"),
  toolset: document.querySelector("#toolset"),
  toolsetWrap: document.querySelector("#toolsetWrap"),
  mode: document.querySelector("#mode"),
  modeWrap: document.querySelector("#modeWrap"),
  schemaPassing: document.querySelector("#schemaPassing"),
  schemaPassingWrap: document.querySelector("#schemaPassingWrap"),
  loadConfigBtn: document.querySelector("#loadConfigBtn"),
  formatConfigBtn: document.querySelector("#formatConfigBtn"),
  loadReportBtn: document.querySelector("#loadReportBtn"),
  runBtn: document.querySelector("#runBtn"),
  configHint: document.querySelector("#configHint"),
  jsonEditor: document.querySelector("#jsonEditor"),
  batchDescription: document.querySelector("#batchDescription"),
  batchMode: document.querySelector("#batchMode"),
  inputHint: document.querySelector("#inputHint"),
  copyCommandBtn: document.querySelector("#copyCommandBtn"),
  metricGrid: document.querySelector("#metricGrid"),
  successChart: document.querySelector("#successChart"),
  failureOnly: document.querySelector("#failureOnly"),
  caseHead: document.querySelector("#caseHead"),
  caseBody: document.querySelector("#caseBody"),
  detailView: document.querySelector("#detailView"),
  refreshHistoryBtn: document.querySelector("#refreshHistoryBtn"),
  clearHistoryBtn: document.querySelector("#clearHistoryBtn"),
  outputDir: document.querySelector("#outputDir"),
  historyList: document.querySelector("#historyList"),
};

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
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

function setBusy(isBusy) {
  el.runBtn.disabled = isBusy;
  el.runBtn.textContent = isBusy ? "运行中..." : runButtonText();
}

function setHint(text, tone = "neutral") {
  el.configHint.textContent = text;
  el.configHint.className = tone === "error" ? "hint-text status-error" : tone === "success" ? "hint-text status-success" : "hint-text";
}

function setInputHint(text, tone = "neutral") {
  el.inputHint.textContent = text;
  el.inputHint.className = tone === "error" ? "status-error" : tone === "success" ? "status-success" : "";
}

function parseBatchConfig() {
  try {
    const value = JSON.parse(el.jsonEditor.value);
    if (!value || Array.isArray(value) || typeof value !== "object") {
      throw new Error("顶层必须是 JSON object");
    }
    return value;
  } catch (error) {
    throw new Error(`Batch JSON 无效：${error.message}`);
  }
}

function renderConfigSummary(summary) {
  el.modelCount.textContent = String(summary?.model_count ?? "-");
  el.caseCount.textContent = String(summary?.case_count ?? "-");
  el.batchDescription.textContent = summary?.description || "-";
  el.batchMode.textContent = summary?.mode || "-";
}

function runButtonText() {
  const labels = {
    single_generate: "运行基础 AIMessage",
    plan_execute: "运行 Plan-and-Execute",
    schema_compare: "运行 Schema 对比",
    batch_eval: "运行多模型批量评估",
  };
  return labels[el.operation.value] || "运行 B4 场景";
}

function summarizeConfig(config) {
  const models = Array.isArray(config.models) ? config.models : [];
  const cases = Array.isArray(config.cases) ? config.cases : [];
  return {
    description: config.description || "",
    mode: config.mode || "prompt_json",
    model_count: models.length,
    case_count: cases.length,
  };
}

function parseMessages() {
  try {
    const value = JSON.parse(el.jsonEditor.value);
    if (!Array.isArray(value)) {
      throw new Error("顶层必须是 JSON array");
    }
    return value;
  } catch (error) {
    throw new Error(`Messages JSON 无效：${error.message}`);
  }
}

async function loadDefaults() {
  const payload = await fetchJson("/api/defaults");
  el.batchConfigPath.value = payload.batch_config_path;
  el.messagesPath.value = payload.messages_path;
  el.modelConfigPath.value = payload.model_config_path;
  el.toolsConfigPath.value = payload.tools_config_path;
  el.toolsSchemaPath.value = payload.tools_schema_path || "data/messages/tools_schema.json";
  el.schemaMode.value = payload.schema_mode || "file";
  syncScenarioOutdir();
  state.history = payload.history || [];
  renderHistory();
  updateSchemaMode();
  await loadInput();
}

function setVisible(node, isVisible) {
  if (node) node.hidden = !isVisible;
}

function syncScenarioOutdir() {
  const outdir = scenarioOutputDirs[el.operation.value] || scenarioOutputDirs.single_generate;
  el.outdir.value = outdir;
  el.outputDir.textContent = outdir;
}

function syncOperationView() {
  const operation = el.operation.value;
  const batchMode = operation === "batch_eval";
  const schemaCompareMode = operation === "schema_compare";
  if (!batchMode && scenarioMessages[el.operation.value]) {
    el.messagesPath.value = scenarioMessages[el.operation.value];
  }
  syncScenarioOutdir();
  setVisible(el.batchConfigWrap, batchMode);
  setVisible(el.messagesPathWrap, !batchMode);
  setVisible(el.modelConfigWrap, !batchMode);
  setVisible(el.schemaPassingWrap, !schemaCompareMode);
  setVisible(el.loadReportBtn, batchMode);
  el.failureOnly.checked = false;
  el.runBtn.textContent = runButtonText();
  document.querySelector(".input-panel h2").textContent = batchMode ? "Batch JSON" : "Messages JSON";
  el.batchMode.textContent = el.operation.value;
  updateSchemaMode();
  renderScenarioIntro();
}

async function loadInput() {
  if (el.operation.value === "batch_eval") {
    return loadBatchConfig();
  }
  return loadMessages();
}

async function loadBatchConfig() {
  const path = encodeURIComponent(el.batchConfigPath.value.trim());
  const payload = await fetchJson(`/api/batch_config?path=${path}`);
  state.config = payload.config;
  el.jsonEditor.value = prettyJson(payload.config);
  renderConfigSummary(payload.summary);
  setInputHint(`loaded: ${payload.path}`, "success");
}

async function loadMessages() {
  const path = encodeURIComponent(el.messagesPath.value.trim());
  const payload = await fetchJson(`/api/messages?path=${path}`);
  el.jsonEditor.value = prettyJson(payload.messages);
  el.modelCount.textContent = "1";
  el.caseCount.textContent = String(payload.message_count || 0);
  el.batchDescription.textContent = `${payload.message_count || 0} messages loaded`;
  el.batchMode.textContent = el.operation.value;
  setInputHint(`loaded: ${payload.path}`, "success");
}

function formatInput() {
  try {
    const value = el.operation.value === "batch_eval" ? parseBatchConfig() : parseMessages();
    el.jsonEditor.value = prettyJson(value);
    if (el.operation.value === "batch_eval") {
      renderConfigSummary(summarizeConfig(value));
    }
    setInputHint("JSON formatted", "success");
  } catch (error) {
    setInputHint(error.message, "error");
  }
}

async function runCurrent() {
  if (el.operation.value === "batch_eval") return runBatch();
  if (el.operation.value === "schema_compare") return runSchemaCompare();
  return runGenerate(el.operation.value === "plan_execute");
}

function commonRunPayload() {
  return {
    outdir: el.outdir.value.trim(),
    schema_mode: el.schemaMode.value,
    tools_config_path: el.toolsConfigPath.value.trim(),
    tools_schema_path: el.toolsSchemaPath.value.trim(),
    toolset: el.toolset.value,
    mode: el.mode.value,
    schema_passing: el.schemaPassing.value,
    model_config_path: el.modelConfigPath.value.trim(),
  };
}

async function runGenerate(usePlan) {
  let messages;
  try {
    messages = parseMessages();
  } catch (error) {
    setInputHint(error.message, "error");
    return;
  }
  setBusy(true);
  setHint(usePlan ? "Plan-and-Execute running..." : "AIMessage generation running...");
  try {
    const payload = await fetchJson("/api/run_generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        ...commonRunPayload(),
        messages_path: el.messagesPath.value.trim(),
        messages,
        planning: usePlan ? "plan_and_execute" : "none",
      }),
    });
    state.history = payload.history || [];
    renderHistory();
    renderSingleResult(payload, usePlan ? "Plan-and-Execute" : "AIMessage");
    setHint(`artifact saved: ${payload.raw_output_path}`, "success");
    el.lastStatus.textContent = payload.result.status || "success";
    el.lastStatus.className = payload.result.status === "error" ? "status-error" : "status-success";
  } catch (error) {
    setHint(error.message, "error");
    el.lastStatus.textContent = "error";
    el.lastStatus.className = "status-error";
  } finally {
    setBusy(false);
  }
}

async function runSchemaCompare() {
  let messages;
  try {
    messages = parseMessages();
  } catch (error) {
    setInputHint(error.message, "error");
    return;
  }
  setBusy(true);
  setHint("schema passing comparison running...");
  try {
    const payload = await fetchJson("/api/compare_schema", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        ...commonRunPayload(),
        messages_path: el.messagesPath.value.trim(),
        messages,
        planning: "none",
      }),
    });
    state.history = payload.history || [];
    renderHistory();
    renderCompareResult(payload);
    setHint(`report saved: ${payload.report_path}`, "success");
    el.lastStatus.textContent = "success";
    el.lastStatus.className = "status-success";
  } catch (error) {
    setHint(error.message, "error");
    el.lastStatus.textContent = "error";
    el.lastStatus.className = "status-error";
  } finally {
    setBusy(false);
  }
}

async function runBatch() {
  let config;
  try {
    config = parseBatchConfig();
  } catch (error) {
    setInputHint(error.message, "error");
    return;
  }
  setBusy(true);
  setHint("B4 evaluation running...");
  try {
    const payload = await fetchJson("/api/run_batch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        batch_config_path: el.batchConfigPath.value.trim(),
        batch_config: config,
        ...commonRunPayload(),
        planning: "none",
      }),
    });
    state.config = config;
    state.report = payload.report;
    state.history = payload.history || [];
    el.outputDir.textContent = payload.report_path;
    el.schemaBadge.textContent = payload.schema?.toolset || payload.schema?.source || "schema";
    renderReport();
    renderHistory();
    setHint(`report saved: ${payload.report_path}`, "success");
    el.lastStatus.textContent = "success";
    el.lastStatus.className = "status-success";
  } catch (error) {
    setHint(error.message, "error");
    el.lastStatus.textContent = "error";
    el.lastStatus.className = "status-error";
  } finally {
    setBusy(false);
  }
}

async function loadReport() {
  try {
  const payload = await fetchJson(`/api/report?path=${encodeURIComponent(`${el.outdir.value.trim()}/batch_tool_call_eval_report.json`)}`);
    state.report = payload.report;
    renderReport();
    setHint(`report loaded: ${payload.path}`, "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

function renderSingleResult(payload, title) {
  const result = payload.result || {};
  el.metricGrid.innerHTML = `
    <article class="metric-card best">
      <strong>${escapeHtml(title)}</strong>
      <small>${escapeHtml(result.status || "-")}</small>
      ${metricLine("tools", result.ai_message?.tool_calls?.length || 0, Math.max(1, result.ai_message?.tool_calls?.length || 1), result.ai_message?.tool_calls?.length ? 1 : 0)}
      ${metricLine("plan", result.plan ? 1 : 0, 1, result.plan ? 1 : 0)}
      ${metricLine("tasks", result.task_results?.filter((item) => item.status === "success").length || 0, result.task_results?.length || 1, result.task_results?.length ? (result.task_results.filter((item) => item.status === "success").length / result.task_results.length) : 0)}
    </article>
  `;
  el.successChart.innerHTML = "";
  el.caseHead.innerHTML = "";
  el.caseBody.innerHTML = '<tr><td>单次生成不包含 case matrix</td></tr>';
  el.detailView.textContent = prettyJson({
    status: result.status,
    error: result.error,
    ai_message: result.ai_message,
    plan: result.plan,
    task_results: result.task_results,
    schema_metadata: result.schema_metadata,
    paths: {
      raw_output_path: payload.raw_output_path,
      ai_message_path: payload.ai_message_path,
      plan_path: payload.plan_path,
    },
  });
}

function renderCompareResult(payload) {
  const report = payload.report || {};
  const rows = report.comparison || [];
  el.metricGrid.innerHTML = rows.map((item) => `
    <article class="metric-card">
      <strong>${escapeHtml(item.schema_passing)}</strong>
      <small>${escapeHtml(item.status || "-")}</small>
      ${metricLine("tools", item.tool_call_count || 0, Math.max(1, item.tool_call_count || 1), item.tool_call_count ? 1 : 0)}
      ${metricLine("native", item.native_tools_applied ? 1 : 0, 1, item.native_tools_applied ? 1 : 0)}
      ${metricLine("fallback", item.fallback_to_prompt_injection ? 1 : 0, 1, item.fallback_to_prompt_injection ? 1 : 0)}
    </article>
  `).join("");
  el.successChart.innerHTML = "";
  el.caseHead.innerHTML = "<tr><th>Schema</th><th>Status</th><th>Tool Calls</th><th>Tokens</th></tr>";
  el.caseBody.innerHTML = rows.map((item) => `
    <tr><td>${escapeHtml(item.schema_passing)}</td><td>${escapeHtml(item.status)}</td><td>${escapeHtml((item.tool_call_names || []).join(", ") || "-")}</td><td>${item.input_token_count ?? "-"}</td></tr>
  `).join("");
  el.detailView.textContent = prettyJson(report);
}

function renderReport() {
  const report = state.report;
  if (!report) {
    el.metricGrid.innerHTML = '<div class="metric-card"><strong>暂无报告</strong><small>运行或读取 batch report</small></div>';
    el.successChart.innerHTML = "";
    renderScenarioIntro();
    return;
  }
  el.modelCount.textContent = String(report.total_models ?? report.models?.length ?? "-");
  el.caseCount.textContent = String(report.total_cases ?? "-");
  renderMetrics(report);
  renderChart(report);
  renderCaseMatrix();
  if (report.case_records?.length) {
    state.lastDetail = report.case_records[0];
    renderDetail(state.lastDetail);
  }
}

function renderScenarioIntro() {
  if (state.report && el.operation.value === "batch_eval") return;
  const meta = scenarioHelp[el.operation.value] || scenarioHelp.single_generate;
  el.metricGrid.innerHTML = meta.metrics.map((item, index) => `
    <article class="metric-card ${index === 0 ? "best" : ""}">
      <strong>${escapeHtml(item[0])}</strong>
      <small>${escapeHtml(item[1])}</small>
    </article>
  `).join("");
  el.successChart.innerHTML = "";
  el.caseHead.innerHTML = "<tr><th>展示内容</th><th>说明</th></tr>";
  el.caseBody.innerHTML = `
    <tr><td>${escapeHtml(meta.title)}</td><td>${escapeHtml(meta.subtitle)}</td></tr>
    <tr><td>输出</td><td>${escapeHtml(meta.detail.output)}</td></tr>
    <tr><td>指标口径</td><td>${escapeHtml(meta.detail.calculation)}</td></tr>
  `;
  el.detailView.textContent = prettyJson({
    scenario: meta.title,
    description: meta.subtitle,
    metrics: Object.fromEntries(meta.metrics),
    calculation: meta.detail.calculation,
  });
}

function renderMetrics(report) {
  const models = report.models || [];
  const best = Math.max(0, ...models.map((model) => Number(model.tool_call_success_rate || 0)));
  el.metricGrid.innerHTML = "";
  for (const model of models) {
    const total = model.total_cases || report.total_cases || 0;
    const card = document.createElement("article");
    card.className = `metric-card${Number(model.tool_call_success_rate || 0) === best ? " best" : ""}`;
    card.innerHTML = `
      <strong>${escapeHtml(model.label || model.model_id)}</strong>
      <small>${escapeHtml(model.series || model.model_id)}</small>
      ${metricLine("parse", model.parse_success_count, total, model.parse_success_rate)}
      ${metricLine("tool", model.tool_name_success_count, total, model.tool_name_success_rate)}
      ${metricLine("call", model.tool_call_success_count, total, model.tool_call_success_rate)}
    `;
    el.metricGrid.appendChild(card);
  }
}

function metricLine(label, count, total, rate) {
  const value = Number(rate || 0);
  const tone = value >= 0.75 ? "good" : value < 0.35 ? "bad" : "";
  return `
    <div class="metric-line">
      <span>${label}</span>
      <span class="meter ${tone}"><i style="width:${Math.max(0, Math.min(100, value * 100))}%"></i></span>
      <span>${count || 0}/${total || 0}</span>
    </div>
  `;
}

function renderChart(report) {
  const models = report.models || [];
  const best = Math.max(0, ...models.map((model) => Number(model.tool_call_success_rate || 0)));
  el.successChart.innerHTML = "";
  for (const model of models) {
    const rate = Number(model.tool_call_success_rate || 0);
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `
      <span>${escapeHtml(model.label || model.model_id)}</span>
      <span class="bar-track"><i class="bar-fill ${rate === best ? "best" : rate < 0.3 ? "low" : ""}" style="width:${Math.max(0, Math.min(100, rate * 100))}%"></i></span>
      <span>${Math.round(rate * 100)}%</span>
    `;
    el.successChart.appendChild(row);
  }
}

function renderCaseMatrix() {
  const report = state.report;
  if (!report?.case_records?.length) {
    el.caseHead.innerHTML = "";
    el.caseBody.innerHTML = '<tr><td>暂无 case 明细</td></tr>';
    el.detailView.textContent = "等待运行或读取报告...";
    return;
  }
  const models = report.models || [];
  const grouped = {};
  for (const record of report.case_records) {
    if (!grouped[record.case_id]) grouped[record.case_id] = {case_id: record.case_id, records: {}, expected: record.expected_tool_calls || []};
    grouped[record.case_id].records[record.model_id] = record;
  }
  el.caseHead.innerHTML = `<tr><th>Case</th><th>Expected</th>${models.map((model) => `<th>${escapeHtml(model.label || model.model_id)}</th>`).join("")}</tr>`;
  el.caseBody.innerHTML = "";
  const rows = Object.values(grouped).filter((group) => {
    if (!el.failureOnly.checked) return true;
    return Object.values(group.records).some((record) => !record.tool_call_success);
  });
  if (!rows.length) {
    const colspan = Math.max(1, models.length + 2);
    el.caseBody.innerHTML = `<tr><td colspan="${colspan}">${el.failureOnly.checked ? "当前没有失败 case" : "暂无 case 明细"}</td></tr>`;
    return;
  }
  for (const group of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(group.case_id)}</td>
      <td>${escapeHtml(group.expected.map((call) => call.name).join(", ") || "none")}</td>
      ${models.map((model) => statusCell(group.records[model.model_id])).join("")}
    `;
    tr.addEventListener("click", () => {
      const first = models.map((model) => group.records[model.model_id]).find(Boolean);
      if (first) renderDetail(first);
    });
    el.caseBody.appendChild(tr);
  }
}

function statusCell(record) {
  if (!record) return '<td><span class="badge fail">NA</span></td>';
  const tone = record.tool_call_success ? "ok" : record.tool_name_success ? "mid" : "fail";
  const text = record.tool_call_success ? "OK" : record.tool_name_success ? "ARG" : record.parse_success ? "TOOL" : "JSON";
  return `<td><span class="badge ${tone}">${text}</span></td>`;
}

function renderDetail(record) {
  state.lastDetail = record;
  el.detailView.textContent = prettyJson({
    model: record.model_label || record.model_id,
    case_id: record.case_id,
    status: record.status,
    expected_tool_calls: record.expected_tool_calls,
    predicted_tool_calls: record.predicted_tool_calls,
    match: record.match,
    tokens: {
      input: record.input_token_count,
      output: record.output_token_count,
      total: record.total_token_count,
    },
    error: record.error,
    raw_output_path: record.raw_output_path,
    ai_message_path: record.ai_message_path,
  });
}

function renderHistory() {
  el.historyList.innerHTML = "";
  if (!state.history.length) {
    el.historyList.innerHTML = '<div class="history-row"><span>暂无运行记录</span></div>';
    return;
  }
  for (const record of state.history.slice().reverse()) {
    const row = document.createElement("div");
    const statusClass = record.status === "success" ? "status-success" : "status-error";
    row.className = "history-row";
    row.innerHTML = `
      <span>${record.timestamp || "-"}</span>
      <span class="${statusClass}">${record.status || "-"}</span>
      <span title="${record.report_path || record.error?.message || ""}">${record.report_path || record.error?.message || "-"}</span>
    `;
    el.historyList.appendChild(row);
  }
}

async function refreshHistory() {
  try {
    const payload = await fetchJson("/api/history");
    state.history = payload.records || [];
    renderHistory();
  } catch (error) {
    setHint(error.message, "error");
  }
}

async function clearHistory() {
  if (!window.confirm("确定清空 B4 WebUI 运行日志吗？")) return;
  try {
    const payload = await fetchJson("/api/clear_history", {method: "POST"});
    state.history = payload.records || [];
    renderHistory();
    setHint(`cleared ${payload.cleared_records ?? 0} log records`, "success");
  } catch (error) {
    setHint(error.message, "error");
  }
}

async function copyCommand() {
  const command = [
    "conda activate lab",
    "cd \"D:\\\\大三下\\\\实训\\\\第二阶段\\\\project\\\\agent\\\\code\"",
    "python b4_local_agent_llm.py `",
    `  --tools_schema ../outputs/B4_webui/schema/tools_schema.json \``,
    `  --batch_eval ../${el.batchConfigPath.value.trim()} \``,
    `  --mode ${el.mode.value} \``,
    `  --schema_passing ${el.schemaPassing.value} \``,
    `  --outdir ../${el.outdir.value.trim()}`,
  ].join("\n");
  try {
    await navigator.clipboard.writeText(command);
    setInputHint("command copied", "success");
  } catch {
    setInputHint(command, "error");
  }
}

function buildScenarioCommand() {
  const q = (value) => `"${String(value || "").replace(/"/g, '`"')}"`;
  const outdir = el.outdir.value.trim();
  const cleanOutdir = outdir.replace(/[\\/]+$/, "");
  const generatedSchemaPath = `${cleanOutdir}/schema/tools_schema.json`;
  const toolsSchemaPath = el.schemaMode.value === "file" ? el.toolsSchemaPath.value.trim() : generatedSchemaPath;
  const lines = [
    "conda activate lab",
    'cd "D:\\大三下\\实训\\第二阶段\\project\\agent"',
  ];
  if (el.schemaMode.value === "generate") {
    lines.push(
      "python code\\b3_tool_layer.py `",
      `  --tools_config ${q(el.toolsConfigPath.value.trim())} \``,
      `  --toolset ${el.toolset.value} \``,
      "  --export_schema `",
      `  --outdir ${q(`${cleanOutdir}/schema`)}`,
      "",
    );
  }
  const args = [
    `  --tools_schema ${q(toolsSchemaPath)} \``,
    `  --mode ${el.mode.value} \``,
  ];
  if (el.operation.value === "batch_eval") {
    args.push(`  --batch_eval ${q(el.batchConfigPath.value.trim())} \``);
    args.push(`  --schema_passing ${el.schemaPassing.value} \``);
  } else {
    args.push(`  --model_config ${q(el.modelConfigPath.value.trim())} \``);
    args.push(`  --messages ${q(el.messagesPath.value.trim())} \``);
    if (el.operation.value === "plan_execute") {
      args.push("  --planning plan_and_execute `");
      args.push(`  --schema_passing ${el.schemaPassing.value} \``);
    } else if (el.operation.value === "schema_compare") {
      args.push("  --compare_schema_passing `");
    } else {
      args.push(`  --schema_passing ${el.schemaPassing.value} \``);
    }
  }
  args.push(`  --outdir ${q(outdir)}`);
  return [...lines, "python code\\b4_local_agent_llm.py `", ...args].join("\n");
}

async function copyScenarioCommand() {
  const command = buildScenarioCommand();
  try {
    await navigator.clipboard.writeText(command);
    setInputHint("command copied", "success");
  } catch {
    setInputHint(command, "error");
  }
}

function updateSchemaMode() {
  const fileMode = el.schemaMode.value === "file";
  setVisible(el.toolsSchemaWrap, fileMode);
  setVisible(el.toolsConfigWrap, !fileMode);
  setVisible(el.toolsetWrap, !fileMode);
  el.toolset.disabled = fileMode;
  el.schemaBadge.textContent = fileMode ? "schema file" : el.toolset.value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function bindEvents() {
  el.operation.addEventListener("change", () => {
    state.report = null;
    syncOperationView();
    loadInput();
  });
  el.loadConfigBtn.addEventListener("click", loadInput);
  el.formatConfigBtn.addEventListener("click", formatInput);
  el.runBtn.addEventListener("click", runCurrent);
  el.loadReportBtn.addEventListener("click", loadReport);
  el.copyCommandBtn.addEventListener("click", copyScenarioCommand);
  el.refreshHistoryBtn.addEventListener("click", refreshHistory);
  el.clearHistoryBtn.addEventListener("click", clearHistory);
  el.failureOnly.addEventListener("change", () => {
    if (state.report?.case_records?.length && el.operation.value === "batch_eval") {
      renderCaseMatrix();
    } else {
      renderScenarioIntro();
    }
  });
  el.schemaMode.addEventListener("change", updateSchemaMode);
  el.toolset.addEventListener("change", updateSchemaMode);
  el.jsonEditor.addEventListener("input", () => {
    try {
      if (el.operation.value === "batch_eval") {
        renderConfigSummary(summarizeConfig(parseBatchConfig()));
      } else {
        const messages = parseMessages();
        el.caseCount.textContent = String(messages.length);
      }
    } catch {
      // Let explicit format/run actions surface the parse error.
    }
  });
}

async function init() {
  bindEvents();
  updateSchemaMode();
  syncOperationView();
  await loadDefaults();
  renderReport();
}

init().catch((error) => {
  setHint(error.message, "error");
  el.detailView.textContent = prettyJson({error: error.message});
});
