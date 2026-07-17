from __future__ import annotations

"""B4 的 plan-and-execute 实现。

这个文件把 plan 相关能力集中在一起：
- 生成/解析 plan 后，为 task 选择工具或专家模型
- 按依赖顺序执行 task
- 工具型 task 交给 B3
- 模型型 task 交给专家模型 runner
- 把 task_results 合成最终 AIMessage

service.py 只调用本文件的几个高层函数，不直接关心 task 执行细节。
"""

import copy
import json
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from b3_tool_layer import execute_tool_calls
from common.io_utils import ensure_dir, read_yaml
from common.schemas import make_ai_message, make_error_payload, make_task_execution_result

from .generation import (
    _latest_user_text,
    _mock_generate_plan,
    _parse_model_output,
    _parse_plan_output,
    _prompt_json_generate,
    _prompt_json_generate_plan,
    _strip_trailing_newlines,
    _task_name_from_tool,
)

PLACEHOLDER_RE = re.compile(r"\{\{(?P<task_id>[^.{}]+)\.output\.(?P<field>[^{}]+)\}\}")
CODE_FENCE_RE = re.compile(r"^\s*```(?:python)?\s*(?P<code>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
MAX_PLAN_TASKS = 3
META_TASK_KEYWORDS = {
    "format_final_answer",
    "output_final_answer",
    "return_planning_json",
    "execute_planning",
    "finalize_response",
    "complete_task",
    "finish_planning",
    "end_process",
    "return_json",
    "final_answer",
    "combine_results",
}
PSEUDO_MODEL_TOOLS = {"light_text_generation", "code_generation"}
TEXT_FILE_EXTENSIONS = {".txt", ".md"}
DOCUMENT_FILE_EXTENSIONS = {".pdf", ".docx", ".pptx"}
TABLE_FILE_EXTENSIONS = {".csv", ".tsv"}


def _norm_text(value: Any) -> str:
    """Normalize small task/tool strings for lightweight routing."""
    return str(value or "").strip().lower()

 # 拼接多段清洗后的文本，空字符串自动过滤
def _task_search_text(task: dict) -> str:
    args = task.get("args", {}) if isinstance(task.get("args"), dict) else {}
    return " ".join(
        part
        for part in [
            _norm_text(task.get("task")),
            _norm_text(task.get("description")),
            _norm_text(args.get("path")),
            _norm_text(args.get("query")),
            _norm_text(args.get("expression")),
            _norm_text(args.get("target_format")),
            _norm_text(args.get("code")),
        ]
        if part
    )

# 根据配置文件、指定工具集名称，筛选并返回当前可用的合法工具名称列表。
def _configured_tool_names(tools_config: str | Path | None, config_path: Path, toolset: str | None) -> list[str]:
    """Return available B3 tool names for the selected toolset."""
    config_file = Path(tools_config).resolve() if tools_config else _default_tools_config(config_path)
    if not config_file.is_file():
        return []
    config = read_yaml(config_file)
    if not isinstance(config, dict):
        return []
    toolsets = config.get("toolsets", {})
    tools = config.get("tools", {})
    if not isinstance(toolsets, dict) or not isinstance(tools, dict):
        return []
    selected_toolset = toolset or config.get("default_toolset")
    names = toolsets.get(selected_toolset, [])
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if isinstance(name, str) and name in tools]

