from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import urllib.parse


MAX_TOP_K = 10
MAX_QUERY_CHARS = 200
MAX_TIMEOUT_SECONDS = 10.0
MAX_HTML_BYTES = 512_000
SEARCH_URL = "https://duckduckgo.com/html?q={query}"
RESULT_BLOCK_PATTERN = re.compile(r'<div class="result .*?</div>\s*</div>\s*</div>', re.DOTALL)
TITLE_PATTERN = re.compile(r'<a[^>]*class="result__a"[^>]*href="(?P<href>.*?)"[^>]*>(?P<title>.*?)</a>', re.DOTALL)
SNIPPET_PATTERN = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.DOTALL)
TAG_PATTERN = re.compile(r"<.*?>", re.DOTALL)

FETCH_RUNNER = r"""
import json
import sys
import urllib.request

payload = json.loads(sys.stdin.read())
request = urllib.request.Request(
    payload["url"],
    headers={
        "User-Agent": "Mozilla/5.0 AgentSkillDemo/1.0",
        "Accept": "text/html,application/xhtml+xml",
    },
)

try:
    with urllib.request.urlopen(request, timeout=float(payload["timeout_seconds"])) as response:
        raw_html = response.read(int(payload["max_html_bytes"]) + 1)
    if len(raw_html) > int(payload["max_html_bytes"]):
        result = {
            "status": "error",
            "error_type": "ValueError",
            "message": "web_search response exceeded maximum HTML size",
        }
    else:
        result = {
            "status": "success",
            "html": raw_html.decode("utf-8", errors="replace"),
        }
except TimeoutError as exc:
    result = {
        "status": "error",
        "error_type": "TimeoutError",
        "message": str(exc),
    }
except OSError as exc:
    result = {
        "status": "error",
        "error_type": "OSError",
        "message": str(exc),
    }
print(json.dumps(result, ensure_ascii=False))
"""


def _clean_html(value: str) -> str:
    text = TAG_PATTERN.sub("", value)
    text = html.unescape(text)
    return " ".join(text.split())


def _normalize_duckduckgo_url(url: str) -> str:
    decoded = html.unescape(url)
    if decoded.startswith("//"):
        decoded = "https:" + decoded
    parsed = urllib.parse.urlparse(decoded)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    return decoded


def _parse_duckduckgo_results(raw_html: str, top_k: int) -> list[dict]:
    results = []
    for block_match in RESULT_BLOCK_PATTERN.finditer(raw_html):
        block = block_match.group(0)
        title_match = TITLE_PATTERN.search(block)
        if not title_match:
            continue
        snippet_match = SNIPPET_PATTERN.search(block)
        title = _clean_html(title_match.group("title"))
        url = _normalize_duckduckgo_url(title_match.group("href"))
        snippet = _clean_html(snippet_match.group("snippet")) if snippet_match else ""
        if title and url:
            results.append(
                {
                    "rank": len(results) + 1,
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                }
            )
        if len(results) >= top_k:
            break
    return results


def _mock_search(query: str, top_k: int, mock_results: list[dict]) -> dict:
    results = []
    for index, item in enumerate(mock_results[:top_k], start=1):
        if not isinstance(item, dict):
            raise ValueError("mock_results items must be objects")
        title = item.get("title")
        url = item.get("url")
        snippet = item.get("snippet", "")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("mock result title must be a non-empty string")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("mock result url must be a non-empty string")
        if not isinstance(snippet, str):
            raise ValueError("mock result snippet must be a string")
        results.append(
            {
                "rank": index,
                "title": title.strip(),
                "url": url.strip(),
                "snippet": snippet.strip(),
            }
        )
    return {"query": query, "source": "mock", "request_mode": "mock", "results": results}


def _fetch_search_html(url: str, timeout_seconds: float) -> str:
    payload = {
        "url": url,
        "timeout_seconds": timeout_seconds,
        "max_html_bytes": MAX_HTML_BYTES,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", FETCH_RUNNER],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(timeout_seconds) + 1.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"web_search subprocess timed out after {timeout_seconds} seconds") from exc
    if not completed.stdout.strip():
        message = completed.stderr.strip() or "web_search subprocess returned no output"
        raise RuntimeError(message)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("web_search subprocess returned invalid JSON") from exc
    if result.get("status") != "success":
        message = result.get("message", "web_search subprocess failed")
        if result.get("error_type") == "TimeoutError":
            raise TimeoutError(f"web_search timed out after {timeout_seconds} seconds: {message}")
        raise RuntimeError(f"web_search request failed in subprocess: {message}")
    html_text = result.get("html")
    if not isinstance(html_text, str):
        raise RuntimeError("web_search subprocess response did not include HTML")
    return html_text


def web_search(
    query: str,
    top_k: int = 5,
    timeout_seconds: float = 5.0,
    mock_results: list[dict] | None = None,
) -> dict:
    """Search the public web and return titles, URLs, and snippets."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must not exceed {MAX_QUERY_CHARS} characters")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if top_k > MAX_TOP_K:
        raise ValueError(f"top_k must not exceed {MAX_TOP_K}")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be a number")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be > 0 and <= {MAX_TIMEOUT_SECONDS}")
    if mock_results is not None:
        if not isinstance(mock_results, list):
            raise ValueError("mock_results must be an array")
        return _mock_search(query, top_k, mock_results)

    encoded_query = urllib.parse.quote_plus(query)
    raw_html = _fetch_search_html(SEARCH_URL.format(query=encoded_query), float(timeout_seconds))

    results = _parse_duckduckgo_results(raw_html, top_k)
    if not results:
        raise RuntimeError("web_search returned no parseable results")
    return {"query": query, "source": "duckduckgo_html", "request_mode": "subprocess", "results": results}
