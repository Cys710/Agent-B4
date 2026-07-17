from __future__ import annotations

"""Compatibility facade for B4.

The implementation now lives in the compact `b4/` package. This file keeps the
original CLI and import path stable for B1, B5, tests, and README commands.
"""

from b4.cli import build_parser, main
from b4.evaluation import (
    _args_match,
    _average_number,
    _batch_messages,
    _expected_calls,
    _expected_tool_names,
    _load_batch_eval_config,
    _load_schema_passing_eval_config,
    _match_expected_tool_calls,
    _predicted_tool_names,
    run_batch_tool_call_eval,
    run_schema_passing_batch_eval,
)
from b4.generation import (
    PARSE_ERROR_CONTENT,
    SCHEMA_PASSING_MODES,
    _MODEL_CACHE,
    _apply_chat_template_with_schema_passing,
    _artifact_paths,
    _available_schema_tool_names,
    _build_native_tool_messages,
    _build_plan_messages,
    _build_prompt_messages,
    _candidate_to_message,
    _dtype_value,
    _ensure_unique_tool_call_ids,
    _extract_tool_result,
    _latest_user_text,
    _load_model_bundle,
    _load_model_config,
    _mock_answer_from_tool_messages,
    _mock_generate,
    _mock_generate_plan,
    _mock_plan_calls_for_request,
    _mock_tool_calls_for_request,
    _model_cache_key,
    _parse_json_with_backtick_tail,
    _parse_fenced_json_block,
    _parse_model_output,
    _parse_native_tool_call_blocks,
    _parse_plan_output,
    _parse_tool_calls_fragment,
    _plan_artifact_path,
    _prompt_json_generate,
    _prompt_json_generate_plan,
    _safe_id,
    _schema_passing_artifact_path,
    _schema_text_chars,
    _shorten_text,
    _strip_trailing_newlines,
    _task_name_from_tool,
    _tool_names_from_schema,
)
from b4.planning import (
    _default_expert_config_path,
    _default_tools_config,
    _generate_plan,
    _inject_plan_messages,
    _load_expert_models,
    _plan_to_text,
    _project_root,
    _select_resources_for_plan,
    _synthesize_final_ai_message,
    _task_matches_model,
    _task_prompt,
    _torch_dtype,
    dependency_result,
    dependencies_succeeded,
    execute_plan,
    find_expert_model,
    generate_final_response,
    get_nested,
    load_expert_models,
    make_final_ai_message,
    resolve_value,
    rule_based_final_response,
    run_expert_task,
    run_tool_task_via_b3,
    task_results_as_messages,
)
from b4.service import compare_schema_passing_methods, generate_ai_message


if __name__ == "__main__":
    raise SystemExit(main())
