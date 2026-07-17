# 个人模块说明：B4 本地 Agent LLM 决策模块

## 1. 模块定位

我负责的个人部分是团队 Agent 项目中的 B4 模块，核心入口是 `agent/code/b4_local_agent_llm.py`，具体实现拆分在 `agent/code/b4/` 目录下。

B4 在整个 Agent 架构中承担“模型决策层”的职责：接收 B1 传入的对话消息 `messages` 和 B3 提供的工具说明 `tools_schema`，调用 mock 逻辑或本地 LLM，输出统一格式的 `AIMessage`。这个 `AIMessage` 只有两种合法形态：

```json
{
  "role": "assistant",
  "content": "最终回答文本",
  "tool_calls": []
}
```

或者：

```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {
      "id": "call_001",
      "name": "file_reader",
      "args": {"path": "b4_file/agent_intro.txt"}
    }
  ]
}
```

也就是说，B4 负责判断当前轮是直接回答，还是需要调用工具；如果需要工具，就生成标准化的工具调用请求，交给后续模块执行。

## 2. 我完成的主要工作

### 2.1 标准 AIMessage 生成流程

我实现了 B4 的主流程函数 `generate_ai_message`，位于 `agent/code/b4/service.py`。它负责：

- 读取并校验模型配置 `model.yaml`。
- 校验输入消息格式和工具 schema。
- 支持 `mock` 和 `prompt_json` 两种运行模式。
- 将模型原始输出解析成统一的 `AIMessage`。
- 保存调试产物，包括 `raw_model_output.json`、`ai_message.json` 和 `llm_run_log.jsonl`。

其中 `mock` 模式不加载真实模型，主要用于无 GPU、无模型或团队联调时快速跑通 B1-B3-B4-B5 链路；`prompt_json` 模式会基于 `transformers` 加载本地模型，进行真实推理。

### 2.2 模型输出解析与容错

在 `agent/code/b4/generation.py` 中，我实现了模型输出处理逻辑。由于小模型经常不能严格输出 JSON，我加入了多种解析兼容能力：

