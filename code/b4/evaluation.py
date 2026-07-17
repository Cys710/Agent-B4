from __future__ import annotations

"""
B4 批量工具调用评估。

这个文件用于离线评估“模型是否选对工具调用”，不会参与 B1 主运行链路。
输入通常是 data/messages/b4_5batch_tool_call_eval_sample.json 这类 batch 配置。
"""

import json
from pathlib import Path

from common.io_utils import read_json, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_from_file

from .generation import _safe_id, _schema_text_chars, _tool_names_from_schema
from .service import generate_ai_message

# 拼装标准对话消息列表
def _batch_messages(system_prompt: str, user_text: str) -> list[dict]:
    """把单条评测 case 的用户文本包装成 messages。"""
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": user_text})
    return messages

# 读取测试用例里「标准答案工具调用」
def _expected_calls(case: dict) -> list[dict]:
    """读取评测样例中的期望工具调用。"""
    expected = case.get("expected_tool_calls", case.get("expected"))
    if isinstance(expected, dict):
        return [expected]
    if isinstance(expected, list):
        return [item for item in expected if isinstance(item, dict)]
    return []

# 比较工具参数是否匹配；exact=False 时允许 predicted 多带参数
def _args_match(expected_args: dict, predicted_args: dict, exact: bool) -> bool:
    """比较工具参数是否匹配；exact=False 时允许 predicted 多带参数。"""
    if not isinstance(expected_args, dict) or not isinstance(predicted_args, dict):
        return expected_args == predicted_args
    if exact and expected_args != predicted_args:
        return False
    for key, value in expected_args.items():
        if key not in predicted_args or predicted_args[key] != value:
            return False
    return True

# 工具调用匹配
def _match_expected_tool_calls(expected_calls: list[dict], predicted_calls: list[dict], exact_args: bool) -> dict:
    """逐个匹配 expected 和 predicted tool calls，并返回匹配明细。"""
    matched_indexes = set()
    matches = []
    for expected in expected_calls:
        expected_name = expected.get("name")
        expected_args = expected.get("args", {})
        match_index = None
        for index, predicted in enumerate(predicted_calls):
            if index in matched_indexes:
                continue
            if predicted.get("name") != expected_name:
                continue
            if _args_match(expected_args, predicted.get("args", {}), exact_args):
                match_index = index
                break
        if match_index is not None:
            matched_indexes.add(match_index)
            matches.append(
                {
                    "expected": expected,
                    "predicted": predicted_calls[match_index],
                    "matched": True,
                }
            )
        else:
            matches.append({"expected": expected, "predicted": None, "matched": False})
    return {
        "expected_count": len(expected_calls),
        "predicted_count": len(predicted_calls),
        "matched_count": sum(1 for item in matches if item["matched"]),
        "all_expected_matched": bool(expected_calls) and all(item["matched"] for item in matches),
        "matches": matches,
    }

# 计算有效值平均值
def _average_number(values: list[int | float | None]) -> float | None:
    clean = [float(value) for value in values if isinstance(value, (int, float))]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 3)

# 计算比率 / 成功率，保留 4 位小数
def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0

# 提取标准答案所有工具名称
def _expected_tool_names(expected_calls: list[dict]) -> list[str]:
    return [str(item.get("name")) for item in expected_calls if isinstance(item.get("name"), str)]

# 提取模型预测输出所有工具名称
def _predicted_tool_names(predicted_calls: list[dict]) -> list[str]:
    return [str(item.get("name")) for item in predicted_calls if isinstance(item.get("name"), str)]

# 仅工具名匹配成功判定
def _tool_name_success(expected_calls: list[dict], predicted_calls: list[dict], has_final_content: bool) -> bool:
    expected_names = _expected_tool_names(expected_calls)
    predicted_names = _predicted_tool_names(predicted_calls)
    if not expected_names:
        return not predicted_names and has_final_content
    remaining = list(predicted_names)
    for name in expected_names:
        if name not in remaining:
            return False
        remaining.remove(name)
    return True

# 预期所有工具名必须全部出现，数量一一对应，模型多输出工具不扣分
def _tool_call_success(expected_calls: list[dict], predicted_calls: list[dict], has_final_content: bool, exact_args: bool) -> tuple[bool, dict]:
    if not expected_calls:
        match = {
            "expected_count": 0,
            "predicted_count": len(predicted_calls),
            "matched_count": 0,
            "all_expected_matched": not predicted_calls and has_final_content,
            "matches": [],
        }
        return bool(match["all_expected_matched"]), match
    match = _match_expected_tool_calls(expected_calls, predicted_calls, exact_args)
    return bool(match["all_expected_matched"]), match

