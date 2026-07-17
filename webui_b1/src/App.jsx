import { useEffect, useMemo, useState } from "react";

const API_HEADERS = { "Content-Type": "application/json" };
const API_BASE = (import.meta.env.VITE_B1_API_BASE || "").replace(/\/+$/, "");

function pretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function compactText(value, max = 220) {
  if (value === null || value === undefined) return "-";
  const text = typeof value === "string" ? value : pretty(value);
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

async function fetchJson(url, options) {
  const path = url.startsWith("/") ? url : `/${url}`;
  const response = await fetch(`${API_BASE}${path}`, options);
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  }
  return payload;
}

function Icon({ name }) {
  const paths = {
    play: "M8 5v14l11-7z",
    stop: "M7 7h10v10H7z",
    refresh: "M18 8a6 6 0 1 0 1.5 5.9M18 8h-5M18 8V3",
    resume: "M5 12h11M12 5l7 7-7 7",
    load: "M12 3v10M8 9l4 4 4-4M5 19h14",
    send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z",
  };
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="icon">
      <path d={paths[name]} />
    </svg>
  );
}

function StatusPill({ value }) {
  const tone = value === "success" ? "success" : value === "running" ? "info" : value ? "warn" : "neutral";
  return <span className={`status-pill ${tone}`}>{value || "not_run"}</span>;
}

function ArchitectureFlow() {
  const nodes = ["用户输入", "B1 Runtime", "B5 Memory", "B3 Tools Schema", "B4 AIMessage", "B3 Tool Execute", "messages / trace / final_answer"];
  return (
    <section className="architecture" aria-label="B1 架构链路">
      {nodes.map((node, index) => (
        <div className="arch-item" key={node}>
          <span>{node}</span>
          {index < nodes.length - 1 ? <i /> : null}
        </div>
      ))}
    </section>
  );
}

function ScenarioNav({ scenarios, selectedId, onSelect }) {
  return (
    <aside className="sidebar" aria-label="B1 验收模块">
      <div className="brand">
        <strong>B1 Agent Runtime</strong>
        <span>验收演示控制台</span>
      </div>
      <nav className="scenario-list">
        {scenarios.map((scenario) => (
          <button
            key={scenario.id}
            type="button"
            className={`scenario-button ${selectedId === scenario.id ? "active" : ""}`}
            onClick={() => onSelect(scenario.id)}
          >
            <span>{scenario.title}</span>
            <small>{scenario.short_description}</small>
          </button>
        ))}
      </nav>
    </aside>
  );
}

function RunPanel({
  scenario,
  inputText,
  setInputText,
  llmMode,
  setLlmMode,
  chatMessages,
  chatInput,
  setChatInput,
  chatCompressDemo,
  onSendChat,
  onResetChat,
  onRun,
  onLoadHistory,
  onStop,
  onResume,
  loading,
  error,
}) {
  if (!scenario) return null;
  if (scenario.kind === "chat") {
    return (
      <ChatPanel
        scenario={scenario}
        llmMode={llmMode}
        setLlmMode={setLlmMode}
        chatMessages={chatMessages}
        chatInput={chatInput}
        setChatInput={setChatInput}
        chatCompressDemo={chatCompressDemo}
        onSendChat={onSendChat}
        onResetChat={onResetChat}
        onLoadHistory={onLoadHistory}
        loading={loading}
        error={error}
      />
    );
  }
  const isCheckpoint = scenario.id === "checkpoint_resume";
  return (
    <section className="run-panel" aria-label="运行控制">
      <div className="section-head">
        <div>
          <h1>{scenario.title}</h1>
          <p>{scenario.description}</p>
        </div>
        <StatusPill value={scenario.kind === "batch" ? "batch" : "integrated"} />
      </div>

      <div className="scenario-meta">
        <div>
          <span>输入</span>
          <strong>{scenario.input_label}</strong>
        </div>
        <div>
          <span>输出</span>
          <strong>{scenario.output_dir}</strong>
        </div>
      </div>

      <div className="control-row" aria-label="运行设置">
        <div className="segmented">
          {["mock", "prompt_json"].map((mode) => (
            <button
              type="button"
              className={llmMode === mode ? "selected" : ""}
              onClick={() => setLlmMode(mode)}
              key={mode}
            >
              {mode}
            </button>
          ))}
        </div>
        <button type="button" className="primary" onClick={() => onRun(false)} disabled={loading}>
          <Icon name="play" />
          运行
        </button>
        <button type="button" onClick={onLoadHistory} disabled={loading}>
          <Icon name="load" />
          读取历史
        </button>
      </div>

      {isCheckpoint ? (
        <div className="checkpoint-controls">
          <button type="button" onClick={() => onRun(true)} disabled={loading}>
            <Icon name="play" />
            后台运行
          </button>
          <button type="button" onClick={onStop} disabled={loading}>
            <Icon name="stop" />
            停止保留 checkpoint
          </button>
          <button type="button" onClick={onResume} disabled={loading}>
            <Icon name="resume" />
            resume 恢复
          </button>
        </div>
      ) : null}

      <label className="editor-label" htmlFor="runtimeInput">
        Runtime Input JSON
      </label>
      <textarea
        id="runtimeInput"
        value={inputText}
        spellCheck="false"
        onChange={(event) => setInputText(event.target.value)}
      />

      {error ? <div className="error-box">{error}</div> : null}
    </section>
  );
}