- 直接解析标准 JSON。
- 兼容被 ```json 代码块包住的 JSON。
- 兼容 JSON 后面多输出反引号的情况。
- 从不完整输出中抢救 `tool_calls` 片段。
- 兼容部分模型输出的 `<tool_call>...</tool_call>` 风格工具调用。
- 校验 `content` 和 `tool_calls` 必须二选一，避免既回答又请求工具的歧义结果。
- 校验工具调用 id 唯一，保证后续 B3 能正确关联 ToolMessage。

这部分工作的作用是提升 B4 在真实本地模型下的稳定性，让模型即使有轻微格式漂移，也尽量能被转成项目统一的数据结构。

### 2.3 工具 schema 传递方式实验

B4 支持两种工具 schema 传递方式：

- `prompt_injection`：把 tools schema 直接拼接进 prompt。
- `native_tools`：通过 `tokenizer.apply_chat_template(..., tools=...)` 传给模型。

相关逻辑在 `generation.py` 的 `_apply_chat_template_with_schema_passing` 和 `service.py` 的 `compare_schema_passing_methods` 中实现。

如果当前 tokenizer 不支持 native tools，B4 会自动 fallback 到 prompt 注入，并在 `schema_metadata` 中记录：

- 是否请求 native tools。
- native tools 是否真实生效。
- 是否发生 fallback。
- fallback 原因。
- 输入 token、输出 token 和总 token 数。

这部分用于比较不同 schema 传递方式对工具调用准确率和 token 开销的影响。

### 2.4 Plan-and-Execute 扩展

除了基础的“生成一次 AIMessage”流程，我还实现了 B4 的 `plan_and_execute` 扩展，主要在 `agent/code/b4/planning.py` 中。

该流程分为四步：

1. 先让模型根据用户请求生成可执行计划 `plan`。
2. 对计划进行清洗，去掉“输出最终答案”“结束流程”等不应执行的元任务，并限制任务数量。
3. 为每个 task 选择执行资源：如果适合真实工具，就选择 B3 工具；如果适合模型任务，就选择专家模型。
4. 按依赖顺序执行 task，并把 `task_results` 综合成最终 `AIMessage`。

资源选择部分使用轻量规则打分，综合考虑：

- planner 提示的 `task_tools`。
- task 名称和描述中的关键词。
- 参数完整性，例如是否有 `path`、`expression`、`query`。
- 文件后缀，例如 `.txt` 适合 `file_reader`，`.csv` 适合 `table_analyzer`，`.pdf` 适合 `document_reader`。
- `expert_models.yaml` 中注册的专家模型能力。

因此，B4 在扩展模式下不仅能决定“要不要调用工具”，还能把复杂请求拆成多个 task，选择工具或专家模型执行，再合成最终回答。

### 2.5 与 B3 工具层和专家模型的衔接

B4 的 plan-and-execute 不直接重复实现工具能力，而是复用 B3 的 `execute_tool_calls`。这样可以保证工具 schema、参数修复、缓存、日志和错误格式都与团队工具层保持一致。

对于不需要真实工具的抽象任务，例如文本总结、改写、意图分类、代码生成、代码解释等，B4 会根据 `configs/expert_models.yaml` 选择专家模型执行。当前配置中主要包括：

- `light_text_worker`：处理轻量文本任务。
- `code_worker`：处理代码生成、解释、调试和单元测试编写任务。

这让 B4 具备“工具任务走 B3，模型任务走专家模型”的调度能力。

### 2.6 批量评估与实验报告

我在 `agent/code/b4/evaluation.py` 中实现了离线评估功能，用于衡量模型工具调用效果。评估内容包括：

- 模型输出是否能成功解析成标准 `AIMessage`。
- 工具名是否选对。
- 工具调用参数是否匹配预期。
- 多模型、多 case 的工具调用成功率。
- prompt 注入和 native tools 两种 schema 传递方式的 token 开销对比。

对应 CLI 参数包括：

- `--batch_eval`
- `--schema_passing_batch_eval`
- `--compare_schema_passing`

评估结果会输出为：

- `batch_tool_call_eval_report.json`
- `batch_tool_call_eval_summary.md`
- `schema_passing_batch_report.json`
- `schema_passing_batch_summary.md`

这些文件可以直接用于实验分析和展示。

### 2.7 B4 WebUI 展示

我还实现了 B4 的简单 WebUI 服务端和前端页面：

- 后端入口：`agent/code/b4_webui_server.py`
- 前端目录：`agent/webui/B4/`

WebUI 支持四类场景：

- 基础 AIMessage 生成。
- Plan-and-Execute 执行。
- Schema Passing 对比。
- 多模型批量评估。

前端可以展示模型数量、case 数量、解析成功率、工具名成功率、完整 tool call 成功率、token 统计、case matrix、运行历史和详细 JSON 结果。这样 B4 不只是命令行模块，也有可视化的演示入口。

## 3. 模块文件结构

| 文件 | 作用 |
|---|---|
| `agent/code/b4_local_agent_llm.py` | 兼容旧入口，保留给 B1、B5、测试脚本和命令行继续导入 |
| `agent/code/b4/service.py` | B4 核心服务入口，提供 `generate_ai_message` 和 schema passing 对比 |
| `agent/code/b4/generation.py` | 模型加载、prompt 构造、mock 生成、模型输出解析和 schema 传递 |
| `agent/code/b4/planning.py` | plan-and-execute、资源选择、任务执行、结果合成 |
| `agent/code/b4/evaluation.py` | 批量工具调用评估和 schema passing 批量对比 |
| `agent/code/b4/cli.py` | 命令行参数解析和任务分发 |
| `agent/code/b4_plan_executor.py` | 独立 plan-and-execute demo 入口 |
| `agent/code/b4_webui_server.py` | B4 WebUI 的 HTTP 服务和 API |
| `agent/webui/B4/` | B4 可视化页面 |
| `agent/data/messages/b4/` | B4 基础、批量、多工具、plan-and-execute 样例 |
| `agent/data/b4_file/` | B4 演示用本地文件、表格和文档 |

## 4. 与团队其他模块的关系

### 与 B1 的关系

B1 是 Agent 总控，负责维护对话循环。B1 调用 B4 的 `generate_ai_message` 后，会根据返回的标准 `AIMessage` 决定下一步：

- 如果 `tool_calls` 非空，就交给 B3 执行工具。
- 如果 `content` 非空，就作为最终回答。

B4 因此是 B1 运行循环中的“LLM 决策节点”。

### 与 B3 的关系

B3 提供工具 schema 和工具执行能力。B4 基础模式只生成 tool calls，不执行工具；plan-and-execute 扩展中会复用 B3 的执行函数来完成工具型 task。

这种设计保证了工具能力仍然由 B3 统一管理，B4 只负责选择和调度。

### 与 B5 的关系

B5 负责记忆检索和保存。B4 不直接管理 memory，但 B1 可以把 B5 检索到的记忆注入到 `messages` 中，再交给 B4 生成回答或工具调用。因此 B4 能消费带有 memory 上下文的对话。

## 5. 常用运行方式

从 `agent/code` 目录运行基础 B4：

```bash
python b4_local_agent_llm.py \
  --model_config ../configs/model.yaml \
  --messages ../data/messages/messages_no_tool.json \
  --tools_schema ../data/messages/tools_schema_basic.json \
  --mode mock \
  --outdir ../outputs/B4_llm/mock_demo
