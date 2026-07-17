from __future__ import annotations

import sys
from pathlib import Path


# common/path_utils.py lives under code/common, so parents[2] is the agent root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"


def bootstrap_project_root() -> Path:
    # Allow scripts launched from agent/code to import top-level packages such as skills.
    root_text = str(PROJECT_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return PROJECT_ROOT


def resolve_path(path: str | Path, base_dir: str | Path) -> Path:
    '''把相对路径按指定的 base_dir 解析'''
    # Resolve relative paths against an explicit base, not whichever cwd a caller happens to use.
    candidate = Path(path).expanduser() # 把 ~ 替换成当前用户的 home 目录
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate 
    return candidate.resolve() # resolve 处理../. 等


def resolve_cli_path(path: str | Path) -> Path:
    # CLI arguments are interpreted relative to the shell working directory.
    return resolve_path(path, Path.cwd()) # 列出当前工作目录


def resolve_from_file(path: str | Path, containing_file: str | Path) -> Path:
    # Config/input files often contain relative paths; resolve them beside that file.
    return resolve_path(path, Path(containing_file).resolve().parent)


def require_within(path: str | Path, root: str | Path) -> Path:
    # File skills must not read outside their configured data root.
    resolved_path = Path(path).resolve()
    resolved_root = Path(root).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes data root: {path}") from exc
    return resolved_path