# 给单个工具打分，衡量该工具和当前任务的匹配度，用于 Agent 工具选择排序。
def _score_tool_for_task(tool_name: str, task: dict, hinted_tools: list[str]) -> tuple[int, list[str]]:
    """Score a B3 tool candidate against a planned task."""
    score = 0
    reasons: list[str] = []
    text = _task_search_text(task)
    args = task.get("args", {}) if isinstance(task.get("args"), dict) else {}
    path = _norm_text(args.get("path"))
    suffix = Path(path).suffix.lower() if path else ""

    if tool_name in hinted_tools:
        score += 25
        reasons.append("planner hinted this tool")

    keyword_rules = {
        "calculator": ["calculate", "arithmetic", "expression", "compute", "计算", "算"],
        "file_reader": ["read_local_file", "read file", "markdown", "txt", "读取"],
        "document_reader": ["document", "pdf", "docx", "pptx", "文档"],
        "local_file_search": ["search", "find", "query", "检索", "搜索", "查找"],
        "table_analyzer": ["table", "csv", "tsv", "analyze_table", "statistics", "表格", "统计"],
        "format_converter": ["convert", "markdown", "json", "format", "转换"],
        "read_then_convert": ["read_then_convert", "read and convert", "读取并转换"],
        "python_sandbox": ["python", "run code", "execute code", "运行代码"],
        "python_unit_test_sandbox": ["unit test", "unit_test", "pytest", "测试"],
        "web_search": ["web", "internet", "online", "联网"],
    }
    hits = [keyword for keyword in keyword_rules.get(tool_name, []) if keyword in text]
    if hits:
        score += 12 + min(len(hits), 3) * 4
        reasons.append(f"matched task keywords: {', '.join(hits[:3])}")

    if tool_name == "calculator" and isinstance(args.get("expression"), str):
        score += 35
        reasons.append("has arithmetic expression")
    elif tool_name == "local_file_search" and isinstance(args.get("query"), str):
        score += 35
        reasons.append("has search query")
    elif tool_name == "format_converter" and isinstance(args.get("target_format"), str) and isinstance(args.get("text"), str):
        score += 35
        reasons.append("has text and target format")
    elif tool_name == "read_then_convert" and path and isinstance(args.get("target_format"), str):
        score += 35
        reasons.append("has path and target format")
    elif tool_name == "python_sandbox" and isinstance(args.get("code"), str):
        score += 25
        reasons.append("has code snippet")
    elif tool_name == "python_unit_test_sandbox" and isinstance(args.get("code"), str) and isinstance(args.get("tests"), list):
        score += 40
        reasons.append("has code and tests")

    if path:
        if tool_name == "file_reader":
            if suffix in TEXT_FILE_EXTENSIONS:
                score += 40
                reasons.append(f"text file extension {suffix}")
            elif suffix in DOCUMENT_FILE_EXTENSIONS | TABLE_FILE_EXTENSIONS:
                score -= 30
                reasons.append(f"file_reader does not fit {suffix}")
        elif tool_name == "document_reader":
            if suffix in DOCUMENT_FILE_EXTENSIONS:
                score += 45
                reasons.append(f"document extension {suffix}")
            elif suffix in TEXT_FILE_EXTENSIONS | TABLE_FILE_EXTENSIONS:
                score -= 25
                reasons.append(f"document_reader does not fit {suffix}")
        elif tool_name == "table_analyzer":
            if suffix in TABLE_FILE_EXTENSIONS:
                score += 45
                reasons.append(f"table extension {suffix}")
            elif suffix in TEXT_FILE_EXTENSIONS | DOCUMENT_FILE_EXTENSIONS:
                score -= 25
                reasons.append(f"table_analyzer does not fit {suffix}")

    required_args = {
        "calculator": ["expression"],
        "file_reader": ["path"],
        "document_reader": ["path"],
        "local_file_search": ["query"],
        "table_analyzer": ["path"],
        "format_converter": ["text", "target_format"],
        "read_then_convert": ["path", "target_format"],
        "python_sandbox": ["code"],
        "python_unit_test_sandbox": ["code", "tests"],
        "web_search": ["query"],
    }
    missing = [name for name in required_args.get(tool_name, []) if name not in args]
    if missing:
        score -= 20 * len(missing)
        reasons.append(f"missing required args: {', '.join(missing)}")

    return score, reasons


def _rank_tool_candidates(task: dict, available_tools: list[str]) -> list[dict]:
    hinted_tools = [
        tool
        for tool in task.get("task_tools", [])
        if isinstance(tool, str) and tool.strip() and tool not in PSEUDO_MODEL_TOOLS
    ]
    candidate_names = list(dict.fromkeys(hinted_tools + available_tools))
    ranked = []
    for tool_name in candidate_names:
        if tool_name not in available_tools:
            ranked.append(
                {
                    "name": tool_name,
                    "score": -100,
                    "reasons": ["tool is not available in selected toolset"],
                    "available": False,
                }
            )
            continue
        score, reasons = _score_tool_for_task(tool_name, task, hinted_tools)
        ranked.append({"name": tool_name, "score": score, "reasons": reasons, "available": True})
    return sorted(ranked, key=lambda item: item["score"], reverse=True)

