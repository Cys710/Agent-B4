from __future__ import annotations

import argparse
import json
from pathlib import Path

from b4.planning import _select_resources_for_plan, execute_plan, rule_based_final_response
from common.io_utils import read_json, write_json


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]

# 入口函数，封装完整规划执行全流程，接收输入文件、输出目录、生成长度限制，返回完整运行结果字典。
def run_plan_and_execute_demo(input_path: Path, output_dir: Path, max_new_tokens: int = 96) -> dict:
    root = _project_root()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = read_json(input_path)
    original_plan = payload["plan"]
    # 读取模型配置 model.yaml，给每个任务匹配可用模型、算力资源
    selected_plan = _select_resources_for_plan(original_plan, root / "configs" / "model.yaml")
    # 步骤2：执行全部规划任务（核心执行逻辑）
    task_results = execute_plan(
        selected_plan,
        output_dir=output_dir / "tasks",
        tools_config=root / "configs" / "tools.yaml",
        toolset="extended_tools",
        expert_config_path=root / "configs" / "expert_models.yaml",
        max_new_tokens=max_new_tokens,
    )
    # 步骤3：基于所有任务执行结果，生成最终回复
    final_response = rule_based_final_response(str(payload.get("user_request", "")), task_results)
    result = {
        "user_request": payload.get("user_request", ""),
        "original_plan": original_plan,
        "selected_plan": selected_plan,
        "task_results": task_results,
        "final_response": final_response,
        "status": "success" if all(item["status"] == "success" for item in task_results) else "partial_or_error",
    }
    write_json(result, output_dir / "plan_demo_result.json")
    return result

# 命令行参数构建 
def build_parser() -> argparse.ArgumentParser:
    root = _project_root()
    parser = argparse.ArgumentParser(description="Run a compact plan-and-execute demo.")
    parser.add_argument("--input", default=str(root / "data" / "messages" / "plan_demo_input.json"))
    parser.add_argument("--outdir", default=str(root / "outputs" / "plan_demo"))
    parser.add_argument("--max_new_tokens", type=int, default=96)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 调用核心执行函数
    result = run_plan_and_execute_demo(Path(args.input), Path(args.outdir), args.max_new_tokens)
    print(
        json.dumps(
            {"status": result["status"], "output_path": str(Path(args.outdir) / "plan_demo_result.json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
