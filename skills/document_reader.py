from __future__ import annotations

import re
import zlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from skills import resolve_data_path


SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx"}
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_RETURN_CHARS = 50_000


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _normalize_text(text: str) -> str:
    # 清理不可打印字符并压缩空白，输出更适合后续展示和交给模型。
    text = "".join(char if char in {"\n", "\t"} or char.isprintable() else " " for char in text)
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = " ".join(line.split())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _text_from_paragraph(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = _local_name(node.tag)
        # Office XML 常把正文拆在多个文本节点里，这里把文字、制表和换行重新拼起来。
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _read_xml_from_zip(source: Path, member_name: str) -> ElementTree.Element:
    try:
        # DOCX/PPTX 本质上是 zip 容器，正文和幻灯片内容都存放在内部 XML 文件里。
        with zipfile.ZipFile(source) as archive:
            with archive.open(member_name) as handle:
                return ElementTree.fromstring(handle.read())
    except KeyError as exc:
        raise ValueError(f"document is missing required XML member: {member_name}") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid Office document archive: {source.name}") from exc
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid Office document XML: {member_name}") from exc


def _read_docx(source: Path) -> tuple[str, dict, list[str]]:
    root = _read_xml_from_zip(source, "word/document.xml")
    # 逐段提取正文段落，保留自然的段落换行。
    paragraphs = [_text_from_paragraph(node) for node in root.iter() if _local_name(node.tag) == "p"]
    paragraphs = [text for text in paragraphs if text]
    return "\n".join(paragraphs), {"paragraph_count": len(paragraphs)}, []


def _slide_number(member_name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", member_name)
    return int(match.group(1)) if match else 0


def _read_pptx(source: Path) -> tuple[str, dict, list[str]]:
    try:
        with zipfile.ZipFile(source) as archive:
            slide_names = sorted(
                [
                    name
                    for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                ],
                key=_slide_number,
            )
            sections = []
            for index, slide_name in enumerate(slide_names, start=1):
                try:
                    root = ElementTree.fromstring(archive.read(slide_name))
                except ElementTree.ParseError as exc:
                    raise ValueError(f"invalid slide XML: {slide_name}") from exc
                # 每页幻灯片按段落提取文本，并在输出里保留 Slide 编号方便定位。
                paragraphs = [_text_from_paragraph(node) for node in root.iter() if _local_name(node.tag) == "p"]
                paragraphs = [text for text in paragraphs if text]
                if paragraphs:
                    sections.append(f"Slide {index}\n" + "\n".join(paragraphs))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid Office document archive: {source.name}") from exc
    return "\n\n".join(sections), {"slide_count": len(slide_names)}, []


def _decode_pdf_bytes(value: bytes) -> str:
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", errors="replace")
    if value.startswith(b"\xff\xfe"):
        return value[2:].decode("utf-16-le", errors="replace")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1", errors="replace")


def _decode_pdf_literal(value: bytes) -> str:
    result = bytearray()
    index = 0
    while index < len(value):
        current = value[index]
        if current != 0x5C:
            result.append(current)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        escaped = value[index]
        escapes = {
            ord("n"): ord("\n"),
            ord("r"): ord("\r"),
            ord("t"): ord("\t"),
            ord("b"): ord("\b"),
            ord("f"): ord("\f"),
            ord("("): ord("("),
            ord(")"): ord(")"),
            ord("\\"): ord("\\"),
        }
        if escaped in escapes:
            result.append(escapes[escaped])
            index += 1
        elif 48 <= escaped <= 55:
            # PDF 字面量里可能出现八进制转义，例如 \\050 表示 '('。
            digits = bytes([escaped])
            index += 1
            while index < len(value) and len(digits) < 3 and 48 <= value[index] <= 55:
                digits += bytes([value[index]])
                index += 1
            result.append(int(digits, 8) & 0xFF)
        elif escaped in {ord("\n"), ord("\r")}:
            index += 1
            if escaped == ord("\r") and index < len(value) and value[index] == ord("\n"):
                index += 1
        else:
            result.append(escaped)
            index += 1
    return _decode_pdf_bytes(bytes(result))


def _decode_pdf_hex(value: bytes) -> str:
    cleaned = re.sub(rb"\s+", b"", value)
    if len(cleaned) % 2:
        cleaned += b"0"
    try:
        return _decode_pdf_bytes(bytes.fromhex(cleaned.decode("ascii")))
    except ValueError:
        return ""


def _pdf_strings(payload: bytes) -> list[str]:
    strings = []
    literal_pattern = rb"\((?:\\.|[^\\()])*\)"
    hex_pattern = rb"<([0-9A-Fa-f\s]+)>"
    # 匹配常见 PDF 文本绘制指令，把字面量字符串和十六进制字符串都尽量提取出来。
    for match in re.finditer(literal_pattern + rb"\s*(?:Tj|'|\")", payload, flags=re.DOTALL):
        literal = match.group(0)
        strings.append(_decode_pdf_literal(literal[1 : literal.rfind(b")")]))
    for array_match in re.finditer(rb"\[(.*?)\]\s*TJ", payload, flags=re.DOTALL):
        array_payload = array_match.group(1)
        parts = []
        for literal in re.finditer(literal_pattern, array_payload, flags=re.DOTALL):
            parts.append(_decode_pdf_literal(literal.group(0)[1:-1]))
        for hex_string in re.finditer(hex_pattern, array_payload, flags=re.DOTALL):
            parts.append(_decode_pdf_hex(hex_string.group(1)))
        if parts:
            strings.append("".join(parts))
    for match in re.finditer(rb"<([0-9A-Fa-f\s]+)>\s*(?:Tj|'|\")", payload, flags=re.DOTALL):
        strings.append(_decode_pdf_hex(match.group(1)))
    return [text for text in strings if text and not text.isspace()]


def _pdf_streams(raw: bytes) -> tuple[list[bytes], list[str]]:
    streams = []
    warnings: list[str] = []
    unsupported_filters: set[str] = set()
    for match in re.finditer(rb"stream(?:\r\n|\n|\r)(.*?)\r?\nendstream", raw, flags=re.DOTALL):
        prefix = raw[max(0, match.start() - 2048) : match.start()]
        dictionary_start = prefix.rfind(b"<<")
        dictionary = prefix[dictionary_start:] if dictionary_start >= 0 else prefix
        if b"/Subtype" in dictionary and b"/Image" in dictionary:
            continue
        payload = match.group(1)
        if b"/FlateDecode" in dictionary:
            try:
                # 很多 PDF 文本流会先经过 Flate 压缩，需要先解压才能继续抽字符串。
                payload = zlib.decompress(payload)
            except zlib.error:
                warnings.append("skipped a FlateDecode stream that could not be decompressed")
                continue
        elif b"/Filter" in dictionary:
            filter_match = re.search(rb"/Filter\s*/([A-Za-z0-9]+)", dictionary)
            if filter_match:
                unsupported_filters.add(filter_match.group(1).decode("ascii", errors="replace"))
            continue
        streams.append(payload)
    if unsupported_filters:
        warnings.append("skipped unsupported PDF filters: " + ", ".join(sorted(unsupported_filters)))
    return streams, warnings


def _read_pdf(source: Path) -> tuple[str, dict, list[str]]:
    raw = source.read_bytes()
    streams, warnings = _pdf_streams(raw)
    chunks: list[str] = []
    for stream in streams:
        chunks.extend(_pdf_strings(stream))
    text = "\n".join(chunks)
    if not text.strip():
        # 扫描版 PDF 往往只有图片没有可复制文字，这里返回告警而不是直接报错。
        warnings.append("no extractable PDF text was found; scanned PDFs or custom font encodings may need OCR")
    page_count = len(re.findall(rb"/Type\s*/Page\b", raw))
    return text, {"page_count": page_count, "stream_count": len(streams)}, warnings


def document_reader(path: str, max_chars: int = 4000, *, data_root: str | None = None) -> dict:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if max_chars > MAX_RETURN_CHARS:
        raise ValueError(f"max_chars must not exceed {MAX_RETURN_CHARS}")

    # 先把路径限制在 data_root 下，和其他本地读取类 Skill 保持一致的安全边界。
    source, root = resolve_data_path(path, data_root)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("document_reader only supports .pdf, .docx, and .pptx files")
    if not source.is_file():
        raise FileNotFoundError(f"document file not found: {path}")
    file_size = source.stat().st_size
    if file_size > MAX_FILE_BYTES:
        raise ValueError(f"document file is too large: {file_size} bytes")

    # 按文件类型选择不同的文本抽取策略。
    if suffix == ".pdf":
        extracted, metadata, warnings = _read_pdf(source)
    elif suffix == ".docx":
        extracted, metadata, warnings = _read_docx(source)
    else:
        extracted, metadata, warnings = _read_pptx(source)

    normalized = _normalize_text(extracted)
    content = normalized[:max_chars]
    return {
        "content": content,
        "num_chars": len(content),
        "source": source.relative_to(root).as_posix(),
        "file_type": suffix.lstrip("."),
        "truncated": len(normalized) > len(content),
        "metadata": {"file_size_bytes": file_size, **metadata},
        "warnings": warnings,
    }
