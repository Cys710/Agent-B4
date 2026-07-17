# B4 结果文件阅读说明

本文说明 B4 独立运行、B4 plan-and-execute、B3 工具执行以及 B1 集成运行时常见结果文件的含义。阅读输出时建议先看“入口结果”，再看“过程记录”。

## 1. B4 独立运行输出

B4 独立运行通常由命令生成：

```bash
python b4_local_agent_llm.py \
  --model_config ../configs/model.yaml \
  --messages ../data/messages/messages_no_tool.json \
  --tools_schema ../data/messages/tools_schema_basic.json \
  --mode prompt_json \
  --outdir ../outputs/B4_llm/<case>
```

常见文件：

| 文件 | 作用 | 重点字段 |
|---|---|---|
| `ai_message.json` | B4 最终交给上游模块使用的标准 AIMessage。 | `content`、`tool_calls` |
| `raw_model_output.json` | B4 调试主文件，记录模型原始输出、解析结果、状态和元数据。 | `raw_text`、`parsed_candidate`、`status`、`error`、`schema_metadata` |
| `llm_run_log.jsonl` | B4 运行日志，每行是一轮生成记录。 | `timestamp`、`mode`、`status`、`raw_output_path`、`ai_message_path` |

### `ai_message.json`

这是最应该先看的文件。它只有一个标准 assistant 消息：

```json
{
  "role": "assistant",
  "content": "最终回答文本，或空字符串",
  "tool_calls": []
}
```

两种情况：

- `content` 非空且 `tool_calls` 为空：B4 已经给出最终回答。
- `content` 为空且 `tool_calls` 非空：B4 认为需要调用工具，后续应交给 B3 执行。

### `raw_model_output.json`

这是排查问题时最重要的文件。它比 `ai_message.json` 更完整，记录 B4 内部发生了什么。

常见字段：

| 字段 | 含义 |
|---|---|
| `mode` | B4 运行模式，通常是 `mock` 或 `prompt_json`。 |
| `backend` | 模型后端，真实模型一般是 `transformers`。 |
| `raw_text` | 模型原始生成文本，未必是合法 JSON。 |
| `parsed_candidate` | B4 从 `raw_text` 解析出的候选 JSON。 |
| `status` | B4 最终是否成功生成合法 AIMessage。 |
| `error` | 如果模型输出无法解析，这里会记录异常类型和信息。 |
| `planning` | 是否启用 plan-and-execute。 |
| `plan` | plan-and-execute 模式下的最终执行计划。 |
| `task_results` | plan-and-execute 模式下每个任务的执行结果。 |
| `schema_passing` | tools schema 的传递方式。 |
| `schema_metadata` | token 数、schema 大小、native tools 是否生效等信息。 |

注意：`raw_model_output.json.status = success` 只表示 B4 最终生成了合法 AIMessage，不一定表示 plan 里的任务全部执行成功。任务是否成功要看 `task_results` 或 `plan.json`。

## 2. B4 Plan-and-Execute 输出

启用 `--planning plan_and_execute` 后，会额外生成：

| 文件 | 作用 | 重点字段 |
|---|---|---|
| `plan.json` | plan-and-execute 的完整过程记录。 | `raw_text`、`plan`、`task_results`、`status`、`error` |
| `raw_model_output.json` | 最终回答合成阶段的记录，同时也包含 plan 和 task results。 | `raw_text`、`plan`、`task_results` |
| `ai_message.json` | 最终回答 AIMessage。 | `content`、`tool_calls` |

### `plan.json`

这是 plan-and-execute 最应该看的过程文件。

常见字段：

