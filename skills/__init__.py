from __future__ import annotations

from pathlib import Path


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"


def resolve_data_path(path: str, data_root: str | None = None) -> tuple[Path, Path]:
    root = Path(data_root).resolve() if data_root else DEFAULT_DATA_ROOT.resolve()
    candidate = Path(path).expanduser()  # 把 ~ 展开成用户主目录
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        # 判断 candidate 是不是 root 下面的路径
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes data root: {path}") from exc
    return candidate, root