#  按匹配分降序排序，输出有序工具候选列表，给 Agent 选择最优工具。
def _rank_model_candidates(task: dict, expert_models: dict) -> list[dict]:
    ranked = []
    for model_key, entry in expert_models.items():
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("model_id", model_key))
        if not _task_matches_model(task, entry):
            continue
        role = str(entry.get("role", ""))
        score = 35
        task_types = [str(item).lower() for item in entry.get("task_types", []) if isinstance(item, str)]
        task_name = _norm_text(task.get("task"))
        if task_name in task_types:
            score += 20
        if role:
            score += 5
        ranked.append(
            {
                "name": model_id,
                "score": score,
                "role": role,
                "reasons": [f"matched expert role/task types for `{task.get('task')}`"],
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


# ---------------------------------------------------------------------------
# 资源选择：给 plan 里的每个 task 选择工具和可选专家模型
# ---------------------------------------------------------------------------

def _load_expert_models(config_path: Path) -> dict:
    """读取与 model.yaml 同目录下的 expert_models.yaml。"""
    expert_config_path = config_path.parent / "expert_models.yaml"
    if not expert_config_path.is_file():
        return {}
    config = read_yaml(expert_config_path)
    models = config.get("expert_models", {}) if isinstance(config, dict) else {}
    return models if isinstance(models, dict) else {}


def _task_matches_model(task: dict, model_entry: dict) -> bool:
    """判断某个 task 是否适合某个专家模型配置。"""
    task_name = str(task.get("task", "")).lower()
    task_tools = [str(name).lower() for name in task.get("task_tools", []) if isinstance(name, str)]
    task_types = [str(name).lower() for name in model_entry.get("task_types", []) if isinstance(name, str)]
    if task_name in task_types:
        return True
    if any(task_type in task_name or task_name in task_type for task_type in task_types):
        return True
    role = str(model_entry.get("role", "")).lower()
    if role == "code_worker":
        return any(keyword in task_name for keyword in ("code", "python", "debug", "unit_test", "unit_tests", "function", "代码", "函数", "调试", "单元测试"))
    if role == "light_text_worker":
        return any(keyword in task_name for keyword in ("summarize", "summary", "translate", "classify", "rewrite", "extract", "text", "intent", "field", "总结", "摘要", "翻译", "分类", "改写", "抽取"))
    return any(tool in task_types for tool in task_tools)

# 专家模型选择链路的顶层入口函数
def _select_resources_for_plan(
    plan: dict,
    config_path: Path,
    tools_config: str | Path | None = None,
    toolset: str | None = None,
) -> dict:
    """
    为 plan 中的每个 task 填充 selected_tool / selected_model。
    资源选择采用轻量打分：
    - planner 给出的 task_tools 只是提示，不再无条件选择第一个
    - 根据任务名、参数、文件后缀和必填参数完整度选择真实 B3 工具
    - 没有合适工具时，再按抽象 task 名称匹配专家模型
    """
    expert_models = _load_expert_models(config_path)
    available_tools = _configured_tool_names(tools_config, config_path, toolset)
    selected_plan = deepcopy(plan)
    tasks = selected_plan.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
        selected_plan["tasks"] = tasks
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_tools = task.get("task_tools", [])
        if not isinstance(task_tools, list):
            task_tools = []
        task_tools = [
            tool
            for tool in task_tools
            if isinstance(tool, str) and tool.strip() and tool not in PSEUDO_MODEL_TOOLS
        ]
        tool_candidates = _rank_tool_candidates({**task, "task_tools": task_tools}, available_tools)
        viable_tools = [item for item in tool_candidates if item["available"] and item["score"] > 0]
        selected_tool = viable_tools[0]["name"] if viable_tools else None
        model_candidates = [] if selected_tool else _rank_model_candidates(task, expert_models)
        candidate_models = [item["name"] for item in model_candidates]
        selected_model = candidate_models[0] if candidate_models else None
        task["task_tools"] = task_tools
        task["selected_tool"] = selected_tool
        task["selected_model"] = selected_model
        task["candidate_tools"] = tool_candidates[:5]
        task["candidate_models"] = candidate_models
        task["candidate_model_rankings"] = model_candidates[:5]
        if selected_model:
            task["selection_reason"] = (
                f"Ranked resource selection matched abstract task `{task.get('task')}` "
                f"to expert model `{selected_model}`."
            )
        elif selected_tool:
            top = viable_tools[0]
            task["selection_reason"] = (
                f"Ranked resource selection chose tool `{selected_tool}` "
                f"with score {top['score']}."
            )
        else:
            task["selection_reason"] = "No candidate tool or expert model was available for this task."
    selected_plan["resource_selection"] = {
        "source": "ranked_rule_based",
        "expert_model_count": len(expert_models),
        "available_tool_count": len(available_tools),
        "toolset": toolset,
        "selected_task_count": len(tasks),
    }
    return selected_plan


def _plan_to_text(plan: dict) -> str:
    """把 plan 转成可读文本，用于注入 prompt 或调试。"""
    steps = plan.get("steps", [])
    lines = ["Plan:"]
    for index, step in enumerate(steps, 1):
        lines.append(f"{index}. {step}")
    tasks = plan.get("tasks") or []
    if tasks:
        lines.append("Task list:")
        for task in tasks:
            dependencies = task.get("dependencies") or []
            dep_text = ", ".join(dependencies) if dependencies else "none"
            task_tools = task.get("task_tools") or []
            tool_text = ", ".join(task_tools) if task_tools else "none"
            lines.append(f"- {task.get('id')}: {task.get('task')} tools={tool_text} deps={dep_text} args={json.dumps(task.get('args', {}), ensure_ascii=False)}")
    tool_names = plan.get("tool_names") or []
    if tool_names:
        lines.append("Suggested tools: " + ", ".join(tool_names))
    summary = plan.get("summary")
    if summary:
        lines.append("Plan summary: " + str(summary))
    return "\n".join(lines)


def _inject_plan_messages(messages: list[dict], plan: dict) -> list[dict]:
    """
    把计划作为额外 system 指令注入到消息中。

    这是早期 plan guidance 路径保留下来的 helper，目前主流程主要使用
    execute_plan 后的 task_results_as_messages。
    """
    planned_messages = deepcopy(messages)
    plan_instruction = (
        "\n\nPlan-and-Execute guidance:\n"
        + _plan_to_text(plan)
        + "\nUse this plan as guidance. Still obey the final AIMessage JSON output format."
    )
    if planned_messages and planned_messages[0].get("role") == "system":
        planned_messages[0]["content"] += plan_instruction
    else:
        planned_messages.insert(0, {"role": "system", "content": plan_instruction.strip()})
    return planned_messages


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_expert_config_path() -> Path:
    return _project_root() / "configs" / "expert_models.yaml"


# ---------------------------------------------------------------------------
# 专家模型 runner：执行 selected_model 不为空的模型型 task
# ---------------------------------------------------------------------------

def load_expert_models(config_path: str | Path | None = None) -> dict[str, dict]:
    """读取专家模型注册表。"""
    path = Path(config_path) if config_path else _default_expert_config_path()
    config = read_yaml(path)
    models = config.get("expert_models", {}) if isinstance(config, dict) else {}
    return models if isinstance(models, dict) else {}


def find_expert_model(model_id: str, config_path: str | Path | None = None) -> dict[str, Any]:
    """根据 model_id 查找专家模型配置。"""
    for model_key, entry in load_expert_models(config_path).items():
        if not isinstance(entry, dict):
            continue
        if model_id in {model_key, entry.get("model_id")}:
            return entry
    raise ValueError(f"expert model is not registered: {model_id}")


def _torch_dtype(dtype_name: str | None):
    import torch

    if dtype_name == "float16":
        return torch.float16
    if dtype_name == "bfloat16":
        return torch.bfloat16
    return torch.float32


def _task_prompt(task: dict[str, Any]) -> str:
    """把 TaskExecutionResult 风格的 task 转成专家模型输入 prompt。"""
    args = task.get("args", {})
    if not isinstance(args, dict):
        args = {}
    if isinstance(args.get("instruction"), str):
        return args["instruction"]
    if isinstance(args.get("prompt"), str):
        return args["prompt"]
    if isinstance(args.get("text"), str):
        return f"Task: {task.get('task')}\nText: {args['text']}"
    return f"Task: {task.get('task')}\nArgs: {json.dumps(args, ensure_ascii=False)}"

# 专家模型任务独立执行器
def run_expert_task(
    task: dict[str, Any],
    config_path: str | Path | None = None,
    max_new_tokens: int = 128,
) -> dict[str, Any]:
    """
    执行一个专家模型 task，并封装为 TaskExecutionResult。

    这里是真实加载 transformers 模型的路径；如果本机模型或 torch 环境不可用，
    错误会被捕获进 result["error"]，不会让整个 plan 执行崩掉。
    """
    started = time.perf_counter()
    task_id = str(task.get("id") or task.get("task_id") or "task_001")
    task_name = str(task.get("task") or "")
    selected_model = task.get("selected_model")
    selected_tool = task.get("selected_tool")
    dependencies = task.get("dependencies", [])
    if not isinstance(dependencies, list):
        dependencies = []
    dependencies = [str(item) for item in dependencies]

    try:
        # 加载选定模型
        if not selected_model:
            raise ValueError("task has no selected_model; run tool execution instead")
        entry = find_expert_model(str(selected_model), config_path)
        if entry.get("backend", "transformers") != "transformers":
            raise ValueError(f"unsupported expert backend: {entry.get('backend')}")

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_path = str(entry["model_name_or_path"])
        tokenizer_path = str(entry.get("tokenizer_name_or_path", model_path))
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path,
            local_files_only=bool(entry.get("local_files_only", True)),
            trust_remote_code=bool(entry.get("trust_remote_code", True)),
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=bool(entry.get("local_files_only", True)),
            trust_remote_code=bool(entry.get("trust_remote_code", True)),
            torch_dtype=_torch_dtype(entry.get("torch_dtype")),
        )
        if str(entry.get("device_map", "cpu")).lower() == "cuda" and torch.cuda.is_available():
            model = model.to("cuda")

        inputs = tokenizer.apply_chat_template(
            [{"role": "user", "content": _task_prompt(task)}],
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )
        if next(model.parameters()).is_cuda:
            inputs = {key: value.to("cuda") for key, value in inputs.items()}
        with torch.no_grad():
            generated_ids = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        new_tokens = generated_ids[0][inputs["input_ids"].shape[-1] :]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

        return make_task_execution_result(
            task_id=task_id,
            task=task_name,
            status="success",
            input_data=task.get("args") if isinstance(task.get("args"), dict) else {},
            output={"text": text},
            selected_tool=selected_tool if isinstance(selected_tool, str) else None,
            selected_model=str(selected_model),
            executor_type="model",
            dependencies=dependencies,
            resources={"resource_id": f"resource-{task_id}", "resource_type": "model_text"},
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as exc:
        return make_task_execution_result(
            task_id=task_id,
            task=task_name or "unknown_task",
            status="error",
            input_data=task.get("args") if isinstance(task.get("args"), dict) else {},
            output=None,
            error=make_error_payload(exc),
            selected_tool=selected_tool if isinstance(selected_tool, str) else None,
            selected_model=str(selected_model) if selected_model else None,
            executor_type="model",
            dependencies=dependencies,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def generate_final_response(user_request: str, task_results: list[dict]) -> dict:
    """
    规则版最终回答生成。

    mock 模式和模型合成失败时会走这里，用 task_results 中最后一个有效输出拼最终回答。
    """
    failed = [item for item in task_results if item.get("status") != "success"]
    successful = [item for item in task_results if item.get("status") == "success"]
    if failed:
        failed_ids = ", ".join(str(item.get("task_id")) for item in failed)
        content = f"任务执行未完全成功，失败或跳过的任务包括：{failed_ids}。"
    else:
        final_text = ""
        for item in reversed(successful):
            output = item.get("output") or {}
            if isinstance(output, dict) and isinstance(output.get("text"), str) and output["text"].strip():
                final_text = output["text"].strip()
                break
        if not final_text and successful:
            final_text = json.dumps(successful[-1].get("output", {}), ensure_ascii=False)
        content = final_text or "任务已完成，但没有可展示的文本结果。"
    return make_ai_message(content=content, tool_calls=[])


def get_nested(data: Any, dotted_path: str) -> Any:
    """从嵌套 dict 中按 a.b.c 路径取值。"""
    current = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def resolve_value(value: Any, results_by_task_id: dict[str, dict]) -> Any:
    """
    解析 task args 中引用前序任务输出的占位符。

    支持形如 {{task_001.output.field}} 的字符串，也支持 list/dict 递归解析。
    """
    if isinstance(value, str):
        matches = list(PLACEHOLDER_RE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].group(0) == value:
            match = matches[0]
            task_id = match.group("task_id")
            if task_id not in results_by_task_id:
                raise ValueError(f"placeholder depends on missing task result: {task_id}")
            return get_nested(results_by_task_id[task_id].get("output", {}), match.group("field"))

        def replace_match(match: re.Match) -> str:
            task_id = match.group("task_id")
            if task_id not in results_by_task_id:
                raise ValueError(f"placeholder depends on missing task result: {task_id}")
            return str(get_nested(results_by_task_id[task_id].get("output", {}), match.group("field")))

        return PLACEHOLDER_RE.sub(replace_match, value)
    if isinstance(value, list):
        return [resolve_value(item, results_by_task_id) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, results_by_task_id) for key, item in value.items()}
    return value


def dependencies_succeeded(task: dict, results_by_task_id: dict[str, dict]) -> bool:
    """判断 task 声明的 dependencies 是否都已成功。"""
    dependencies = task.get("dependencies", [])
    if not isinstance(dependencies, list):
        return False
    return all(dep in results_by_task_id and results_by_task_id[dep].get("status") == "success" for dep in dependencies)


def topological_sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按依赖关系对规划任务排序"""
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")

    task_by_id: dict[str, dict[str, Any]] = {}
    original_order: dict[str, int] = {}

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task at index {index} must be an object")

        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"task at index {index} is missing a valid id")

        task_id = task_id.strip()
        if task_id in task_by_id:
            raise ValueError(f"duplicate task id: {task_id}")

        task_by_id[task_id] = task
        original_order[task_id] = index

    dependencies_by_id: dict[str, list[str]] = {}

    for task_id, task in task_by_id.items():
        raw_dependencies = task.get("dependencies", [])
        if raw_dependencies is None:
            raw_dependencies = []

        if not isinstance(raw_dependencies, list):
            raise ValueError(f"dependencies of task {task_id} must be a list")

        dependencies: list[str] = []
        for dep in raw_dependencies:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError(f"dependency of task {task_id} must be a non-empty string")

            dep_id = dep.strip()
            if dep_id == task_id:
                raise ValueError(f"task {task_id} cannot depend on itself")

            if dep_id not in task_by_id:
                raise ValueError(f"task {task_id} depends on missing task id: {dep_id}")

            dependencies.append(dep_id)

        dependencies_by_id[task_id] = dependencies

    sorted_task_ids: list[str] = []
    state: dict[str, str] = {}
    visiting_stack: list[str] = []

    def visit(task_id: str) -> None:
        current_state = state.get(task_id)

        if current_state == "done":
            return

        if current_state == "visiting":
            cycle_start = visiting_stack.index(task_id)
            cycle = visiting_stack[cycle_start:] + [task_id]
            raise ValueError(f"circular task dependencies detected: {' -> '.join(cycle)}")

        state[task_id] = "visiting"
        visiting_stack.append(task_id)

        dependencies = dependencies_by_id.get(task_id, [])
        dependencies = sorted(dependencies, key=lambda dep_id: original_order[dep_id])

        for dep_id in dependencies:
            visit(dep_id)

        visiting_stack.pop()
        state[task_id] = "done"
        sorted_task_ids.append(task_id)

    for task_id in sorted(task_by_id, key=lambda item: original_order[item]):
        visit(task_id)

    return [deepcopy(task_by_id[task_id]) for task_id in sorted_task_ids]

def dependency_result(task: dict, reason: str) -> dict:
    """当依赖未满足时，生成 skipped 状态的 TaskExecutionResult。"""
    dependencies = task.get("dependencies", [])
    if not isinstance(dependencies, list):
        dependencies = []
    return make_task_execution_result(
        task_id=str(task.get("id") or task.get("task_id") or "unknown_task"),
        task=str(task.get("task") or "unknown_task"),
        status="skipped",
        input_data=task.get("args") if isinstance(task.get("args"), dict) else {},
        output=None,
        error={"type": "SkippedTask", "code": "DEPENDENCY_NOT_READY", "message": reason},
        selected_tool=task.get("selected_tool") if isinstance(task.get("selected_tool"), str) else None,
        selected_model=task.get("selected_model") if isinstance(task.get("selected_model"), str) else None,
        executor_type="model" if task.get("selected_model") else "tool",
        dependencies=[str(dep) for dep in dependencies],
    )


def _tool_message_to_task_result(task: dict, tool_message: dict) -> dict:
    """把 B3 返回的 ToolMessage 转成 plan 使用的 TaskExecutionResult。"""
    try:
        skill_result = json.loads(tool_message.get("content", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        skill_result = {
            "status": "error",
            "output": None,
            "error": {"type": type(exc).__name__, "code": "INVALID_TOOL_MESSAGE", "message": str(exc)},
            "latency_ms": None,
        }
    if not isinstance(skill_result, dict):
        skill_result = {
            "status": "error",
            "output": None,
            "error": {"type": "InvalidToolMessage", "code": "INVALID_TOOL_MESSAGE", "message": "ToolMessage content is not an object"},
            "latency_ms": None,
        }
    dependencies = task.get("dependencies", [])
    if not isinstance(dependencies, list):
        dependencies = []
    selected_tool = task.get("selected_tool") if isinstance(task.get("selected_tool"), str) else tool_message.get("name")
    return make_task_execution_result(
        task_id=str(task.get("id") or task.get("task_id") or "unknown_task"),
        task=str(task.get("task") or "unknown_task"),
        status="success" if skill_result.get("status") == "success" else "error",
        input_data=task.get("args") if isinstance(task.get("args"), dict) else {},
        output=skill_result.get("output"),
        error=skill_result.get("error"),
        selected_tool=selected_tool if isinstance(selected_tool, str) else None,
        selected_model=task.get("selected_model") if isinstance(task.get("selected_model"), str) else None,
        executor_type="tool",
        dependencies=[str(dep) for dep in dependencies],
        resources={
            "resource_id": f"resource-{task.get('id')}",
            "resource_type": "tool_output",
            "tool_call_id": tool_message.get("tool_call_id"),
        },
        latency_ms=skill_result.get("latency_ms"),
    )


def run_tool_task_via_b3(
    task: dict,
    output_dir: Path,
    tools_config: str | Path,
    toolset: str | None,
) -> dict:
    """
    执行工具型 task。

    这里不直接调用 skill，而是复用 B3 的 execute_tool_calls，
    保持 tool schema、参数修复、日志和缓存行为都与主工具层一致。
    """
    selected_tool = task.get("selected_tool")
    if not isinstance(selected_tool, str) or not selected_tool:
        raise ValueError("task has no selected_tool")
    args = task.get("args") if isinstance(task.get("args"), dict) else {}
    if selected_tool == "python_unit_test_sandbox" and isinstance(args.get("code"), str):
        match = CODE_FENCE_RE.match(args["code"])
        if match:
            args = dict(args)
            args["code"] = match.group("code").strip()
            task["args"] = args
    task_id = str(task.get("id") or task.get("task_id") or "unknown_task")
    task_output_dir = output_dir / task_id
    tool_messages = execute_tool_calls(
        [{"id": task_id, "name": selected_tool, "args": args}],
        str(tools_config),
        toolset,
        str(task_output_dir),
    )
    if not tool_messages:
        raise ValueError(f"B3 returned no ToolMessage for task: {task_id}")
    return _tool_message_to_task_result(task, tool_messages[0])


def execute_plan(
    selected_plan: dict,
    output_dir: str | Path,
    *,
    tools_config: str | Path | None = None,
    toolset: str | None = None,
    expert_config_path: str | Path | None = None,
    max_new_tokens: int = 96,
) -> list[dict]:
    """
    按顺序执行一个已经完成资源选择的 plan。

    每个 task 有两条执行路径：
    - selected_model 非空：走专家模型
    - 否则：走 B3 工具执行
    """
    output_path = ensure_dir(output_dir)
    task_results = []
    results_by_task_id: dict[str, dict] = {}

    for raw_task in selected_plan.get("tasks", []):
        task = copy.deepcopy(raw_task)
        task_id = str(task.get("id") or task.get("task_id") or "unknown_task")
        if not dependencies_succeeded(task, results_by_task_id):
            result = dependency_result(task, "one or more dependencies did not complete successfully")
        else:
            try:
                task["args"] = resolve_value(task.get("args", {}), results_by_task_id)
                if task.get("selected_model"):
                    result = run_expert_task(task, config_path=expert_config_path, max_new_tokens=max_new_tokens)
                else:
                    if tools_config is None:
                        raise ValueError("tools_config is required for tool-backed plan tasks")
                    result = run_tool_task_via_b3(task, output_path, tools_config, toolset)
            except Exception as exc:
                result = make_task_execution_result(
                    task_id=task_id,
                    task=str(task.get("task") or "unknown_task"),
                    status="error",
                    input_data=task.get("args") if isinstance(task.get("args"), dict) else {},
                    output=None,
                    error=make_error_payload(exc),
                    selected_tool=task.get("selected_tool") if isinstance(task.get("selected_tool"), str) else None,
                    selected_model=task.get("selected_model") if isinstance(task.get("selected_model"), str) else None,
                    executor_type="model" if task.get("selected_model") else "tool",
                    dependencies=[str(dep) for dep in task.get("dependencies", [])]
                    if isinstance(task.get("dependencies", []), list)
                    else [],
                )
        task_results.append(result)
        results_by_task_id[task_id] = result
    return task_results


def rule_based_final_response(user_request: str, task_results: list[dict]) -> dict:
    """对外保留的规则版最终回答接口。"""
    return generate_final_response(user_request, task_results)


def task_results_as_messages(user_request: str, selected_plan: dict, task_results: list[dict]) -> list[dict]:
    """把 plan 执行结果包装成 messages，交给本地模型做最终综合。"""
    return [
        {
            "role": "system",
            "content": (
                "You are synthesizing the final answer for a completed plan-and-execute run. "
                "Use the task results as evidence and answer the user's original request."
            ),
        },
        {
            "role": "user",
            "content": (
                "Original user request:\n"
                + user_request
                + "\n\nSelected execution plan JSON:\n"
                + json.dumps(selected_plan, ensure_ascii=False, indent=2)
                + "\n\nTask execution results JSON:\n"
                + json.dumps(task_results, ensure_ascii=False, indent=2)
                + "\n\nReturn the final answer in the standard AIMessage JSON format with content filled and tool_calls as []."
            ),
        },
    ]


def make_final_ai_message(content: str) -> dict:
    return make_ai_message(content=content, tool_calls=[])


# ---------------------------------------------------------------------------
# Plan 生成和最终合成：service.py 的 plan_and_execute 分支会调用这里
# ---------------------------------------------------------------------------

def _is_meta_task(task: dict) -> bool:
    """判断 task 是否只是“输出/结束流程”这类不应执行的元任务。"""
    task_name = str(task.get("task", "")).lower()
    return any(keyword in task_name for keyword in META_TASK_KEYWORDS)


def _normalize_plan(plan: dict) -> dict:
    """
    压缩明显过度规划的 plan。

    模型偶尔会把“最终回答、返回 JSON、结束流程”也写成 task。
    这些 task 不对应真实工具或专家模型，执行它们只会拉长 JSON 并增加截断概率。
    """
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("plan tasks must be a list")

    cleaned_tasks = []
    for task in tasks:
        if not isinstance(task, dict) or _is_meta_task(task):
            continue
        cleaned_task = deepcopy(task)
        task_tools = cleaned_task.get("task_tools", [])
        if not isinstance(task_tools, list):
            task_tools = []
        cleaned_task["task_tools"] = [
            tool
            for tool in task_tools
            if isinstance(tool, str) and tool.strip() and tool not in PSEUDO_MODEL_TOOLS
        ]
        cleaned_tasks.append(cleaned_task)
    if not cleaned_tasks:
        raise ValueError("plan contains no executable tasks after removing meta tasks")
    if len(cleaned_tasks) > MAX_PLAN_TASKS:
        cleaned_tasks = cleaned_tasks[:MAX_PLAN_TASKS]

    normalized = deepcopy(plan)
    normalized["tasks"] = cleaned_tasks
    normalized["steps"] = [
        f"{task.get('id', f'task_{index:03d}')}: {task.get('task', 'unknown_task')}"
        for index, task in enumerate(cleaned_tasks, 1)
    ]
    normalized["tool_names"] = [
        tool
        for task in cleaned_tasks
        for tool in task.get("task_tools", [])
        if isinstance(tool, str) and tool.strip()
    ]
    normalized["summary"] = str(normalized.get("summary") or "Concise executable plan.").strip()
    normalized["normalized"] = True
    normalized["max_task_limit"] = MAX_PLAN_TASKS
    return normalized

def _generate_plan(
    config_path: Path,
    config: dict,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str,
) -> tuple[dict, str | None, dict | None]:
    """
    生成 plan。

    mock 模式直接规则生成；prompt_json 模式调用模型生成，解析失败时 fallback 到 mock plan。
    返回值中的 error 只表示 plan 解析是否 fallback，不一定表示整体失败。
    """
    if mode == "mock":
        plan = _mock_generate_plan(messages, tools_schema)
        plan = _normalize_plan(plan)
        raw_text = json.dumps(plan, ensure_ascii=False)
        return plan, raw_text, None
    if mode == "prompt_json":
        raw_text = _prompt_json_generate_plan(config_path, config, messages, tools_schema)
        raw_text = _strip_trailing_newlines(raw_text)
        try:
            plan = _parse_plan_output(raw_text)
            plan = _normalize_plan(plan)
            return plan, raw_text, None
        except Exception as exc:
            fallback = _mock_generate_plan(messages, tools_schema)
            fallback = _normalize_plan(fallback)
            fallback["source"] = "fallback_after_parse_error"
            return fallback, raw_text, {"type": type(exc).__name__, "message": str(exc)}
    raise ValueError("mode must be mock or prompt_json")


def _default_tools_config(config_path: Path) -> Path:
    """默认使用 model.yaml 同目录下的 tools.yaml。"""
    return config_path.parent / "tools.yaml"


def _synthesize_final_ai_message(
    config_path: Path,
    config: dict,
    messages: list[dict],
    selected_plan: dict,
    task_results: list[dict],
    mode: str,
    schema_passing: str,
) -> tuple[dict, str | None, dict | None, dict]:
    """
    把执行结果合成最终 AIMessage。

    mock 模式直接用规则答案；prompt_json 模式会再调用一次本地模型做总结。
    如果最终总结模型输出解析失败，会 fallback 到规则答案。
    """
    user_request = _latest_user_text(messages)
    fallback = rule_based_final_response(user_request, task_results)
    if mode == "mock":
        raw_text = json.dumps({"content": fallback["content"], "tool_calls": []}, ensure_ascii=False)
        metadata = {
            "schema_passing": schema_passing,
            "requested_native_tools": schema_passing == "native_tools",
            "native_tools_applied": None,
            "fallback_to_prompt_injection": False,
            "fallback_reason": None,
            "input_token_count": None,
            "output_token_count": None,
            "total_token_count": None,
            "schema_text_chars": 0,
            "available_tool_names": [],
            "mock_note": "mock mode uses the rule-based plan execution summary",
        }
        return fallback, raw_text, None, metadata
    try:
        final_messages = task_results_as_messages(user_request, selected_plan, task_results)
        raw_text, metadata = _prompt_json_generate(
            config_path,
            config,
            final_messages,
            [],
            schema_passing,
        )
        raw_text = _strip_trailing_newlines(raw_text)
        parsed_candidate, ai_message = _parse_model_output(raw_text)
        if ai_message.get("tool_calls"):
            raise ValueError("final synthesis must not request additional tool calls")
        metadata["final_parsed_candidate"] = parsed_candidate
        return ai_message, raw_text, None, metadata
    except Exception as exc:
        metadata = {
            "schema_passing": schema_passing,
            "requested_native_tools": schema_passing == "native_tools",
            "native_tools_applied": None,
            "fallback_to_prompt_injection": True,
            "fallback_reason": f"{type(exc).__name__}: {exc}",
            "input_token_count": None,
            "output_token_count": None,
            "total_token_count": None,
            "schema_text_chars": 0,
            "available_tool_names": [],
        }
        return fallback, None, {"type": type(exc).__name__, "message": str(exc)}, metadata
