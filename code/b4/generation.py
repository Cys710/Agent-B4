from __future__ import annotations

"""B4 的 AIMessage 生成基础能力。

这个文件放的是“生成和解析”相关代码：
- 通用路径/字符串 helper
- mock 模式下的工具调用和回答生成
- 将模型原始输出解析成标准 AIMessage 或 plan
- 本地 transformers 模型加载、prompt 组装和推理

"""

import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from common.io_utils import read_yaml
from common.path_utils import resolve_from_file
from common.schemas import make_ai_message, validate_ai_message

PARSE_ERROR_CONTENT = "模型输出解析失败，无法生成有效工具调用或最终回答。"
SCHEMA_PASSING_MODES = {"prompt_injection", "native_tools"}
_MODEL_CACHE: dict[tuple[str, ...], tuple[Any, Any]] = {}


# ---------------------------------------------------------------------------
# 通用 helper：配置、artifact 路径、schema 摘要、文本处理
# ---------------------------------------------------------------------------

def _load_model_config(model_config: str | Path) -> tuple[Path, dict]:
    """读取 model.yaml，并返回配置文件路径和配置内容。"""
    path = Path(model_config).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict):
        raise ValueError("model.yaml must contain an object")
    return path, config


def _artifact_paths(artifact_dir: str | Path, stem: str | None) -> tuple[Path, Path, Path]:
    """统一管理 B4 单次生成会写出的三个主要 artifact 路径。"""
    directory = Path(artifact_dir)
    prefix = f"{stem}_" if stem else ""
    return (
        directory / f"{prefix}raw_model_output.json",
        directory / f"{prefix}ai_message.json",
        directory / "llm_run_log.jsonl",
    )

# 计划输出路径
def _plan_artifact_path(artifact_dir: str | Path, stem: str | None) -> Path:
    directory = Path(artifact_dir)
    prefix = f"{stem}_" if stem else ""
    return directory / f"{prefix}plan.json"

# 对比报告
def _schema_passing_artifact_path(artifact_dir: str | Path, stem: str | None) -> Path:
    directory = Path(artifact_dir)
    prefix = f"{stem}_" if stem else ""
    return directory / f"{prefix}schema_passing_report.json"

# tools schema 抽取工具名
def _tool_names_from_schema(tools_schema: list[dict]) -> list[str]:
    """从 OpenAI-style tools schema 中抽取工具名列表。"""
    names = []
    for item in tools_schema:
        if isinstance(item, dict):
            function = item.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.append(function["name"])
    return names

# 工具 Schema 完整字符串的字符总长度
def _schema_text_chars(tools_schema: list[dict]) -> int:
    return len(json.dumps(tools_schema, ensure_ascii=False))


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._") or "item"


def _extract_tool_result(message: dict) -> dict:
    """把 ToolMessage.content 中的 SkillResult JSON 字符串解析成字典。"""
    try:
        result = json.loads(message["content"])
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("ToolMessage content is not a SkillResult JSON string") from exc
    if not isinstance(result, dict):
        raise ValueError("ToolMessage content must decode to an object")
    return result


def _latest_user_text(messages: list[dict]) -> str:
    """获取最近一条 user 消息，mock 和 plan 生成都用它做规则判断。"""
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""