| 字段 | 含义 |
|---|---|
| `planning` | 固定为 `plan_and_execute`。 |
| `mode` | planning 阶段使用 mock 还是真实模型。 |
| `raw_text` | 模型生成 plan 的原始 JSON 文本。 |
| `plan` | 解析、清洗、资源选择后的最终计划。 |
| `task_results` | 每个 task 的执行结果。 |
| `execution_dir` | task 执行产物目录。 |
| `tools_config` | B3 工具配置文件路径。 |
| `toolset` | 使用的 B3 工具集合。 |
| `expert_config_path` | 专家模型配置文件路径。 |
| `status` | plan 执行总体状态。 |
| `error` | plan 生成或解析阶段的错误。 |
| `generated_at` | 生成时间。 |

### `plan.raw_text`

这是模型原始生成的计划。排查 planning 阶段问题时先看它。

如果这里出现：

```json
"error": {
  "type": "JSONDecodeError"
}
```

通常说明模型生成的 plan JSON 不完整或格式错误，例如：

- JSON 被 `max_new_tokens` 截断。
- `dependencies` / `task_tools` 被错误放进 `args`。
- 少了右括号或逗号。

### `plan.plan`

这是 B4 实际拿去执行的计划，不一定等于模型原始计划。

B4 会做几步处理：

1. 解析模型原始 plan。
2. 过滤 `format_final_answer`、`return_planning_json`、`end_process` 等元任务。
3. 限制任务数量，避免简单任务生成过长步骤。
4. 根据抽象任务选择真实工具或专家模型。

一个推荐的抽象任务示例：