function ChatPanel({
  scenario,
  llmMode,
  setLlmMode,
  chatMessages,
  chatInput,
  setChatInput,
  chatCompressDemo,
  onSendChat,
  onResetChat,
  onLoadHistory,
  loading,
  error,
}) {
  return (
    <section className="run-panel" aria-label="实时对话">
      <div className="section-head">
        <div>
          <h1>{scenario.title}</h1>
          <p>{scenario.description}</p>
        </div>
        <StatusPill value="chat" />
      </div>

      <div className="scenario-meta">
        <div>
          <span>输入</span>
          <strong>{scenario.input_label}</strong>
        </div>
        <div>
          <span>输出</span>
          <strong>{scenario.output_dir}</strong>
        </div>
      </div>

      <div className="control-row" aria-label="对话设置">
        <div className="segmented">
          {["mock", "prompt_json"].map((mode) => (
            <button
              type="button"
              className={llmMode === mode ? "selected" : ""}
              onClick={() => setLlmMode(mode)}
              key={mode}
            >
              {mode}
            </button>
          ))}
        </div>
        <button type="button" onClick={onLoadHistory} disabled={loading}>
          <Icon name="load" />
          读取历史
        </button>
        <button type="button" onClick={onResetChat} disabled={loading}>
          清空对话
        </button>
      </div>

      <div className="chat-hints">
        <span>/prompt brief_agent</span>
        <span>/prompt-text 只用三句话回答</span>
        <span>/compress-demo</span>
        <strong>{chatCompressDemo ? "压缩演示已开启" : "压缩演示未开启"}</strong>
      </div>

      <div className="chat-window" aria-label="实时对话历史">
        {chatMessages.length ? (
          chatMessages.map((message, index) => (
            <article className={`chat-bubble ${message.role}`} key={`${message.role}-${index}`}>
              <span>{message.role === "assistant" ? "B1" : message.role === "command" ? "命令" : "你"}</span>
              <p>{message.content}</p>
            </article>
          ))
        ) : (
          <div className="empty">输入一条消息开始对话。命令会改变后续运行参数，不会直接假造 B1 输出。</div>
        )}
      </div>

      <form className="chat-input-row" onSubmit={onSendChat}>
        <textarea
          aria-label="对话输入"
          value={chatInput}
          spellCheck="false"
          onChange={(event) => setChatInput(event.target.value)}
          placeholder="输入问题，或输入 /prompt brief_agent、/prompt-text 只用三句话回答、/compress-demo"
        />
        <button type="submit" className="primary" disabled={loading || !chatInput.trim()}>
          <Icon name="send" />
          发送
        </button>
      </form>

      {error ? <div className="error-box">{error}</div> : null}
    </section>
  );
}

