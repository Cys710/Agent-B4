from __future__ import annotations

import ast
import math
import operator


_BINARY_OPERATORS = {
    # +  -  *  /  //  %  **
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 一元正负号
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _evaluate(node: ast.AST) -> int | float:
    # AST 的根节点是 Expression，真正的表达式在 body 中。
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    # 解析常量
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        # 幂运算单独限制指数，防止生成极大数导致耗时或溢出。
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent magnitude must not exceed 12")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if isinstance(result, complex) or not math.isfinite(float(result)) or abs(result) > 1e100:
            raise ValueError("calculation result is out of range")
        return result
    # AST 白名单 
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def calculator(expression: str) -> dict:
    # guard code
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty string")
    if len(expression) > 200:
        raise ValueError("expression is too long")
    try:
        # 获取 AST 语法树
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid arithmetic expression") from exc
    return {"result": _evaluate(tree)}
