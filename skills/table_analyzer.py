from __future__ import annotations

import csv
import statistics

from skills import resolve_data_path


def table_analyzer(
    path: str,
    max_rows_preview: int = 5,
    describe: bool = True,
    *,
    data_root: str | None = None,
) -> dict:
    if not isinstance(max_rows_preview, int) or isinstance(max_rows_preview, bool) or max_rows_preview < 0:
        raise ValueError("max_rows_preview must be a non-negative integer")
    # 统一解析到 data_root 下的真实路径，阻止越界访问。
    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".csv", ".tsv"}:
        raise ValueError("table_analyzer only supports .csv and .tsv files")
    if not source.is_file():
        raise FileNotFoundError(f"table file not found: {path}")
    delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
    with source.open("r", encoding="utf-8", newline="") as handle:
        # DictReader 会把首行当作表头，后续每行读成 {列名: 单元格值} 的字典。
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("table must contain a header row")
        rows = list(reader)
        columns = list(reader.fieldnames)
    stats: dict[str, dict] = {}
    if describe:
        for column in columns:
            raw_values = [row.get(column, "").strip() for row in rows]
            # 只对完整非空的列尝试数值统计，避免缺失值混入结果。
            if not raw_values or any(value == "" for value in raw_values):
                continue
            try:
                # 只有整列都能转成 float，才把它视为数值列。
                values = [float(value) for value in raw_values]
            except ValueError:
                continue
            stats[column] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
            }
    return {
        "path": source.relative_to(root).as_posix(),
        "num_rows": len(rows),
        "num_columns": len(columns),
        "columns": columns,
        "preview": rows[:max_rows_preview],
        "describe": stats,
    }
