from __future__ import annotations

import math
import re
from collections import Counter

from skills import resolve_data_path


MAX_INDEXED_FILE_BYTES = 2_000_000
# 分词器: 英文/数字/下划线连续串 + 连续中文字符
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


# "Hello, Gary Gan!" -> ["hello", "gary", "gan"]
def _query_terms(query: str) -> list[str]:
    terms = [term.casefold() for term in TOKEN_PATTERN.findall(query)]
    # 去重但保留顺序；结果为空，返回整个 query.casefold() 
    return list(dict.fromkeys(terms or [query.casefold()]))


def _token_counts(text: str) -> Counter[str]:
    return Counter(token.casefold() for token in TOKEN_PATTERN.findall(text))


def _term_counts(text: str, terms: list[str]) -> dict[str, int]:
    lowered = text.casefold()
    token_counts = _token_counts(text)
    # 子串匹配，适合中文短语; 按 token 计数，适合英文单词
    return {term: max(lowered.count(term), token_counts.get(term, 0)) for term in terms}


def _line_number(text: str, terms: list[str]) -> int | None:
    lowered = text.casefold()
    for index, line in enumerate(lowered.splitlines(), start=1):
        if any(term in line for term in terms):
            return index
    return None


def _snippet(text: str, terms: list[str], radius: int = 80) -> str:
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - radius)
    end = min(len(text), start + radius * 2)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ").strip() + suffix


def _rank_documents(documents: list[dict], terms: list[str]) -> list[dict]:
    doc_count = len(documents)
    # 计算每个查询词出现在多少篇文档里
    document_frequency = {
        term: sum(1 for document in documents if document["term_counts"].get(term, 0) > 0)
        for term in terms
    }
    ranked = []
    for document in documents:
        score = 0.0
        token_length = max(document["token_count"], 1)
        for term in terms:
            term_frequency = document["term_counts"].get(term, 0)
            if not term_frequency:
                continue
            # score += (词频 * 稀有度) / 长度惩罚
            idf = math.log((doc_count + 1) / (document_frequency[term] + 0.5)) + 1
            score += (term_frequency / (1 + token_length / 120)) * idf
            if term in document["path"].casefold():
                score += 0.5
        matched_terms = [term for term in terms if document["term_counts"].get(term, 0) > 0]
        ranked.append(
            {
                "path": document["path"],
                "score": round(score, 3),
                "snippet": _snippet(document["text"], matched_terms),
                "line_number": _line_number(document["text"], matched_terms),
                "matched_terms": matched_terms,
            }
        )
    # 先按 score 降序，分数相同时按 path 升序
    ranked.sort(key=lambda item: (-item["score"], item["path"]))
    return ranked


def local_file_search(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int = 5,
    *,
    data_root: str | None = None,
) -> dict:
    """Search local txt/md files and return ranked paths, scores, and snippets."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if top_k > 50:
        raise ValueError("top_k must not exceed 50")
    search_root, data_root_path = resolve_data_path(root_dir, data_root)
    if not search_root.is_dir():
        raise FileNotFoundError(f"search directory not found: {root_dir}")
    extensions = file_types or ["txt", "md"]
    normalized_extensions = {f".{item.lower().lstrip('.')}" for item in extensions}
    if not normalized_extensions.issubset({".txt", ".md"}):
        raise ValueError("local_file_search only supports txt and md")
    terms = _query_terms(query)
    documents = []
    for path in sorted(search_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in normalized_extensions:
            continue
        if path.stat().st_size > MAX_INDEXED_FILE_BYTES:
            continue
        text = path.read_text(encoding="utf-8")
        term_counts = _term_counts(text, terms)
        if any(term_counts.values()):
            documents.append(
                {
                    "path": path.relative_to(data_root_path).as_posix(),
                    "text": text,
                    "term_counts": term_counts,
                    "token_count": sum(_token_counts(text).values()),
                }
            )
    results = _rank_documents(documents, terms)[:top_k]
    return {"query_terms": terms, "results": results}