function TraceSummary({ result }) {
  const trace = result?.trace;
  const batch = result?.batch_results;
  const metrics = batch
    ? [
        ["total", batch.total],
        ["success", batch.success],
        ["error", batch.error],
        ["elapsed", `${batch.total_elapsed_ms ?? "-"}ms`],
      ]
    : [
        ["status", trace?.status],
        ["llm_calls", trace?.llm_call_count],
        ["tool_rounds", trace?.tool_rounds_used],
        ["turns", trace?.total_turns ?? trace?.turns?.length],
      ];
  return (
    <div className="metric-grid">
      {metrics.map(([label, value]) => (
        <div className="metric" key={label}>
          <span>{label}</span>
          <strong>{value ?? "-"}</strong>
        </div>
      ))}
    </div>
  );
}

function MessageTimeline({ messages }) {
  if (!messages?.length) {
    return <div className="empty">暂无 messages.json，可先运行或读取历史。</div>;
  }
  return (
    <div className="timeline">
      {messages.map((message, index) => (
        <article className={`message-row ${message.role}`} key={`${message.role}-${index}`}>
          <div className="message-role">{message.role}</div>
          <div className="message-body">
            <strong>{message.name || message.tool_call_id || `#${index + 1}`}</strong>
            <p>{compactText(message.content, 280)}</p>
            {message.tool_calls?.length ? <small>tool_calls: {message.tool_calls.map((call) => call.name).join(", ")}</small> : null}
          </div>
        </article>
      ))}
    </div>
  );
}

