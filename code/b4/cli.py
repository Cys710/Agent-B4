from __future__ import annotations

"""
    B4 命令行入口。
    真实逻辑都在 service.py / evaluation.py；这里只负责解析参数并分派。
"""

import argparse
import sys

from common.io_utils import read_json
from common.path_utils import resolve_cli_path

from .evaluation import run_batch_tool_call_eval, run_schema_passing_batch_eval
from .generation import SCHEMA_PASSING_MODES
from .service import compare_schema_passing_methods, generate_ai_message


def build_parser() -> argparse.ArgumentParser:
    """定义 B4 CLI 参数。"""
    parser = argparse.ArgumentParser(description="Generate one multi-tool-capable AIMessage with a local or mock LLM.")
    parser.add_argument("--model_config")
    parser.add_argument("--messages")
    parser.add_argument("--tools_schema", required=True)
    parser.add_argument("--mode", choices=["mock", "prompt_json"])
    parser.add_argument("--planning", choices=["none", "plan_and_execute"])
    parser.add_argument("--schema_passing", choices=sorted(SCHEMA_PASSING_MODES))
    parser.add_argument("--tools_config")
    parser.add_argument("--toolset")
    parser.add_argument("--compare_schema_passing", action="store_true")
    parser.add_argument("--schema_passing_batch_eval")
    parser.add_argument("--batch_eval")
    parser.add_argument("--outdir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主函数：单次生成、schema passing 对比、batch eval 三种入口。"""
    args = build_parser().parse_args(argv)
    try:
        outdir = resolve_cli_path(args.outdir)
        tools_schema = read_json(resolve_cli_path(args.tools_schema))
        # 批处理模式
        if args.schema_passing_batch_eval:
            run_schema_passing_batch_eval(
                str(resolve_cli_path(args.schema_passing_batch_eval)),
                tools_schema,
                str(outdir),
                model_config_override=args.model_config,
                mode_override=args.mode,
            )
            print(outdir / "schema_passing_batch_report.json")
            return 0
        if args.batch_eval:
            run_batch_tool_call_eval(
                str(resolve_cli_path(args.batch_eval)),
                tools_schema,
                str(outdir),
                mode_override=args.mode,
                planning_override=args.planning,
                schema_passing_override=args.schema_passing,
            )
            print(outdir / "batch_tool_call_eval_report.json")
            return 0
        if not args.model_config or not args.messages or not args.mode:
            raise ValueError("single-run mode requires --model_config, --messages, and --mode")
        model_config = str(resolve_cli_path(args.model_config))
        messages = read_json(resolve_cli_path(args.messages))
        # schema passing 对比模式
        if args.compare_schema_passing:
            compare_schema_passing_methods(
                model_config,
                messages,
                tools_schema,
                args.mode,
                str(outdir),
                planning=args.planning or "none",
            )
            print(outdir / "schema_passing_report.json")
        # 单次生成模式
        else:
            generate_ai_message(
                model_config,
                messages,
                tools_schema,
                args.mode,
                str(outdir),
                planning=args.planning or "none",
                schema_passing=args.schema_passing or "prompt_injection",
                tools_config=str(resolve_cli_path(args.tools_config)) if args.tools_config else None,
                toolset=args.toolset,
            )
            print(outdir / "ai_message.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
