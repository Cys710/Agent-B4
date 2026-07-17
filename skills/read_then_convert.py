from __future__ import annotations

from skills.file_reader import file_reader
from skills.format_converter import format_converter


def read_then_convert(
    path: str,
    target_format: str,
    max_chars: int = 2000,
    output_filename: str | None = None,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    """Read a local text file, convert its content, and write the converted file."""
    # 先复用 file_reader 读取文本，这样路径沙箱、扩展名限制和截断逻辑都保持一致。
    read_result = file_reader(path, max_chars=max_chars, data_root=data_root)
    # 再把读取到的内容交给 format_converter，沿用已有的格式转换和输出落盘能力。
    converted = format_converter(
        read_result["content"],
        target_format,
        output_filename=output_filename,
        output_dir=output_dir,
    )
    return {
        # 对外同时返回“读取阶段”的元信息和“转换阶段”的产物，便于上层直接消费。
        "source": read_result["source"],
        "num_chars": read_result["num_chars"],
        "truncated": read_result["truncated"],
        "target_format": target_format,
        "formatted_text": converted["formatted_text"],
        "generated_file_path": converted["generated_file_path"],
    }