# 文本截断压缩工具
def _shorten_text(text: str, max_chars: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def _strip_trailing_newlines(raw_text: str) -> str:
    """Remove newline characters that small models sometimes append after JSON."""
    return raw_text.rstrip("\r\n")


def _ensure_unique_tool_call_ids(tool_calls: list[dict]) -> None:
    ids = [call.get("id") for call in tool_calls]
    if len(ids) != len(set(ids)):
        raise ValueError("tool call ids must be unique within one AIMessage")


def _task_name_from_tool(tool_name: str) -> str:
    """把工具名映射成抽象任务名，用于 plan task 的 task 字段。"""
    mapping = {
        "calculator": "calculate_expression",
        "file_reader": "read_local_file",
        "local_file_search": "search_local_files",
        "table_analyzer": "analyze_table",
        "format_converter": "convert_format",
        "read_then_convert": "read_and_convert_file",
        "python_sandbox": "run_python_snippet",
        "python_unit_test_sandbox": "run_python_unit_tests",
        "web_search": "search_web",
        "light_text_generation": "summarize_short_text",
        "code_generation": "generate_code",
    }
    return mapping.get(tool_name, f"use_{tool_name}")


def _available_schema_tool_names(tools_schema: list[dict]) -> set[str]:
    return set(_tool_names_from_schema(tools_schema))


# ---------------------------------------------------------------------------
# Mock 模式：不加载模型，用规则模拟 AIMessage / plan
# ---------------------------------------------------------------------------

def _mock_tool_calls_for_request(user_text: str) -> list[dict]:
    """根据用户文本粗略判断应该调用哪些工具。

    这个函数只用于离线联调和演示，目标是让 B1-B3-B5 链路稳定跑通，
    不是要做真实的意图识别。
    """
    lower_text = user_text.lower()
    tool_calls = []
    if any(keyword in user_text for keyword in ("读取", "文件", "文档", "docs")) or "read" in lower_text:
        tool_calls.append(
            {
                "id": "call_001",
                "name": "file_reader",
                "args": {"path": "docs/agent_intro.txt", "max_chars": 2000},
            }
        )
    if any(keyword in user_text for keyword in ("计算", "算", "表达式")) or "calculate" in lower_text:
        tool_calls.append(
            {
                "id": f"call_{len(tool_calls) + 1:03d}",
                "name": "calculator",
                "args": {"expression": "23 * 17 + 9"},
            }
        )
    if not tool_calls:
        tool_calls.append(
            {
                "id": "call_001",
                "name": "file_reader",
                "args": {"path": "docs/agent_intro.txt", "max_chars": 2000},
            }
        )
    return tool_calls


def _mock_answer_from_tool_messages(tool_messages: list[dict]) -> dict:
    """根据已有 ToolMessage 生成最终回答。

    如果工具失败，直接汇总失败原因；如果成功，就把关键输出压缩成简短文本。
    """
    summaries = []
    errors = []
    for message in tool_messages:
        result = _extract_tool_result(message)
        tool_name = message.get("name", result.get("skill_name", "unknown"))
        if message.get("status") != "success" or result.get("status") != "success":
            error = result.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            errors.append(f"{tool_name}: {detail or 'unknown tool error'}")
            continue

        output = result.get("output") or {}
        if tool_name == "file_reader" and isinstance(output, dict):
            content = output.get("content", "")
            summaries.append(f"file_reader returned: {_shorten_text(str(content))}")
        elif tool_name == "calculator" and isinstance(output, dict):
            summaries.append(f"calculator result: {output.get('result')}")
        else:
            summaries.append(f"{tool_name} returned: {_shorten_text(json.dumps(output, ensure_ascii=False))}")

    if errors:
        return make_ai_message("Some tool calls failed:\n" + "\n".join(f"- {item}" for item in errors), [])
    if not summaries:
        return make_ai_message("No usable tool result was provided.", [])
    return make_ai_message("Combined tool results:\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(summaries, 1)), [])


def _mock_generate(messages: list[dict]) -> dict:
    """mock 模式下生成 AIMessage。

    有 tool 消息时返回最终回答；没有 tool 消息时返回工具调用。
    """
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    if tool_messages:
        return _mock_answer_from_tool_messages(tool_messages)
    return make_ai_message("", _mock_tool_calls_for_request(_latest_user_text(messages)))


def _mock_plan_calls_for_request(user_text: str, tools_schema: list[dict]) -> list[dict]:
    """为 plan-and-execute 生成 mock task 的底层规则。"""
    lower_text = user_text.lower()
    available = _available_schema_tool_names(tools_schema)
    calls = []
    if any(keyword in lower_text for keyword in ("code", "python", "function", "debug", "unit test")) or any(
        keyword in user_text for keyword in ("代码", "函数", "调试", "单元测试")
    ):
        calls.append(
            {
                "id": "task_001",
                "task": "generate_code",
                "args": {"instruction": user_text},
                "task_tools": [],
            }
        )
    elif "calculator" in available and (any(keyword in user_text for keyword in ("计算", "算", "表达式")) or "calculate" in lower_text):
        calls.append(
            {
                "id": "task_001",
                "task": "calculate_expression",
                "args": {"expression": "23 * 17 + 9"},
                "task_tools": ["calculator"],
            }
        )
    elif "file_reader" in available and (any(keyword in user_text for keyword in ("读取", "文件", "文档", "docs")) or "read" in lower_text):
        calls.append(
            {
                "id": "task_001",
                "task": "read_local_file",
                "args": {"path": "docs/agent_intro.txt", "max_chars": 2000},
                "task_tools": ["file_reader"],
            }
        )
    elif any(keyword in lower_text for keyword in ("summarize", "summary", "translate", "rewrite", "classify", "extract")) or any(
        keyword in user_text for keyword in ("总结", "摘要", "翻译", "改写", "分类", "抽取")
    ):
        calls.append(
            {
                "id": "task_001",
                "task": "summarize_text",
                "args": {"text": user_text},
                "task_tools": [],
            }
        )
    else:
        calls.append(
            {
                "id": "task_001",
                "task": "summarize_text",
                "args": {"text": user_text},
                "task_tools": [],
            }
        )
    return calls


def _mock_generate_plan(messages: list[dict], tools_schema: list[dict]) -> dict:
    """mock 模式下生成一个可执行 plan。"""
    user_text = _latest_user_text(messages)
    tool_calls = _mock_plan_calls_for_request(user_text, tools_schema)
    steps = []
    tasks = []
    for index, call in enumerate(tool_calls, 1):
        task_tools = call.get("task_tools", [])
        if "file_reader" in task_tools:
            objective = "Read the requested local document."
        elif "calculator" in task_tools:
            objective = "Calculate the requested arithmetic expression."
        else:
            objective = f"Run abstract task {call['task']} with a suitable expert model."
        steps.append(objective)
        tasks.append(
            {
                "id": f"task_{index:03d}",
                "task": call["task"],
                "args": call.get("args", {}),
                "dependencies": [],
                "task_tools": task_tools,
            }
        )
    steps.append("Combine all tool results into one final answer.")
    return {
        "steps": steps,
        "tasks": tasks,
        "tool_names": [tool for call in tool_calls for tool in call.get("task_tools", [])],
        "summary": "Plan independent tasks first; model selection and execution happen in later stages.",
        "source": "mock",
        "available_tool_count": len(tools_schema),
    }


# ---------------------------------------------------------------------------
# 输出解析：把模型原始文本修复/校验成 AIMessage 或 plan
# ---------------------------------------------------------------------------

def _parse_tool_calls_fragment(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    """从“不完整但含 tool_calls 片段”的模型输出中抢救工具调用数组。"""
    markers = ['"tool_calls":[', '\\"tool_calls\\":[']
    marker_index = -1
    marker = ""
    for item in markers:
        marker_index = raw_text.find(item)
        if marker_index != -1:
            marker = item
            break
    if marker_index == -1:
        raise original_error
    array_start = marker_index + marker.index("[")
    array_end = raw_text.rfind("]")
    if array_end < array_start:
        raise ValueError("model output contains tool_calls marker but no closing array")
    array_text = raw_text[array_start : array_end + 1]
    try:
        tool_calls = json.loads(array_text)
    except json.JSONDecodeError:
        tool_calls = json.loads(array_text.replace('\\"', '"'))
    if not isinstance(tool_calls, list) or not tool_calls:
        raise original_error
    return {"content": "", "tool_calls": tool_calls}


def _parse_json_with_backtick_tail(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    """兼容模型在合法 JSON 后面多吐几个反引号的情况。"""
    text = raw_text.strip()
    try:
        candidate, end_index = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        raise original_error
    trailing = text[end_index:].strip()
    if trailing and set(trailing) <= {"`"}:
        return candidate
    raise original_error


def _parse_fenced_json_block(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    """兼容模型把合法 AIMessage JSON 包在 ```json 代码块里的情况。"""
    fence_pattern = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)
    for block in fence_pattern.findall(raw_text.strip()):
        candidate_text = block.strip()
        if not candidate_text:
            continue
        try:
            candidate = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    raise original_error


def _parse_native_tool_call_blocks(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    """兼容部分模型输出的 <tool_call>...</tool_call> 风格工具调用。"""
    tool_calls = []
    block_pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    function_pattern = re.compile(r"<function=([A-Za-z_][\w.-]*)>\s*(.*?)\s*</function>", re.DOTALL)
    parameter_pattern = re.compile(r"<parameter=([A-Za-z_][\w.-]*)>\s*(.*?)\s*</parameter>", re.DOTALL)
    for block in block_pattern.findall(raw_text):
        function_match = function_pattern.search(block)
        if not function_match:
            continue
        name = function_match.group(1).strip()
        body = function_match.group(2)
        args = {}
        for key, value in parameter_pattern.findall(body):
            args[key.strip()] = value.strip()
        tool_calls.append(
            {
                "id": f"call_{len(tool_calls) + 1:03d}",
                "name": name,
                "args": args,
            }
        )
    if not tool_calls:
        raise original_error
    return {"content": "", "tool_calls": tool_calls}


def _candidate_to_message(candidate: dict) -> tuple[dict, dict]:
    """把解析出的 JSON 候选对象校验并转成标准 AIMessage。"""
    if not isinstance(candidate, dict):
        raise ValueError("model output JSON must be an object")
    expected_keys = {"content", "tool_calls"}
    unknown_keys = set(candidate) - expected_keys
    if unknown_keys:
        raise ValueError(f"model output JSON contains unknown keys: {', '.join(sorted(unknown_keys))}")
    message = {
        "role": "assistant",
        "content": candidate.get("content", ""),
        "tool_calls": candidate.get("tool_calls", []),
    }
    validate_ai_message(message)
    has_content = bool(message["content"].strip())
    has_tool_calls = bool(message["tool_calls"])
    if has_content == has_tool_calls:
        raise ValueError("model output must contain either final content or tool calls, but not both")
    _ensure_unique_tool_call_ids(message["tool_calls"])
    parsed_candidate = {"content": message["content"], "tool_calls": message["tool_calls"]}
    return parsed_candidate, message


def _parse_model_output(raw_text: str) -> tuple[dict, dict]:
    """解析模型原始输出。

    正常路径是直接 json.loads；后面几个 fallback 是为了兼容小模型常见的格式漂移。
    """
    raw_text = _strip_trailing_newlines(raw_text)
    try:
        candidate = json.loads(raw_text.strip())
    except json.JSONDecodeError as exc:
        try:
            candidate = _parse_fenced_json_block(raw_text, exc)
        except json.JSONDecodeError:
            try:
                candidate = _parse_json_with_backtick_tail(raw_text, exc)
            except json.JSONDecodeError:
                try:
                    candidate = _parse_tool_calls_fragment(raw_text, exc)
                except json.JSONDecodeError:
                    candidate = _parse_native_tool_call_blocks(raw_text, exc)
    return _candidate_to_message(candidate)


def _parse_plan_output(raw_text: str) -> dict:
    """解析 planning 阶段的 JSON 输出。

    支持两种格式：
    - 新格式：显式 tasks 数组
    - 旧格式：只有 steps/tool_names，由本函数补成 tasks
    """
    raw_text = _strip_trailing_newlines(raw_text)
    candidate = json.loads(raw_text.strip())
    if not isinstance(candidate, dict):
        raise ValueError("plan output JSON must be an object")
    raw_tasks = candidate.get("tasks")
    raw_steps = candidate.get("steps", candidate.get("plan"))
    if raw_tasks is None:
        if not isinstance(raw_steps, list) or not raw_steps or not all(isinstance(step, str) and step.strip() for step in raw_steps):
            raise ValueError("plan output must contain a non-empty tasks array or steps array")
        tool_names = candidate.get("tool_names", [])
        if tool_names is None:
            tool_names = []
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
            raise ValueError("plan output tool_names must be an array of strings")
        tasks = [
            {
                "id": f"task_{index + 1:03d}",
                "task": _task_name_from_tool(tool_name),
                "args": {},
                "dependencies": [],
                "task_tools": [tool_name],
            }
            for index, tool_name in enumerate(tool_names)
        ]
        steps = [step.strip() for step in raw_steps]
    else:
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("plan output tasks must be a non-empty array")
        tasks = []
        for index, item in enumerate(raw_tasks, 1):
            if not isinstance(item, dict):
                raise ValueError("each plan task must be an object")
            task_name = item.get("task", item.get("name"))
            if not isinstance(task_name, str) or not task_name.strip():
                raise ValueError("each plan task must contain a non-empty task field")
            args = item.get("args", {})
            if not isinstance(args, dict):
                args = {}
            dependencies = item.get("dependencies", item.get("dep", []))
            if dependencies is None:
                dependencies = []
            if not isinstance(dependencies, list):
                raise ValueError("task dependencies must be an array")
            task_tools = item.get("task_tools")
            if task_tools is None:
                task_tools = [task_name.strip()]
            if not isinstance(task_tools, list) or not all(isinstance(name, str) for name in task_tools):
                raise ValueError("task task_tools must be an array of strings")
            tasks.append(
                {
                    "id": str(item.get("id") or f"task_{index:03d}"),
                    "task": task_name.strip(),
                    "args": args,
                    "dependencies": [str(dep) for dep in dependencies],
                    "task_tools": [name.strip() for name in task_tools if name.strip()],
                }
            )
        steps = [str(step).strip() for step in raw_steps] if isinstance(raw_steps, list) else [
            f"{task['id']}: {task['task']}" for task in tasks
        ]
        tool_names = candidate.get("tool_names")
        if tool_names is None:
            tool_names = [task["task"] for task in tasks]
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
            raise ValueError("plan output tool_names must be an array of strings")
    return {
        "steps": steps,
        "tasks": tasks,
        "tool_names": tool_names,
        "summary": str(candidate.get("summary", "")).strip(),
        "source": "model",
    }


# ---------------------------------------------------------------------------
# 本地模型后端：prompt 组装、模型缓存、transformers 推理
# ---------------------------------------------------------------------------

def _dtype_value(torch_module: Any, configured: str) -> Any:
    """把 yaml 里的 torch_dtype 字符串转成 torch dtype 对象。"""
    if configured == "auto":
        return "auto"
    mapping = {
        "bfloat16": torch_module.bfloat16,
        "float16": torch_module.float16,
        "float32": torch_module.float32,
    }
    if configured not in mapping:
        raise ValueError(f"unsupported torch_dtype: {configured}")
    return mapping[configured]


def _model_cache_key(
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[str, ...]:
    """构造模型缓存 key，避免同一模型在一次进程里重复加载。"""
    try:
        device_map_key = json.dumps(device_map, sort_keys=True, separators=(",", ":"))
    except TypeError:
        device_map_key = repr(device_map)
    try:
        max_memory_key = json.dumps(max_memory, sort_keys=True, separators=(",", ":"))
    except TypeError:
        max_memory_key = repr(max_memory)
    return (
        str(model_path),
        str(tokenizer_path),
        str(local_only),
        str(trust_remote_code),
        str(dtype),
        device_map_key,
        max_memory_key,
    )


def _load_model_bundle(
    auto_model: Any,
    auto_tokenizer: Any,
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[Any, Any]:
    """加载 tokenizer 和模型，并放入进程内缓存。"""
    cache_key = _model_cache_key(
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        device_map,
        max_memory,
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        print("model_cache=hit", file=sys.stderr, flush=True)
        return cached

    print("model_cache=miss", file=sys.stderr, flush=True)
    tokenizer = auto_tokenizer.from_pretrained(
        str(tokenizer_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
    )
    model = auto_model.from_pretrained(
        str(model_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
        dtype=dtype,
        device_map=device_map,
        max_memory=max_memory,
    )
    _MODEL_CACHE[cache_key] = (tokenizer, model)
    return tokenizer, model


def _build_prompt_messages(messages: list[dict], tools_schema: list[dict]) -> list[dict]:
    """prompt_injection 模式下组装消息。

    这里会把 tools schema 和输出格式要求写入 system/user 消息，
    强制模型只输出标准 JSON。
    """
    prompt_messages = deepcopy(messages)
    format_instruction = (
        "IMPORTANT OUTPUT FORMAT:\n"
        "You must return exactly one valid JSON object.\n"
        "Do not output markdown, explanations, code fences, backticks, or any text outside JSON.\n"
        'The first output character must be "{" and the last output character must be "}".\n\n'
        "Valid schema A, final answer:\n"
        '{"content":"final answer text","tool_calls":[]}\n\n'
        "Valid schema B, one or more tool calls:\n"
        '{"content":"","tool_calls":[{"id":"call_001","name":"file_reader",'
        '"args":{"path":"docs/agent_intro.txt","max_chars":2000}},'
        '{"id":"call_002","name":"calculator","args":{"expression":"23 * 17 + 9"}}]}\n\n'
        "Rules:\n"
        "- The top-level keys must be exactly content and tool_calls.\n"
        "- Use schema B when tools are needed before answering.\n"
        "- You may include multiple tool call objects in tool_calls when they are independent.\n"
        "- Use stable unique ids such as call_001, call_002, call_003.\n"
        "- Do not include final answer content in the same message as tool_calls.\n"
        "- Do not call a tool whose arguments depend on a previous tool result in the same message.\n"
        "- Never put tool_calls inside content.\n"
    )
    envelope_reminder = (
        "IMPORTANT OUTPUT FORMAT: Output the JSON object now. "
        'Your first output character must be "{" and your last output character must be "}". '
        'Use exactly the top-level keys "content" (string) and "tool_calls" (array). '
        "Choose exactly one schema: final content with an empty tool_calls array, "
        "or empty content with one or more tool calls. "
        "If multiple independent tools are needed, put all of them in the same tool_calls array. "
        "Never output markdown, backticks, explanations, or text outside the JSON."
    )
    system_instruction = (
        "\n\nAvailable tools JSON schema:\n"
        + json.dumps(tools_schema, ensure_ascii=False)
        + "\n"
        + format_instruction
    )
    # 拼接prompt
    # 1. 开头添加 system_instruction
    # 2. 最新用户添加 envelope_reminder
    # 3. 最后工具调用 envelope_reminder + 说明
    if prompt_messages and prompt_messages[0].get("role") == "system":
        prompt_messages[0]["content"] += system_instruction
    else:
        prompt_messages.insert(0, {"role": "system", "content": system_instruction.strip()})

    for message in reversed(prompt_messages):
        if message.get("role") == "user":
            message["content"] += "\n\n" + envelope_reminder
            break
    if prompt_messages[-1].get("role") == "tool":
        prompt_messages.append(
            {
                "role": "user",
                "content": (
                    envelope_reminder
                    + " The latest messages already contain tool results. If they provide the requested "
                    'information, answer with schema A now and set "tool_calls" to exactly []. Do not repeat '
                    "completed tool calls."
                ),
            }
        )
    return prompt_messages


def _build_native_tool_messages(messages: list[dict]) -> list[dict]:
    """native_tools 模式下组装消息。

    tools schema 不直接拼进 prompt，而是后续传给 tokenizer.apply_chat_template。
    """
    prompt_messages = deepcopy(messages)
    format_instruction = (
        "\n\nIMPORTANT OUTPUT FORMAT:\n"
        "Use the model chat template native tools argument to decide which tools are available.\n"
        "Return exactly one valid JSON object and no markdown, explanations, code fences, or text outside JSON.\n"
        'For final answers use {"content":"final answer text","tool_calls":[]}.\n'
        'For tool calls use {"content":"","tool_calls":[{"id":"call_001","name":"tool_name","args":{}}]}.\n'
        "When multiple independent tools are needed, include all calls in one tool_calls array with unique ids.\n"
        "Do not include final answer content in the same message as tool_calls.\n"
    )
    envelope_reminder = (
        "IMPORTANT OUTPUT FORMAT: Output exactly one JSON object now. "
        'Use the top-level keys "content" and "tool_calls". '
        "Use the native tool list supplied to the chat template; do not invent tool names."
    )
    if prompt_messages and prompt_messages[0].get("role") == "system":
        prompt_messages[0]["content"] += format_instruction
    else:
        prompt_messages.insert(0, {"role": "system", "content": format_instruction.strip()})
    for message in reversed(prompt_messages):
        if message.get("role") == "user":
            message["content"] += "\n\n" + envelope_reminder
            break
    if prompt_messages[-1].get("role") == "tool":
        prompt_messages.append(
            {
                "role": "user",
                "content": (
                    envelope_reminder
                    + " The latest messages already contain tool results. If they provide the requested "
                    'information, answer with final content and set "tool_calls" to exactly [].'
                ),
            }
        )
    return prompt_messages


def _apply_chat_template_with_schema_passing(
    tokenizer: Any,
    prompt_messages: list[dict],
    tools_schema: list[dict],
    schema_passing: str,
) -> tuple[Any, dict]:
    """根据 schema_passing 选择 schema 传递方式并生成模型输入。

    如果 native_tools 不被当前 tokenizer 支持，会自动 fallback 到 prompt_injection。
    """
    metadata = {
        "schema_passing": schema_passing,
        "requested_native_tools": schema_passing == "native_tools",
        "native_tools_applied": False,
        "fallback_to_prompt_injection": False,
        "fallback_reason": None,
    }
    common_kwargs = {
        "tokenize": True,
        "add_generation_prompt": True,
        "return_tensors": "pt",
        "return_dict": True,
        "enable_thinking": False,
    }
    if schema_passing == "native_tools":
        try:
            inputs = tokenizer.apply_chat_template(
                prompt_messages,
                tools=tools_schema,
                **common_kwargs,
            )
            metadata["native_tools_applied"] = True
            return inputs, metadata
        except Exception as exc:
            metadata["fallback_to_prompt_injection"] = True
            metadata["fallback_reason"] = f"{type(exc).__name__}: {exc}"
            fallback_messages = _build_prompt_messages(prompt_messages, tools_schema)
            inputs = tokenizer.apply_chat_template(fallback_messages, **common_kwargs)
            return inputs, metadata
    inputs = tokenizer.apply_chat_template(prompt_messages, **common_kwargs)
    return inputs, metadata


def _prompt_json_generate(
    config_path: Path,
    config: dict,
    messages: list[dict],
    tools_schema: list[dict],
    schema_passing: str,
) -> tuple[str, dict]:
    """执行一次本地模型推理，并返回模型新生成的原始文本。"""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("prompt_json mode requires the LLM dependencies in requirements.txt") from exc
    # 加载模型
    model_config = config.get("model", {})
    generation_config = config.get("generation", {})
    model_setting = model_config.get("model_name_or_path")
    tokenizer_setting = model_config.get("tokenizer_name_or_path", model_setting)
    if not isinstance(model_setting, str) or not isinstance(tokenizer_setting, str):
        raise ValueError("model_name_or_path and tokenizer_name_or_path are required")
    model_path = resolve_from_file(model_setting, config_path)
    tokenizer_path = resolve_from_file(tokenizer_setting, config_path)
    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(f"local model path does not exist: {model_path}")
    local_only = bool(model_config.get("local_files_only", True))
    trust_remote_code = bool(model_config.get("trust_remote_code", False))
    dtype = _dtype_value(torch, str(model_config.get("torch_dtype", "auto")))
    tokenizer, model = _load_model_bundle(
        AutoModelForCausalLM,
        AutoTokenizer,
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    # 设置输入
    if schema_passing == "prompt_injection":
        prompt_messages = _build_prompt_messages(messages, tools_schema)
    elif schema_passing == "native_tools":
        prompt_messages = _build_native_tool_messages(messages)
    else:
        raise ValueError("schema_passing must be prompt_injection or native_tools")
    inputs, schema_metadata = _apply_chat_template_with_schema_passing(
        tokenizer,
        prompt_messages,
        tools_schema,
        schema_passing,
    )
    device = next(model.parameters()).device
    inputs = inputs.to(device)
    input_length = inputs["input_ids"].shape[-1]
    options = {
        "max_new_tokens": int(generation_config.get("max_new_tokens", 1024)),
        "do_sample": bool(generation_config.get("do_sample", False)),
    }
    # 推理生成文本
    with torch.no_grad():
        generated = model.generate(**inputs, **options)
    new_tokens = generated[0][input_length:]
    schema_metadata["input_token_count"] = int(input_length)
    schema_metadata["output_token_count"] = int(new_tokens.shape[-1])
    schema_metadata["total_token_count"] = int(input_length + new_tokens.shape[-1])
    schema_metadata["schema_text_chars"] = _schema_text_chars(tools_schema)
    schema_metadata["available_tool_names"] = _tool_names_from_schema(tools_schema)
    return tokenizer.decode(new_tokens, skip_special_tokens=True), schema_metadata


def _build_plan_messages(messages: list[dict], tools_schema: list[dict]) -> list[dict]:
    """为 plan 生成阶段构造 prompt。"""
    plan_messages = deepcopy(messages)
    plan_instruction = (
        "\n\nPlan-and-Execute planning step:\n"
        "Create a concise execution plan before the final AIMessage generation.\n"
        "Return exactly one valid JSON object and no markdown.\n"
        "The JSON object must contain:\n"
        "- tasks: a non-empty array of task objects. Each task object has id, task, args, dependencies, task_tools.\n"
        "- steps: a non-empty array of short strings for backward compatibility.\n"
        "- tool_names: an array of tool names that may be useful.\n"
        "- summary: a short string.\n\n"
        "Hard limits:\n"
        "- Keep the plan minimal; use at most 3 tasks.\n"
        "- If one tool or one expert model can solve the request, output exactly one task.\n"
        "- Create a task only when it requires a real tool call or expert model execution.\n"
        "- Do not create tasks for formatting, returning JSON, executing the plan, finalizing, completing, or ending the process.\n"
        "- Forbidden task names include format_final_answer, output_final_answer, return_planning_json, "
        "execute_planning, finalize_response, complete_task, finish_planning, and end_process.\n"
        "- Available tools are real B3 tools only. Do not invent pseudo tools such as light_text_generation or code_generation.\n"
        "- If a task should be handled directly by a model, set task_tools to [] and use an abstract task name.\n"
        "- Do not chain repeated model tasks for the same text; use one abstract text task instead.\n\n"
        "Recommended abstract task names:\n"
        "- Model text tasks: summarize_text, translate_text, rewrite_text, classify_intent, extract_simple_fields.\n"
        "- Model code tasks: generate_code, explain_code, debug_code, write_unit_tests.\n"
        "- Tool tasks: read_local_file, calculate_expression, search_local_files, analyze_table, convert_format.\n\n"
        "Follow HuggingGPT-style task planning: parse the request into a task list, decide execution order, "
        "and express resource dependencies with dependencies or placeholders like <resource-task_001> in args. "
        "The task field is an abstract task name generated from the user request, not a tool name; "
        "put available concrete B3 tools only in task_tools, or [] when no tool is needed. "
        "Do not select concrete models yet.\n\n"
        "Available tools JSON schema:\n"
        + json.dumps(tools_schema, ensure_ascii=False)
        + "\n\n"
        "Example:\n"
        '{"tasks":[{"id":"task_001","task":"summarize_text","args":{"text":"inline text to summarize"},'
        '"dependencies":[],"task_tools":[]}],'
        '"steps":["Summarize the provided text with a suitable model."],'
        '"tool_names":[],"summary":"One model task is enough."}\n'
        "Tool example:\n"
        '{"tasks":[{"id":"task_001","task":"read_local_file","args":{"path":"docs/agent_intro.txt"},'
        '"dependencies":[],"task_tools":["file_reader"]}],'
        '"steps":["Read the requested document."],"tool_names":["file_reader"],'
        '"summary":"Use a B3 file_reader tool because local file access is required."}'
    )
    if plan_messages and plan_messages[0].get("role") == "system":
        plan_messages[0]["content"] += plan_instruction
    else:
        plan_messages.insert(0, {"role": "system", "content": plan_instruction.strip()})
    for message in reversed(plan_messages):
        if message.get("role") == "user":
            message["content"] += "\n\nReturn only the planning JSON object now. Do not produce tool_calls or final answer yet."
            break
    return plan_messages


def _prompt_json_generate_plan(config_path: Path, config: dict, messages: list[dict], tools_schema: list[dict]) -> str:
    """用本地模型生成 plan JSON 的原始文本。"""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("prompt_json mode requires the LLM dependencies in requirements.txt") from exc
    model_config = config.get("model", {})
    generation_config = config.get("generation", {})
    model_setting = model_config.get("model_name_or_path")
    tokenizer_setting = model_config.get("tokenizer_name_or_path", model_setting)
    if not isinstance(model_setting, str) or not isinstance(tokenizer_setting, str):
        raise ValueError("model_name_or_path and tokenizer_name_or_path are required")
    model_path = resolve_from_file(model_setting, config_path)
    tokenizer_path = resolve_from_file(tokenizer_setting, config_path)
    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(f"local model path does not exist: {model_path}")
    local_only = bool(model_config.get("local_files_only", True))
    trust_remote_code = bool(model_config.get("trust_remote_code", False))
    dtype = _dtype_value(torch, str(model_config.get("torch_dtype", "auto")))
    tokenizer, model = _load_model_bundle(
        AutoModelForCausalLM,
        AutoTokenizer,
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    plan_messages = _build_plan_messages(messages, tools_schema)
    inputs = tokenizer.apply_chat_template(
        plan_messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        enable_thinking=False,
    )
    device = next(model.parameters()).device
    inputs = inputs.to(device)
    input_length = inputs["input_ids"].shape[-1]
    options = {
        "max_new_tokens": min(int(generation_config.get("max_new_tokens", 1024)), 512),
        "do_sample": bool(generation_config.get("do_sample", False)),
    }
    with torch.no_grad():
        generated = model.generate(**inputs, **options)
    new_tokens = generated[0][input_length:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)