```

运行真实本地模型：

```bash
python b4_local_agent_llm.py \
  --model_config ../configs/model.yaml \
  --messages ../data/messages/messages_no_tool.json \
  --tools_schema ../data/messages/tools_schema_basic.json \
  --mode prompt_json \
  --outdir ../outputs/B4_llm/no_tool_real
```

运行 Plan-and-Execute：

```bash
python b4_local_agent_llm.py \
  --model_config ../configs/model.yaml \
  --messages ../data/messages/b4_plan_execute_light_text_messages.json \
  --tools_schema ../data/messages/tools_schema.json \
  --mode mock \
  --planning plan_and_execute \
  --tools_config ../configs/tools.yaml \
  --toolset extended_tools \
  --outdir ../outputs/B4_plan_execute
```

运行 schema passing 对比：

```bash
python b4_local_agent_llm.py \
  --model_config ../configs/model.yaml \
  --messages ../data/messages/b4_1messages_multi_tool_no_tool.json \
  --tools_schema ../data/messages/tools_schema.json \
  --mode mock \
  --compare_schema_passing \
  --outdir ../outputs/B4_schema_compare
```

启动 B4 WebUI：

```bash
python b4_webui_server.py --host 127.0.0.1 --port 18088
```

## 6. 输出产物说明

B4 运行后主要关注以下文件：

| 文件 | 说明 |
|---|---|
| `ai_message.json` | 最终交给 B1 或后续模块使用的标准 AIMessage |
| `raw_model_output.json` | 调试主文件，包含原始模型输出、解析结果、错误和 schema 元数据 |
| `llm_run_log.jsonl` | B4 多次运行的追加日志 |
| `plan.json` | 启用 plan-and-execute 时的计划、资源选择和任务执行记录 |
| `batch_tool_call_eval_report.json` | 批量评估完整报告 |
| `schema_passing_batch_report.json` | schema 传递方式对比报告 |

其中 `raw_model_output.json.status = success` 只表示 B4 成功生成了合法 `AIMessage`，不一定表示 plan 中每个 task 都成功。plan-and-execute 的任务是否成功，需要看 `plan.json` 中的 `task_results`。

## 7. 个人模块亮点

我这部分工作的重点不只是“调用本地模型”，而是把本地模型输出包装成团队 Agent 可以稳定消费的决策接口。主要亮点包括：

- 统一了 `AIMessage` 格式，让 B1、B3 可以稳定对接。
- 提供 mock 模式，降低团队联调对 GPU 和本地模型的依赖。
- 增强了模型 JSON 输出解析和容错能力。
- 支持多工具调用，一轮中可以生成多个互相独立的 tool calls。
- 实现 plan-and-execute，把复杂请求拆成 task，并自动选择 B3 工具或专家模型。
- 支持 prompt 注入和 native tools 两种 schema 传递方式，并记录 token 与 fallback 信息。
- 提供批量评估能力，可以量化模型的工具调用准确率。
- 提供 WebUI，可视化展示 B4 的单次生成、规划执行、schema 对比和批量评估结果。

## 8. 当前边界和后续可改进点

当前 B4 已经能支撑团队项目的主要演示和联调，但仍有一些可以继续完善的方向：

- plan 生成仍依赖小模型的 JSON 稳定性，虽然已有 fallback，但真实模型下仍可能出现格式不完整。
- 资源选择目前主要是规则打分，可以进一步引入更细粒度的语义匹配。
- 专家模型加载成本较高，后续可以做更完善的缓存和异步执行。
- WebUI 已能展示核心指标，但还可以加入更细的错误筛选和报告导出能力。

总体来说，B4 的作用是把“本地 LLM 的不稳定自然语言输出”转换成“Agent 系统可执行、可记录、可评估的结构化决策”。它连接了 B1 的对话控制、B3 的工具执行和本地模型能力，是整个团队 Agent 项目中负责智能决策与工具调用规划的核心模块。