# 提取模型输出消息的核心特征
def _prediction_signature(ai_message: dict) -> dict:
    return {
        "content_present": bool(ai_message.get("content", "").strip()),
        "tool_calls": [
            {
                "name": call.get("name"),
                "args": call.get("args", {}),
            }
            for call in ai_message.get("tool_calls", [])
        ],
    }


def _load_schema_passing_eval_config(path: str | Path) -> tuple[Path, dict]:
    """读取拓展四 schema passing 批量评估配置。"""
    config_path = Path(path).resolve()
    config = read_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("schema passing eval config must contain an object")
    if not isinstance(config.get("cases"), list) or not config["cases"]:
        raise ValueError("schema passing eval config requires a non-empty cases array")
    return config_path, config


def _schema_stats(schema_passing: str, records: list[dict]) -> dict:
    total = len(records)
    return {
        "schema_passing": schema_passing,
        "total_cases": total,
        "parse_success_count": sum(1 for item in records if item["parse_success"]),
        "parse_success_rate": _rate(sum(1 for item in records if item["parse_success"]), total),
        "tool_name_success_count": sum(1 for item in records if item["tool_name_success"]),
        "tool_name_success_rate": _rate(sum(1 for item in records if item["tool_name_success"]), total),
        "tool_call_success_count": sum(1 for item in records if item["tool_call_success"]),
        "tool_call_success_rate": _rate(sum(1 for item in records if item["tool_call_success"]), total),
        "avg_input_tokens": _average_number([item["input_token_count"] for item in records]),
        "avg_output_tokens": _average_number([item["output_token_count"] for item in records]),
        "avg_total_tokens": _average_number([item["total_token_count"] for item in records]),
        "native_tools_applied_count": sum(1 for item in records if item["native_tools_applied"] is True),
        "fallback_to_prompt_injection_count": sum(1 for item in records if item["fallback_to_prompt_injection"] is True),
        "case_records": records,
    }