function BatchTable({ batch }) {
  if (!batch) return <div className="empty">当前模块不是批量任务，或尚未生成 batch_results.json。</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>conversation_id</th>
            <th>status</th>
            <th>elapsed</th>
            <th>outdir</th>
          </tr>
        </thead>
        <tbody>
          {(batch.results || []).map((item) => (
            <tr key={item.conversation_id}>
              <td>{item.conversation_id}</td>
              <td><StatusPill value={item.status} /></td>
              <td>{item.elapsed_ms}ms</td>
              <td>{item.outdir}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EventPanel({ result }) {
  const trace = result?.trace || {};
  const events = [
    ...(trace.compress_events || []).map((event) => ({ type: "compress", ...event })),
    ...(trace.prompt_switch_events || []).map((event) => ({ type: "prompt", ...event })),
  ];
  if (!events.length) return <div className="empty">暂无 compress_events 或 prompt_switch_events。</div>;
  return (
    <div className="event-list">
      {events.map((event, index) => (
        <div className="event-row" key={`${event.type}-${index}`}>
          <span>{event.type}</span>
          <strong>turn {event.at_turn ?? "-"}</strong>
          <small>{pretty(event)}</small>
        </div>
      ))}
    </div>
  );
}

function EvidenceChecklist({ scenario, result }) {
  const trace = result?.trace;
  const batch = result?.batch_results;
  const checkpoint = result?.checkpoint_exists;
  const computed = {
    trace: Boolean(trace),
    final_answer: Boolean(result?.final_answer),
    messages: Boolean(result?.messages?.length),
    turns: Boolean(result?.turn_outputs?.length),
    batch: Boolean(batch),
    checkpoint: Boolean(checkpoint),
    compression: Boolean(trace?.compress_events?.length),
    prompt: Boolean(trace?.prompt_switch_events?.length),
  };
  return (
    <div className="checklist">
      {(scenario?.checks || []).map((check) => (
        <div className="check-row" key={check.key}>
          <span className={computed[check.key] ? "dot ok" : "dot"} />
          <span>{check.label}</span>
        </div>
      ))}
    </div>
  );
}

function EvidenceBoard({ scenario, result, activeTab, setActiveTab }) {
  const tabs = ["Trace", "Messages", "Events", "Batch", "Evidence"];
  const boardStatus = result?.active
    ? "running"
    : result?.trace?.status || (result?.batch_results ? (result.batch_results.error ? "partial" : "success") : "");
  return (
    <section className="evidence-board" aria-label="验收证据">
      <div className="section-head compact">
        <div>
          <h2>验收证据</h2>
          <p>{result?.output_dir || "运行后显示输出目录"}</p>
        </div>
        <StatusPill value={boardStatus} />
      </div>

      <TraceSummary result={result} />

      <div className="answer-preview">
        <span>final_answer.md</span>
        <p>{result?.final_answer || "暂无最终回答。"}</p>
      </div>

      <div className="tabs" role="tablist">
        {tabs.map((tab) => (
          <button type="button" className={activeTab === tab ? "selected" : ""} onClick={() => setActiveTab(tab)} key={tab}>
            {tab}
          </button>
        ))}
      </div>

      <div className="tab-surface">
        {activeTab === "Trace" ? <pre>{pretty(result?.trace || result?.process || {})}</pre> : null}
        {activeTab === "Messages" ? <MessageTimeline messages={result?.messages} /> : null}
        {activeTab === "Events" ? <EventPanel result={result} /> : null}
        {activeTab === "Batch" ? <BatchTable batch={result?.batch_results} /> : null}
        {activeTab === "Evidence" ? <EvidenceChecklist scenario={scenario} result={result} /> : null}
      </div>
    </section>
  );
}

export default function App() {
  const [scenarios, setScenarios] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [inputText, setInputText] = useState("{}");
  const [llmMode, setLlmMode] = useState("mock");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState("Trace");
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatPromptSwitches, setChatPromptSwitches] = useState([]);
  const [chatCompressDemo, setChatCompressDemo] = useState(false);

  const selected = useMemo(
    () => scenarios.find((scenario) => scenario.id === selectedId),
    [scenarios, selectedId],
  );

  useEffect(() => {
    fetchJson("/api/scenarios")
      .then((payload) => {
        setScenarios(payload.scenarios || []);
        const first = payload.scenarios?.[0];
        if (first) {
          setSelectedId(first.id);
          setInputText(pretty(first.sample_input));
        }
      })
      .catch((err) => setError(err.message));
  }, []);

  function selectScenario(id) {
    const next = scenarios.find((scenario) => scenario.id === id);
    setSelectedId(id);
    setInputText(pretty(next?.sample_input || {}));
    setResult(null);
    setError("");
    setActiveTab("Trace");
    if (next?.kind === "chat") {
      resetChat();
    }
  }

  function resetChat() {
    setChatMessages([]);
    setChatInput("");
    setChatPromptSwitches([]);
    setChatCompressDemo(false);
  }

  function parseInput() {
    try {
      return JSON.parse(inputText);
    } catch (err) {
      throw new Error(`JSON 输入无效：${err.message}`);
    }
  }

  async function runScenario(asyncRun = false) {
    if (!selected) return;
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson("/api/run", {
        method: "POST",
        headers: API_HEADERS,
        body: JSON.stringify({
          scenario: selected.id,
          llm_mode: llmMode,
          input: parseInput(),
          async: asyncRun,
        }),
      });
      setResult(payload.result || payload);
      setActiveTab(selected.kind === "batch" ? "Batch" : "Trace");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadHistory() {
    if (!selected) return;
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson(`/api/result?scenario=${encodeURIComponent(selected.id)}`);
      setResult(payload);
      setActiveTab(selected.kind === "batch" ? "Batch" : "Trace");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function stopScenario() {
    if (!selected) return;
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson("/api/stop", {
        method: "POST",
        headers: API_HEADERS,
        body: JSON.stringify({ scenario: selected.id }),
      });
      setResult(payload.result || payload);
      setActiveTab("Evidence");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function resumeScenario() {
    if (!selected) return;
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson("/api/resume", {
        method: "POST",
        headers: API_HEADERS,
        body: JSON.stringify({
          scenario: selected.id,
          llm_mode: llmMode,
          input: parseInput(),
        }),
      });
      setResult(payload.result || payload);
      setActiveTab("Trace");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function buildChatRuntimeInput(nextUserText, nextSwitches, useCompressDemo) {
    const base = JSON.parse(JSON.stringify(selected?.sample_input || {}));
    const previousUserTexts = chatMessages
      .filter((message) => message.role === "user")
      .map((message) => message.content);
    const userTexts = [...previousUserTexts, nextUserText];
    base.conversation_id = "conv_live_chat_001";
    base.user_input = userTexts[0] || nextUserText;
    base.turns = userTexts.slice(1).map((user_input) => ({ user_input }));
    base.save_memory = "none";
    base.prompt_switches = nextSwitches;
    if (useCompressDemo) {
      base._compress_demo = true;
    }
    return base;
  }

  async function sendChat(event) {
    event.preventDefault();
    if (!selected || selected.kind !== "chat") return;
    const text = chatInput.trim();
    if (!text) return;
    setError("");
    setChatInput("");

    if (text.startsWith("/prompt-text")) {
      const promptText = text.slice("/prompt-text".length).trim();
      if (!promptText) {
        setError("用法：/prompt-text 你现在只用三句话回答。");
        return;
      }
      const afterUserTurn = chatMessages.filter((message) => message.role === "user").length;
      const nextSwitches = [
        ...chatPromptSwitches,
        { after_user_turn: afterUserTurn, prompt_text: promptText },
      ];
      setChatPromptSwitches(nextSwitches);
      setChatMessages((messages) => [
        ...messages,
        { role: "command", content: `后续第 ${afterUserTurn + 1} 轮开始使用临时 prompt：${promptText}` },
      ]);
      return;
    }

    if (text.startsWith("/prompt")) {
      const [, promptName] = text.split(/\s+/, 2);
      if (!promptName) {
        setError("用法：/prompt brief_agent");
        return;
      }
      const afterUserTurn = chatMessages.filter((message) => message.role === "user").length;
      const nextSwitches = [
        ...chatPromptSwitches,
        { after_user_turn: afterUserTurn, prompt_path: `../prompts/${promptName}.txt` },
      ];
      setChatPromptSwitches(nextSwitches);
      setChatMessages((messages) => [
        ...messages,
        { role: "command", content: `后续第 ${afterUserTurn + 1} 轮开始切换到 ${promptName}.txt` },
      ]);
      return;
    }

    if (text === "/compress-demo") {
      setChatCompressDemo(true);
      setChatMessages((messages) => [
        ...messages,
        { role: "command", content: "已开启低上下文阈值，后续普通消息将更容易触发 B1 历史压缩。" },
      ]);
      return;
    }

    const nextMessages = [...chatMessages, { role: "user", content: text }];
    setChatMessages(nextMessages);
    setLoading(true);
    try {
      const payload = await fetchJson("/api/run", {
        method: "POST",
        headers: API_HEADERS,
        body: JSON.stringify({
          scenario: selected.id,
          llm_mode: llmMode,
          input: buildChatRuntimeInput(text, chatPromptSwitches, chatCompressDemo),
        }),
      });
      const nextResult = payload.result || payload;
      setResult(nextResult);
      setActiveTab("Messages");
      setChatMessages([
        ...nextMessages,
        { role: "assistant", content: nextResult.final_answer || "本轮没有 final_answer。" },
      ]);
    } catch (err) {
      setError(err.message);
      setChatMessages(nextMessages);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="app-shell">
      <ScenarioNav scenarios={scenarios} selectedId={selectedId} onSelect={selectScenario} />
      <div className="main-area">
        <ArchitectureFlow />
        <div className="workspace">
          <RunPanel
            scenario={selected}
            inputText={inputText}
            setInputText={setInputText}
            llmMode={llmMode}
            setLlmMode={setLlmMode}
            chatMessages={chatMessages}
            chatInput={chatInput}
            setChatInput={setChatInput}
            chatCompressDemo={chatCompressDemo}
            onSendChat={sendChat}
            onResetChat={resetChat}
            onRun={runScenario}
            onLoadHistory={loadHistory}
            onStop={stopScenario}
            onResume={resumeScenario}
            loading={loading}
            error={error}
          />
          <EvidenceBoard scenario={selected} result={result} activeTab={activeTab} setActiveTab={setActiveTab} />
        </div>
      </div>
    </main>
  );
}
