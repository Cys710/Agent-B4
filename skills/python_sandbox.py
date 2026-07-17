from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path


MAX_CODE_CHARS = 4000
MAX_TIMEOUT_SECONDS = 5.0
MAX_OUTPUT_CHARS = 8000
FORBIDDEN_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "vars",
}

RUNNER = r"""
import contextlib
import io
import json
import math
import statistics
import sys
import traceback

# 子进程从标准输入读取用户代码，避免把代码直接拼进命令行。
source = sys.stdin.read()
stdout_buffer = io.StringIO()
stderr_text = ""
# 只暴露一小部分安全内建函数，避免代码直接接触文件、导入和解释器环境。
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
try:
    # 捕获用户代码的 print 输出，最后统一包成 JSON 返回给父进程。
    with contextlib.redirect_stdout(stdout_buffer):
        exec(compile(source, "<python_sandbox>", "exec"), namespace, namespace)
    status = "success"
except Exception:
    status = "error"
    stderr_text = traceback.format_exc(limit=1)
print(json.dumps({"status": status, "stdout": stdout_buffer.getvalue(), "stderr": stderr_text}))
"""


def _validate_code(code: str) -> None:
    # 在真正启动子进程前先做 AST 静态检查，尽早拦截高风险语法。
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError("invalid Python syntax") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("imports are not allowed in python_sandbox")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("global and nonlocal statements are not allowed")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            # 禁止直接调用 open/eval/exec/__import__ 等高风险名称。
            raise ValueError(f"name is not allowed in python_sandbox: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            # 限制 dunder 属性，减少通过对象元编程绕过限制的机会。
            raise ValueError("dunder attributes are not allowed in python_sandbox")


def _workdir(output_dir: str | None) -> Path:
    if output_dir:
        # 把沙箱运行目录放到输出目录下，便于调试时查看临时产物。
        root = Path(output_dir).resolve() / "sandbox_runs"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="run_", dir=root))
    return Path(tempfile.mkdtemp(prefix="agent_sandbox_"))


def python_sandbox(
    code: str,
    timeout_seconds: float = 2.0,
    max_output_chars: int = 2000,
    *,
    output_dir: str | None = None,
) -> dict:
    """Execute short Python snippets with AST checks, timeout, and output limits."""
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
    cwd = _workdir(output_dir)
    try:
        # 在全新的 Python 子进程里执行代码，并施加硬超时，避免阻塞主进程。
        completed = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER],
            input=code,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"python_sandbox timed out after {timeout_seconds} seconds") from exc
    stdout = completed.stdout
    stderr = completed.stderr
    runner_payload = None
    if stdout.strip():
        import json

        # RUNNER 最终只打印一个 JSON 包，真正的用户 stdout/stderr 被包在里面。
        runner_payload = json.loads(stdout)
        stdout = runner_payload.get("stdout", "")
        stderr = stderr + runner_payload.get("stderr", "")
    truncated = len(stdout) > max_output_chars or len(stderr) > max_output_chars
    return {
        "status": runner_payload.get("status", "error") if runner_payload else "error",
        "stdout": stdout[:max_output_chars],
        "stderr": stderr[:max_output_chars],
        "returncode": completed.returncode,
        "timed_out": False,
        "truncated": truncated,
        "workdir": str(cwd),
    }