```json
{
  "id": "task_001",
  "task": "summarize_text",
  "args": {
    "text": "..."
  },
  "dependencies": [],
  "task_tools": [],
  "selected_tool": null,
  "selected_model": "qwen3.5-0.8b"
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `task` | 抽象任务名，不是工具名。 |
| `args` | 执行任务需要的参数。 |
| `dependencies` | 依赖的前序 task id。 |
| `task_tools` | 真实 B3 工具列表；如果任务由模型直接处理，应为空数组。 |
| `selected_tool` | B4 资源选择后决定交给 B3 的工具。 |
| `selected_model` | B4 资源选择后决定交给专家模型的模型 id。 |
| `selection_reason` | 为什么这样选择资源。 |

当前设计中，`light_text_generation` 和 `code_generation` 不再是工具。文本/代码任务应写成抽象任务，例如：

- `summarize_text`
- `translate_text`
- `rewrite_text`
- `classify_intent`
- `extract_simple_fields`
- `generate_code`
- `explain_code`
- `debug_code`
- `write_unit_tests`

然后 B4 根据任务名选择专家模型。

### `task_results`

`task_results` 是执行结果数组，每个元素都是一个 TaskExecutionResult。

常见字段：

| 字段 | 含义 |
|---|---|
| `task_id` | 任务 id。 |
| `task` | 抽象任务名。 |
| `status` | `success`、`error` 或 `skipped`。 |
| `input` | 实际执行参数。 |
| `output` | 成功时的输出。 |
| `error` | 失败时的错误对象。 |
| `selected_tool` | 使用的 B3 工具，模型任务通常为 `null`。 |
| `selected_model` | 使用的专家模型，工具任务通常为 `null`。 |
| `executor_type` | `tool` 或 `model`。 |
| `dependencies` | 依赖任务列表。 |
| `resources` | 本任务生成的资源信息。 |
| `latency_ms` | 执行耗时，单位毫秒。 |

如果 `executor_type = tool`，说明任务交给 B3 执行。

如果 `executor_type = model`，说明任务交给专家模型执行。

例如：

```json
{
  "task_id": "task_001",
  "task": "summarize_text",
  "status": "success",
  "selected_tool": null,
  "selected_model": "qwen3.5-0.8b",
  "executor_type": "model",
  "output": {
    "text": "..."
  }
}
```

### `plan.status` 和 `raw_model_output.status` 的区别

这是最容易混淆的地方。

| 字段 | 说明 |
|---|---|
| `plan.json.status` | plan 执行是否成功。只要某个 task 失败，通常就是 `partial_or_error`。 |
| `raw_model_output.json.status` | B4 最终是否生成了合法 AIMessage。即使 task 失败，只要最终回答 JSON 合法，也可能是 `success`。 |

所以排查执行问题时，不要只看 `raw_model_output.json.status`，一定要看 `plan.json.task_results`。

## 3. B3 工具层输出

B4 plan-and-execute 中的工具任务会复用 B3 执行。B3 独立运行或被 B4 调用时常见文件如下：

| 文件 | 作用 |
|---|---|
| `tools_schema.json` | B3 导出的真实工具 schema。 |
| `tool_schema_report.json` | schema 摘要，例如工具数量和工具名。 |
| `tool_schema_audit.json` | schema 与 Python 函数签名的一致性检查。 |
| `tool_messages.json` | B3 执行 tool calls 后生成的 ToolMessage 数组。 |
| `tool_call_log.jsonl` | 每次工具调用的详细日志。 |
| `tool_stats_report.json` | 工具调用统计报告。 |

### `tools_schema.json`

这是 B3 提供给 B4 的真实工具能力描述。它只应该包含真实工具，例如：

- `calculator`
- `file_reader`
- `local_file_search`
- `table_analyzer`
- `format_converter`

B4 planning 中的 `task_tools` 只应引用这些真实工具。

### `tool_messages.json`

这是 B3 执行后的标准 ToolMessage：

```json
{
  "role": "tool",
  "tool_call_id": "task_001",
  "name": "file_reader",
  "content": "{\"skill_name\":\"file_reader\",...}",
  "status": "success"
}
```

其中 `content` 是序列化后的 SkillResult。

## 4. B4 Batch Eval 输出

批量评估命令会生成：

| 文件 | 作用 |
|---|---|
| `batch_tool_call_eval_report.json` | 汇总每个模型、每个 case 的工具调用预测准确率。 |
| `case_outputs/*_raw_model_output.json` | 每个 case 的 B4 原始输出记录。 |
| `case_outputs/*_ai_message.json` | 每个 case 的标准 AIMessage。 |
| `case_outputs/llm_run_log.jsonl` | 批量运行日志。 |

`batch_tool_call_eval_report.json` 重点看：

| 字段 | 含义 |
|---|---|
| `total_models` | 参与评估的模型数量。 |
| `total_cases` | case 数量。 |
| `models[].success_rate` | 某个模型的工具调用匹配成功率。 |
| `case_records` | 每条 case 的详细预测和匹配结果。 |

## 4.1 B4 Schema Passing Batch Eval 输出

拓展四的批量对比命令用于固定同一个模型，比较两种 tools schema 传递方式：

```bash
python code/b4_local_agent_llm.py \
  --tools_schema data/messages/tools_schema_basic.json \
  --schema_passing_batch_eval data/messages/b4_schema_passing_batch.json \
  --outdir outputs/B4_schema_passing_batch_real
```

如果只想快速检查流程，可以临时加 `--mode mock`；正式统计建议使用配置里的 `prompt_json`。

它会生成：

| 文件 | 作用 |
|---|---|
| `schema_passing_batch_report.json` | 完整统计报告，包含两种 schema passing 的逐 case 结果和总体指标。 |
| `schema_passing_batch_summary.md` | 便于阅读的 Markdown 汇总表。 |
| `case_outputs/*_raw_model_output.json` | 每个 case、每种 schema passing 的 B4 原始输出。 |
| `case_outputs/*_ai_message.json` | 每个 case、每种 schema passing 的标准 AIMessage。 |

`schema_passing_batch_report.json` 重点看：

| 字段 | 含义 |
|---|---|
| `summary_by_schema_passing[].parse_success_rate` | 模型输出能否解析成合法 AIMessage 的比例。 |
| `summary_by_schema_passing[].tool_name_success_rate` | 是否选对期望工具名的比例。 |
| `summary_by_schema_passing[].tool_call_success_rate` | 是否匹配期望 tool call 的比例，包含参数子集匹配。 |
| `summary_by_schema_passing[].avg_input_tokens` | 平均输入 token 数，用于比较 prompt 注入与 native tools 的开销。 |
| `summary_by_schema_passing[].native_tools_applied_count` | native tools 真正被 tokenizer 接受并生效的次数。 |
| `summary_by_schema_passing[].fallback_to_prompt_injection_count` | native tools 不支持时退回 prompt 注入的次数。 |
| `comparison_summary.native_minus_prompt_avg_input_tokens` | native tools 平均输入 token 减去 prompt 注入平均输入 token；负数表示 native 更省。 |
| `comparison_summary.same_prediction_rate` | 两种传参方式输出完全相同预测的比例。 |

## 5. B1 集成运行中的 B4 输出

B1 integrated 模式调用 B4 时，B4 产物通常在：

```text
outputs/B1_runtime/llm_calls/
```

常见文件：

| 文件 | 作用 |
|---|---|
| `llm_call_001_raw_model_output.json` | 第 1 次 B4 调用的原始记录。 |
| `llm_call_001_ai_message.json` | 第 1 次 B4 调用的标准 AIMessage。 |
| `llm_call_002_raw_model_output.json` | 第 2 次 B4 调用的原始记录。 |
| `llm_call_002_ai_message.json` | 第 2 次 B4 调用的标准 AIMessage。 |
| `llm_run_log.jsonl` | 本次 B1 运行中所有 B4 调用日志。 |

B1 目录下还会有：

| 文件 | 作用 |
|---|---|
| `messages.json` | 完整对话消息流。 |
| `trace.json` | B1 的完整运行轨迹。 |
| `final_answer.md` | 最终回答。 |
| `tool_messages.json` | 本轮累计产生的 ToolMessage。 |
| `tool_call_log.jsonl` | B3 工具执行明细。 |

## 6. 推荐阅读顺序

### 只看 B4 是否输出了正确答案

1. `ai_message.json`
2. `raw_model_output.json.status`
3. `raw_model_output.json.error`

### 排查模型输出格式错误

1. `raw_model_output.json.raw_text`
2. `raw_model_output.json.parsed_candidate`
3. `raw_model_output.json.error`

### 排查 plan-and-execute 为什么失败

1. `plan.json.status`
2. `plan.json.error`
3. `plan.json.plan`
4. `plan.json.task_results`
5. 如果任务是工具任务，再看对应 task 目录下的 B3 `tool_messages.json` 和 `tool_call_log.jsonl`

### 排查为什么运行很慢

1. 看 `task_results[].latency_ms`
2. 如果 `executor_type = model` 且耗时很长，通常是专家模型加载或推理慢。
3. 如果 `executor_type = tool` 且耗时很长，查看 B3 `tool_call_log.jsonl`。
4. 最终回答合成阶段耗时不在 `task_results` 内，可结合 `llm_run_log.jsonl` 和命令行日志判断。

## 7. 常见问题判断

### `raw_model_output.status = success`，但任务失败了

这是正常现象。它表示最终 AIMessage JSON 解析成功，不代表 task 成功。看 `plan.json.status` 和 `task_results`。

### `plan.source = fallback_after_parse_error`

说明模型生成的 plan JSON 解析失败，B4 使用 mock fallback plan。需要看 `plan.json.raw_text` 和 `plan.json.error`。

### `FileNotFoundError: docs/agent_intro.txt`

说明真实执行了 `file_reader`，但 `data/docs/agent_intro.txt` 不存在。检查任务是否本来就应该读文件，或者 plan 是否错误 fallback 到 file_reader。

### `selected_tool = null, selected_model = qwen3.5-0.8b`

说明这是抽象模型任务，不需要 B3 工具，由 B4 选择专家模型执行。

### `task_tools = []`

说明任务不需要真实 B3 工具。B4 会根据 `task` 名称选择专家模型。

### `task_tools = ["file_reader"]`

说明任务需要真实 B3 工具。B4 会把它交给 B3 执行。
