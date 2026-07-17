from __future__ import annotations

import ast
import json
import subprocess
import sys

from skills.python_sandbox import (
    FORBIDDEN_NAMES,
    MAX_CODE_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    _validate_code,
    _workdir,
)


MAX_TESTS = 20
MAX_EXPRESSION_CHARS = 500

RUNNER = r"""
import contextlib
import io
import json
import math
import statistics
import sys
import traceback

payload = json.loads(sys.stdin.read())
source = payload["code"]
tests = payload["tests"]
stdout_buffer = io.StringIO()
stderr_text = ""
cases = []
safe_builtins = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "pow": pow,
    "print": print,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
namespace = {
    "__builtins__": safe_builtins,
    "math": math,
    "statistics": statistics,
}


def json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return repr(value)


try:
    with contextlib.redirect_stdout(stdout_buffer):
        exec(compile(source, "<python_unit_test_sandbox>", "exec"), namespace, namespace)
        for index, test in enumerate(tests, start=1):
            expression = test["expression"]
            expected = test["expected"]
            try:
                actual = eval(compile(expression, f"<test_case_{index}>", "eval"), namespace, namespace)
                passed = actual == expected
                cases.append(
                    {
                        "index": index,
                        "expression": expression,
                        "expected": expected,
                        "actual": json_safe(actual),
                        "actual_repr": repr(actual),
                        "passed": passed,
                        "error": None,
                    }
                )
            except Exception:
                cases.append(
                    {
                        "index": index,
                        "expression": expression,
                        "expected": expected,
                        "actual": None,
                        "actual_repr": "",
                        "passed": False,
                        "error": traceback.format_exc(limit=1),
                    }
                )
    status = "completed"
except Exception:
    status = "error"
    stderr_text = traceback.format_exc(limit=1)

passed_count = sum(1 for case in cases if case["passed"])
failed_count = len(cases) - passed_count
print(
    json.dumps(
        {
            "status": status,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "all_passed": bool(cases) and failed_count == 0,
            "cases": cases,
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_text,
        },
        ensure_ascii=False,
    )
)
"""


def _validate_test_expression(expression: str) -> None:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid test expression: {expression}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"name is not allowed in test expression: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("dunder attributes are not allowed in test expressions")


def _validate_tests(tests: list[dict]) -> None:
    if not isinstance(tests, list) or not tests:
        raise ValueError("tests must be a non-empty array")
    if len(tests) > MAX_TESTS:
        raise ValueError(f"tests must not contain more than {MAX_TESTS} cases")
    for index, test in enumerate(tests, start=1):
        if not isinstance(test, dict):
            raise ValueError(f"test case {index} must be an object")
        expression = test.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"test case {index} expression must be a non-empty string")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ValueError(f"test case {index} expression must not exceed {MAX_EXPRESSION_CHARS} characters")
        if "expected" not in test:
            raise ValueError(f"test case {index} must include expected")
        _validate_test_expression(expression.strip())


def python_unit_test_sandbox(
    code: str,
    tests: list[dict],
    timeout_seconds: float = 2.0,
    max_output_chars: int = 2000,
    *,
    output_dir: str | None = None,
) -> dict:
    """Run restricted Python code and evaluate expression-based unit test cases."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code must not exceed {MAX_CODE_CHARS} characters")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be a number")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be > 0 and <= {MAX_TIMEOUT_SECONDS}")
    if not isinstance(max_output_chars, int) or isinstance(max_output_chars, bool):
        raise ValueError("max_output_chars must be an integer")
    if max_output_chars <= 0 or max_output_chars > MAX_OUTPUT_CHARS:
        raise ValueError(f"max_output_chars must be > 0 and <= {MAX_OUTPUT_CHARS}")
    _validate_code(code)
    _validate_tests(tests)

    cwd = _workdir(output_dir)
    payload = {"code": code, "tests": tests}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=str(cwd),
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"python_unit_test_sandbox timed out after {timeout_seconds} seconds") from exc

    runner_payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    stdout = runner_payload.get("stdout", "")
    stderr = completed.stderr + runner_payload.get("stderr", "")
    truncated = len(stdout) > max_output_chars or len(stderr) > max_output_chars
    return {
        "status": runner_payload.get("status", "error"),
        "passed_count": runner_payload.get("passed_count", 0),
        "failed_count": runner_payload.get("failed_count", 0),
        "all_passed": runner_payload.get("all_passed", False),
        "cases": runner_payload.get("cases", []),
        "stdout": stdout[:max_output_chars],
        "stderr": stderr[:max_output_chars],
        "returncode": completed.returncode,
        "timed_out": False,
        "truncated": truncated,
        "workdir": str(cwd),
    }