def _write_schema_passing_summary(report: dict, path: Path) -> Path:
    rows = report["summary_by_schema_passing"]
    lines = [
        "# Schema Passing Batch Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Mode: `{report['mode']}`",
        f"- Cases: `{report['total_cases']}`",
        f"- Tools: `{', '.join(report['available_tool_names'])}`",
        "",
        "| schema_passing | parse success | tool name success | tool call success | avg input tokens | avg output tokens | native applied | fallback |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in rows:
        lines.append(
            "| {schema} | {parse_ok}/{total} | {name_ok}/{total} | {call_ok}/{total} | {input_tokens} | {output_tokens} | {native} | {fallback} |".format(
                schema=item["schema_passing"],
                parse_ok=item["parse_success_count"],
                name_ok=item["tool_name_success_count"],
                call_ok=item["tool_call_success_count"],
                total=item["total_cases"],
                input_tokens=item["avg_input_tokens"],
                output_tokens=item["avg_output_tokens"],
                native=item["native_tools_applied_count"],
                fallback=item["fallback_to_prompt_injection_count"],
            )
        )
    delta = report["comparison_summary"]
    lines.extend(
        [
            "",
            "## Comparison",
            "",
            f"- Same prediction count: `{delta['same_prediction_count']}/{report['total_cases']}`",
            f"- Same prediction rate: `{delta['same_prediction_rate']}`",
            f"- Native minus prompt avg input tokens: `{delta['native_minus_prompt_avg_input_tokens']}`",
            f"- Native minus prompt tool-call success rate: `{delta['native_minus_prompt_tool_call_success_rate']}`",
            "",
        ]
    )
    write_text("\n".join(lines), path)
    return path


def run_schema_passing_batch_eval(
    batch_config_path: str,
    tools_schema: list[dict],
    outdir: str,
    model_config_override: str | None = None,
    mode_override: str | None = None,
) -> dict:
    """拓展四：固定一个模型，批量比较 prompt_injection 和 native_tools。"""
    batch_path, config = _load_schema_passing_eval_config(batch_config_path)
    output_dir = Path(outdir)
    generated_at = now_iso()
    system_prompt = str(
        config.get(
            "system_prompt",
            "You are a local tool-using agent. Select the correct tool calls before answering.",
        )
    )
    model_config_setting = model_config_override or config.get("model_config")
    if not isinstance(model_config_setting, str) or not model_config_setting:
        raise ValueError("schema passing batch eval requires --model_config or config.model_config")
    model_config = str(resolve_from_file(model_config_setting, batch_path))
    mode = mode_override or str(config.get("mode", "prompt_json"))
    exact_args = bool(config.get("exact_args", False))
    schema_methods = ["prompt_injection", "native_tools"]
    records_by_schema: dict[str, list[dict]] = {method: [] for method in schema_methods}
    case_pairs = []

    for case_index, case in enumerate(config["cases"], 1):
        if not isinstance(case, dict):
            raise ValueError("each schema passing case must be an object")
        case_id = _safe_id(str(case.get("id", f"case_{case_index:03d}")))
        user_text = case.get("user", case.get("user_input"))
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError(f"case {case_id} requires user or user_input")
        messages = _batch_messages(system_prompt, user_text)
        expected = _expected_calls(case)
        pair_predictions = {}
        for schema_passing in schema_methods:
            artifact_stem = f"{case_id}_{schema_passing}"
            result = generate_ai_message(
                model_config,
                messages,
                tools_schema,
                mode,
                str(output_dir / "case_outputs"),
                artifact_stem=artifact_stem,
                planning="none",
                schema_passing=schema_passing,
            )
            ai_message = result["ai_message"]
            predicted_calls = ai_message.get("tool_calls", [])
            has_final_content = bool(ai_message.get("content", "").strip())
            tool_call_success, match = _tool_call_success(expected, predicted_calls, has_final_content, exact_args)
            record = {
                "case_id": case_id,
                "schema_passing": schema_passing,
                "status": result["status"],
                "error": result["error"],
                "parse_success": result["status"] == "success",
                "expected_tool_calls": expected,
                "predicted_tool_calls": predicted_calls,
                "has_final_content": has_final_content,
                "tool_name_success": result["status"] == "success" and _tool_name_success(expected, predicted_calls, has_final_content),
                "tool_call_success": result["status"] == "success" and tool_call_success,
                "match": match,
                "input_token_count": result["schema_metadata"].get("input_token_count"),
                "output_token_count": result["schema_metadata"].get("output_token_count"),
                "total_token_count": result["schema_metadata"].get("total_token_count"),
                "native_tools_applied": result["schema_metadata"].get("native_tools_applied"),
                "fallback_to_prompt_injection": result["schema_metadata"].get("fallback_to_prompt_injection"),
                "fallback_reason": result["schema_metadata"].get("fallback_reason"),
                "raw_output_path": str(output_dir / "case_outputs" / f"{artifact_stem}_raw_model_output.json"),
                "ai_message_path": str(output_dir / "case_outputs" / f"{artifact_stem}_ai_message.json"),
            }
            records_by_schema[schema_passing].append(record)
            pair_predictions[schema_passing] = _prediction_signature(ai_message)
        case_pairs.append(
            {
                "case_id": case_id,
                "same_prediction": pair_predictions["prompt_injection"] == pair_predictions["native_tools"],
                "prompt_injection": pair_predictions["prompt_injection"],
                "native_tools": pair_predictions["native_tools"],
            }
        )

    summary_by_schema = [_schema_stats(method, records_by_schema[method]) for method in schema_methods]
    prompt_stats = summary_by_schema[0]
    native_stats = summary_by_schema[1]
    input_delta = None
    if prompt_stats["avg_input_tokens"] is not None and native_stats["avg_input_tokens"] is not None:
        input_delta = round(native_stats["avg_input_tokens"] - prompt_stats["avg_input_tokens"], 3)
    success_delta = round(native_stats["tool_call_success_rate"] - prompt_stats["tool_call_success_rate"], 4)
    same_prediction_count = sum(1 for item in case_pairs if item["same_prediction"])
    report = {
        "generated_at": generated_at,
        "batch_config": str(batch_path),
        "model_config": model_config,
        "mode": mode,
        "tool_count": len(tools_schema),
        "available_tool_names": _tool_names_from_schema(tools_schema),
        "schema_text_chars": _schema_text_chars(tools_schema),
        "exact_args": exact_args,
        "total_cases": len(config["cases"]),
        "summary_by_schema_passing": summary_by_schema,
        "comparison_summary": {
            "same_prediction_count": same_prediction_count,
            "same_prediction_rate": _rate(same_prediction_count, len(config["cases"])),
            "native_minus_prompt_avg_input_tokens": input_delta,
            "native_minus_prompt_tool_call_success_rate": success_delta,
        },
        "case_pairs": case_pairs,
    }
    write_json(report, output_dir / "schema_passing_batch_report.json")
    _write_schema_passing_summary(report, output_dir / "schema_passing_batch_summary.md")
    return report


def _load_batch_eval_config(path: str | Path) -> tuple[Path, dict]:
    """读取并校验 batch eval 配置。"""
    config_path = Path(path).resolve()
    config = read_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("batch eval config must contain an object")
    if not isinstance(config.get("cases"), list) or not config["cases"]:
        raise ValueError("batch eval config requires a non-empty cases array")
    if not isinstance(config.get("models"), list) or not config["models"]:
        raise ValueError("batch eval config requires a non-empty models array")
    return config_path, config


def _write_batch_tool_call_summary(report: dict, path: Path) -> Path:
    """写出便于展示的多模型工具调用评估汇总。"""
    lines = [
        "# B4 Batch Tool-Call Evaluation Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Cases: `{report['total_cases']}`",
        f"- Tools: `{', '.join(report['available_tool_names'])}`",
        f"- Exact args: `{report['exact_args']}`",
        "",
        "| model | series | parse success | tool name success | tool call success | avg input tokens | avg output tokens | avg total tokens |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["models"]:
        total = item["total_cases"]
        lines.append(
            "| {model} | {series} | {parse_ok}/{total} | {name_ok}/{total} | {call_ok}/{total} | {input_tokens} | {output_tokens} | {total_tokens} |".format(
                model=item["label"],
                series=item.get("series", ""),
                parse_ok=item["parse_success_count"],
                name_ok=item["tool_name_success_count"],
                call_ok=item["tool_call_success_count"],
                total=total,
                input_tokens=item["avg_input_tokens"],
                output_tokens=item["avg_output_tokens"],
                total_tokens=item["avg_total_tokens"],
            )
        )
    lines.append("")
    write_text("\n".join(lines), path)
    return path


def run_batch_tool_call_eval(
    batch_config_path: str,
    tools_schema: list[dict],
    outdir: str,
    mode_override: str | None = None,
    planning_override: str | None = None,
    schema_passing_override: str | None = None,
) -> dict:
    """运行批量工具调用评估，并写出 batch_tool_call_eval_report.json。"""
    batch_path, config = _load_batch_eval_config(batch_config_path)
    output_dir = Path(outdir)
    generated_at = now_iso()
    system_prompt = str(
        config.get(
            "system_prompt",
            "You are a local tool-using agent. Select the correct tool calls before answering.",
        )
    )
    default_mode = mode_override or str(config.get("mode", "mock"))
    default_planning = planning_override or str(config.get("planning", "none"))
    default_schema_passing = schema_passing_override or str(config.get("schema_passing", "prompt_injection"))
    exact_args = bool(config.get("exact_args", False))
    cases = config["cases"]
    model_reports = []
    all_case_records = []
    for model_index, model_entry in enumerate(config["models"], 1):
        if not isinstance(model_entry, dict):
            raise ValueError("each model entry must be an object")
        model_id = _safe_id(str(model_entry.get("id", f"model_{model_index}")))
        model_label = str(model_entry.get("label", model_id))
        model_series = str(model_entry.get("series", ""))
        model_config_setting = model_entry.get("model_config")
        if not isinstance(model_config_setting, str):
            raise ValueError(f"model entry {model_id} requires model_config")
        model_config = str(resolve_from_file(model_config_setting, batch_path))
        mode = mode_override or str(model_entry.get("mode", default_mode))
        planning = planning_override or str(model_entry.get("planning", default_planning))
        schema_passing = schema_passing_override or str(model_entry.get("schema_passing", default_schema_passing))
        case_records = []
        for case_index, case in enumerate(cases, 1):
            if not isinstance(case, dict):
                raise ValueError("each batch case must be an object")
            case_id = _safe_id(str(case.get("id", f"case_{case_index:03d}")))
            user_text = case.get("user", case.get("user_input"))
            if not isinstance(user_text, str) or not user_text.strip():
                raise ValueError(f"case {case_id} requires user or user_input")
            messages = _batch_messages(system_prompt, user_text)
            artifact_stem = f"{model_id}_{case_id}"
            result = generate_ai_message(
                model_config,
                messages,
                tools_schema,
                mode,
                str(output_dir / "case_outputs"),
                artifact_stem=artifact_stem,
                planning=planning,
                schema_passing=schema_passing,
            )
            ai_message = result["ai_message"]
            predicted_calls = ai_message.get("tool_calls", [])
            expected = _expected_calls(case)
            has_final_content = bool(ai_message.get("content", "").strip())
            tool_call_success, match = _tool_call_success(expected, predicted_calls, has_final_content, exact_args)
            record = {
                "model_id": model_id,
                "model_label": model_label,
                "model_series": model_series,
                "case_id": case_id,
                "mode": mode,
                "planning": planning,
                "schema_passing": schema_passing,
                "status": result["status"],
                "error": result["error"],
                "expected_tool_calls": expected,
                "predicted_tool_calls": predicted_calls,
                "has_final_content": has_final_content,
                "match": match,
                "parse_success": result["status"] == "success",
                "tool_name_success": result["status"] == "success" and _tool_name_success(expected, predicted_calls, has_final_content),
                "tool_call_success": result["status"] == "success" and tool_call_success,
                "success": result["status"] == "success" and tool_call_success,
                "input_token_count": result["schema_metadata"].get("input_token_count"),
                "output_token_count": result["schema_metadata"].get("output_token_count"),
                "total_token_count": result["schema_metadata"].get("total_token_count"),
                "native_tools_applied": result["schema_metadata"].get("native_tools_applied"),
                "fallback_to_prompt_injection": result["schema_metadata"].get("fallback_to_prompt_injection"),
                "raw_output_path": str(output_dir / "case_outputs" / f"{artifact_stem}_raw_model_output.json"),
                "ai_message_path": str(output_dir / "case_outputs" / f"{artifact_stem}_ai_message.json"),
            }
            case_records.append(record)
            all_case_records.append(record)
        total_cases = len(case_records)
        success_count = sum(1 for item in case_records if item["success"])
        parse_error_count = sum(1 for item in case_records if item["status"] != "success")
        parse_success_count = sum(1 for item in case_records if item["parse_success"])
        tool_name_success_count = sum(1 for item in case_records if item["tool_name_success"])
        tool_call_success_count = sum(1 for item in case_records if item["tool_call_success"])
        model_reports.append(
            {
                "model_id": model_id,
                "label": model_label,
                "series": model_series,
                "model_config": model_config,
                "mode": mode,
                "planning": planning,
                "schema_passing": schema_passing,
                "total_cases": total_cases,
                "parse_success_count": parse_success_count,
                "parse_success_rate": round(parse_success_count / total_cases, 4) if total_cases else 0.0,
                "tool_name_success_count": tool_name_success_count,
                "tool_name_success_rate": round(tool_name_success_count / total_cases, 4) if total_cases else 0.0,
                "tool_call_success_count": tool_call_success_count,
                "tool_call_success_rate": round(tool_call_success_count / total_cases, 4) if total_cases else 0.0,
                "success_count": success_count,
                "failure_count": total_cases - success_count,
                "success_rate": round(success_count / total_cases, 4) if total_cases else 0.0,
                "parse_error_count": parse_error_count,
                "parse_error_rate": round(parse_error_count / total_cases, 4) if total_cases else 0.0,
                "avg_input_tokens": _average_number([item["input_token_count"] for item in case_records]),
                "avg_output_tokens": _average_number([item["output_token_count"] for item in case_records]),
                "avg_total_tokens": _average_number([item["total_token_count"] for item in case_records]),
                "native_tools_applied_count": sum(1 for item in case_records if item["native_tools_applied"] is True),
                "fallback_count": sum(1 for item in case_records if item["fallback_to_prompt_injection"] is True),
                "case_records": case_records,
            }
        )
    report = {
        "generated_at": generated_at,
        "batch_config": str(batch_path),
        "tool_count": len(tools_schema),
        "available_tool_names": _tool_names_from_schema(tools_schema),
        "schema_text_chars": _schema_text_chars(tools_schema),
        "exact_args": exact_args,
        "total_models": len(model_reports),
        "total_cases": len(cases),
        "models": model_reports,
        "case_records": all_case_records,
    }
    write_json(report, output_dir / "batch_tool_call_eval_report.json")
    _write_batch_tool_call_summary(report, output_dir / "batch_tool_call_eval_summary.md")
    return report
