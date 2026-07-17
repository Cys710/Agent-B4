from __future__ import annotations

"""
B4 对外服务入口。

本文件只负责“调度”：校验输入，根据运行模式选择普通生成或
plan-and-execute 流程，并把 AIMessage、原始输出、plan 等产物写入 outdir。

阅读顺序建议：
1. 先看 generate_ai_message 的 planning == "plan_and_execute" 分支。
2. 再看普通 mock / prompt_json 分支。
3. 最后看 artifact 写盘逻辑。
"""

import json
from copy import deepcopy
from pathlib import Path

from common.io_utils import append_jsonl, write_json
from common.logging_utils import now_iso
from common.schemas import make_ai_message, validate_messages

from .generation import (
    PARSE_ERROR_CONTENT,
    SCHEMA_PASSING_MODES,
    _artifact_paths,
    _load_model_config,
    _mock_generate,
    _plan_artifact_path,
    _parse_model_output,
    _prompt_json_generate,
    _schema_passing_artifact_path,
    _schema_text_chars,
    _strip_trailing_newlines,
    _tool_names_from_schema,
)
from .planning import (
    _default_tools_config,
    _generate_plan,
    _select_resources_for_plan,
    _synthesize_final_ai_message,
    execute_plan,
)

def generate_ai_message(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str = "prompt_json",
    artifact_dir: str | None = None,
    artifact_stem: str | None = None,
    planning: str = "none",
    schema_passing: str = "prompt_injection",
    tools_config: str | None = None,
    toolset: str | None = None,
    expert_config_path: str | None = None,
) -> dict:
    """
    生成一个标准 AIMessage。

    B1 只需要调用这个函数，不需要关心 B4 内部到底是 mock、本地模型，
    还是 plan-and-execute。返回值中一定包含：
    - ai_message: 标准 assistant 消息
    - status/error: 本轮 B4 生成状态
    - plan/task_results: 只有 planning=plan_and_execute 时才有实际内容
    - schema_metadata: schema 传递方式、token 数、fallback 信息等
    """
    
    config_path, config = _load_model_config(model_config)
    messages = validate_messages(deepcopy(messages))
    if not isinstance(tools_schema, list):
        raise ValueError("tools_schema must be an array")
    if planning not in {"none", "plan_and_execute"}:
        raise ValueError("planning must be none or plan_and_execute")
    if schema_passing not in SCHEMA_PASSING_MODES:
        raise ValueError("schema_passing must be prompt_injection or native_tools")

    generated_at = now_iso()
    backend = "mock" if mode == "mock" else config.get("model", {}).get("backend", "transformers")
    plan_record = None
    task_results = None

    # 分支一：Plan-and-Execute。
    # 先让 B4 生成计划，再选择资源，然后执行任务，最后把任务结果合成最终回答。
    if planning == "plan_and_execute":
        # 生成计划
        plan, plan_raw_text, plan_error = _generate_plan(config_path, config, messages, tools_schema, mode)
        execution_dir = Path(artifact_dir or "plan_execution")
        if artifact_stem:
            execution_dir = execution_dir / f"{artifact_stem}_plan_execution"
        resolved_tools_config = Path(tools_config).resolve() if tools_config else _default_tools_config(config_path)
        resolved_expert_config = Path(expert_config_path).resolve() if expert_config_path else config_path.parent / "expert_models.yaml"
        # 选择资源
        plan = _select_resources_for_plan(plan, config_path, resolved_tools_config, toolset)
        execution_config = config.get("plan_execution", {})
        if not isinstance(execution_config, dict):
            execution_config = {}
        # 执行计划
        task_results = execute_plan(
            plan,
            execution_dir,
            tools_config=resolved_tools_config,
            toolset=toolset,
            expert_config_path=resolved_expert_config,
            max_new_tokens=int(execution_config.get("max_new_tokens", 128)),
        )
        # 记录结果
        plan_record = {
            "planning": planning,
            "mode": mode,
            "backend": backend,
            "raw_text": plan_raw_text,
            "plan": plan,
            "task_results": task_results,
            "execution_dir": str(execution_dir),
            "tools_config": str(resolved_tools_config),
            "toolset": toolset,
            "expert_config_path": str(resolved_expert_config),
            "status": "partial_or_error"
            if any(item.get("status") != "success" for item in task_results)
            else "fallback"
            if plan_error
            else "success",
            "error": plan_error,
            "generated_at": generated_at,
        }
        ai_message, raw_text, final_error, schema_metadata = _synthesize_final_ai_message(
            config_path,
            config,
            messages,
            plan,
            task_results,
            mode,
            schema_passing,
        )
        parsed_candidate = {"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}
        if final_error:
            plan_record["final_synthesis_error"] = final_error
        status = "success"
        error = None
    else:
        messages_for_generation = messages

    # 分支二：普通 mock 模式。
    # 不加载模型，按规则直接给出工具调用或基于 ToolMessage 生成最终回答。
    if planning != "plan_and_execute" and mode == "mock":
        ai_message = _mock_generate(messages_for_generation)
        raw_text = json.dumps({"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}, ensure_ascii=False)
        parsed_candidate = {"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}
        schema_metadata = {
            "schema_passing": schema_passing,
            "requested_native_tools": schema_passing == "native_tools",
            "native_tools_applied": None,
            "fallback_to_prompt_injection": False,
            "fallback_reason": None,
            "input_token_count": None,
            "output_token_count": None,
            "total_token_count": None,
            "schema_text_chars": _schema_text_chars(tools_schema),
            "available_tool_names": _tool_names_from_schema(tools_schema),
            "mock_note": "mock mode records the selected schema passing strategy without loading a model",
        }
        status = "success"
        error = None

    # 分支三：普通本地模型模式。
    # prompt_json_generate 只负责拿到模型原始文本；parse_model_output 负责转成 AIMessage。
    elif planning != "plan_and_execute" and mode == "prompt_json":
        # 模型输出
        raw_text, schema_metadata = _prompt_json_generate(
            config_path,
            config,
            messages_for_generation,
            tools_schema,
            schema_passing,
        )
        raw_text = _strip_trailing_newlines(raw_text)
        try:
            parsed_candidate, ai_message = _parse_model_output(raw_text)
            status = "success"
            error = None
        except Exception as exc:
            parsed_candidate = None
            ai_message = make_ai_message(PARSE_ERROR_CONTENT, [])
            status = "error"
            error = {"type": type(exc).__name__, "message": str(exc)}
    elif planning != "plan_and_execute":
        raise ValueError("mode must be mock or prompt_json")

    raw_record = {
        "mode": mode,
        "backend": backend,
        "raw_text": raw_text,
        "parsed_candidate": parsed_candidate,
        "status": status,
        "error": error,
        "generated_at": generated_at,
        "multi_tool_enabled": True,
        "planning": planning,
        "plan": plan_record["plan"] if plan_record else None,
        "task_results": task_results,
        "schema_passing": schema_passing,
        "schema_metadata": schema_metadata,
    }

    # B4 的 artifact 设计：raw_model_output.json 记录调试所需的完整上下文，
    # ai_message.json 只保存给 B1 继续使用的标准消息。
    if artifact_dir:
        raw_path, message_path, log_path = _artifact_paths(artifact_dir, artifact_stem)
        write_json(raw_record, raw_path)
        write_json(ai_message, message_path)
        plan_path = None
        if plan_record:
            plan_path = _plan_artifact_path(artifact_dir, artifact_stem)
            write_json(plan_record, plan_path)
        append_jsonl(
            {
                "timestamp": generated_at,
                "mode": mode,
                "status": status,
                "raw_output_path": str(raw_path),
                "ai_message_path": str(message_path),
                "plan_path": str(plan_path) if plan_path else None,
                "planning": planning,
                "schema_passing": schema_passing,
                "native_tools_applied": schema_metadata.get("native_tools_applied"),
                "fallback_to_prompt_injection": schema_metadata.get("fallback_to_prompt_injection"),
                "tool_call_count": len(ai_message.get("tool_calls", [])),
                "error": error,
            },
            log_path,
        )
    return {
        "ai_message": ai_message,
        "status": status,
        "error": error,
        "plan": plan_record["plan"] if plan_record else None,
        "task_results": task_results,
        "schema_metadata": schema_metadata,
    }

# 对比两种 schema 传递方式。
def compare_schema_passing_methods(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str,
    artifact_dir: str,
    planning: str = "none",
) -> dict:
    """对比两种 schema 传递方式。

    prompt_injection: 把 tools schema 直接拼进 prompt。
    native_tools: 通过 tokenizer.apply_chat_template(..., tools=...) 传入。
    这个函数主要用于实验和报告，不是 B1 主流程必须调用的函数。
    """
    generated_at = now_iso()
    results = []
    for schema_passing in ("prompt_injection", "native_tools"):
        result = generate_ai_message(
            model_config,
            messages,
            tools_schema,
            mode,
            artifact_dir,
            artifact_stem=schema_passing,
            planning=planning,
            schema_passing=schema_passing,
        )
        ai_message = result["ai_message"]
        metadata = result["schema_metadata"]
        results.append(
            {
                "schema_passing": schema_passing,
                "status": result["status"],
                "error": result["error"],
                "tool_call_count": len(ai_message.get("tool_calls", [])),
                "tool_call_names": [call.get("name") for call in ai_message.get("tool_calls", [])],
                "has_final_content": bool(ai_message.get("content", "").strip()),
                "native_tools_applied": metadata.get("native_tools_applied"),
                "fallback_to_prompt_injection": metadata.get("fallback_to_prompt_injection"),
                "fallback_reason": metadata.get("fallback_reason"),
                "input_token_count": metadata.get("input_token_count"),
                "raw_output_path": str(Path(artifact_dir) / f"{schema_passing}_raw_model_output.json"),
                "ai_message_path": str(Path(artifact_dir) / f"{schema_passing}_ai_message.json"),
            }
        )
    prompt_result = next(item for item in results if item["schema_passing"] == "prompt_injection")
    native_result = next(item for item in results if item["schema_passing"] == "native_tools")
    token_delta = None
    if prompt_result["input_token_count"] is not None and native_result["input_token_count"] is not None:
        token_delta = native_result["input_token_count"] - prompt_result["input_token_count"]
    report = {
        "generated_at": generated_at,
        "mode": mode,
        "planning": planning,
        "tool_count": len(tools_schema),
        "available_tool_names": _tool_names_from_schema(tools_schema),
        "schema_text_chars": _schema_text_chars(tools_schema),
        "comparison": results,
        "summary": {
            "same_tool_call_count": prompt_result["tool_call_count"] == native_result["tool_call_count"],
            "same_tool_call_names": prompt_result["tool_call_names"] == native_result["tool_call_names"],
            "native_minus_prompt_input_tokens": token_delta,
            "native_used_without_fallback": (
                None
                if mode == "mock"
                else bool(native_result["native_tools_applied"])
                and not bool(native_result["fallback_to_prompt_injection"])
            ),
        },
    }
    write_json(report, _schema_passing_artifact_path(artifact_dir, None))
    return report
