"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

# Biến toàn cục theo dõi thời gian gọi arXiv lần gần nhất (đảm bảo >= 3s)
_LAST_ARXIV_CALL = 0.0

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 DeepResearchAgent/1.0"
}


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError or retryable network errors, wait and call it again."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            # Xác định xem lỗi này có thuộc diện retry được không
            is_retryable = False
            retry_after = None

            if isinstance(e, RetryableError):
                is_retryable = True
                retry_after = e.retry_after
            elif isinstance(e, httpx.HTTPStatusError):
                if e.response.status_code in {429, 500, 502, 503, 504}:
                    is_retryable = True
                    # Đọc header Retry-After nếu có
                    ra_hdr = e.response.headers.get("Retry-After")
                    if ra_hdr:
                        try:
                            retry_after = float(ra_hdr)
                        except ValueError:
                            retry_after = None
            elif isinstance(e, httpx.TransportError):
                is_retryable = True

            if not is_retryable:
                raise e

            # Nếu là lần thử cuối cùng: ném lỗi ra ngay, không ngủ thêm
            if attempt == attempts - 1:
                raise e

            # Tính delay
            if retry_after is not None and retry_after > 0:
                delay = retry_after
            else:
                delay = base * (2 ** attempt) + random.uniform(0.1, 1.0)

            delay = min(delay, cap)
            time.sleep(delay)


def _clean_text(text: str) -> str:
    """Xóa khoảng trắng và ký tự xuống dòng thừa."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    global _LAST_ARXIV_CALL
    try:
        # Làm sạch query: chỉ giữ lại từ ngữ và gạch nối
        terms = re.findall(r"[A-Za-z0-9_-]+", query)
        if not terms:
            return "NO RESULTS"

        # Tôn trọng quy tắc của arXiv API: cách nhau ít nhất 3 giây
        now = time.time()
        elapsed = now - _LAST_ARXIV_CALL
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)
        _LAST_ARXIV_CALL = time.time()

        clamped_max = max(1, min(max_results, 30))
        search_query = " AND ".join(f"all:{t}" for t in terms)
        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": clamped_max,
        }

        def _fetch():
            with httpx.Client(headers=DEFAULT_HEADERS, timeout=30.0) as client:
                resp = client.get(ARXIV_URL, params=params)
                resp.raise_for_status()
                return resp.text

        # Cho arXiv cap dài hơn (60s) để chịu được rate limit mạng trường
        xml_text = with_retry(_fetch, attempts=6, base=2.0, cap=60.0)

        # Phân tích Atom XML
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            return "NO RESULTS"

        records = []
        for entry in entries:
            id_elem = entry.find("atom:id", ns)
            pub_elem = entry.find("atom:published", ns)
            title_elem = entry.find("atom:title", ns)
            summary_elem = entry.find("atom:summary", ns)

            if id_elem is None or not id_elem.text:
                continue

            raw_id = id_elem.text.strip()
            # Bóc tách ID sau /abs/ và bỏ đuôi version (ví dụ: 2501.00001v1 -> 2501.00001)
            raw_id_part = raw_id.split("/abs/")[-1].strip()
            paper_id = re.sub(r"v\d+$", "", raw_id_part)
            url = f"https://arxiv.org/abs/{paper_id}"

            published = (pub_elem.text.strip() if pub_elem is not None and pub_elem.text else "")[:10]
            title = _clean_text(title_elem.text if title_elem is not None and title_elem.text else "")
            summary = _clean_text(summary_elem.text if summary_elem is not None and summary_elem.text else "")[:600]

            records.append({
                "id": paper_id,
                "url": url,
                "published": published,
                "title": title,
                "summary": summary,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)

    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    try:
        clamped_limit = max(1, min(limit, 100))
        params = {"limit": clamped_limit}
        if date.strip():
            params["date"] = date.strip()

        def _fetch():
            with httpx.Client(headers=DEFAULT_HEADERS, timeout=25.0) as client:
                resp = client.get(HF_DAILY_URL, params=params)
                resp.raise_for_status()
                return resp.json()

        items = with_retry(_fetch, attempts=5, base=1.5, cap=30.0)
        if not items or not isinstance(items, list):
            return "NO RESULTS"

        records = []
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) and "paper" in item else item
            if not isinstance(paper, dict) or not paper.get("id"):
                continue

            paper_id = str(paper["id"]).strip()
            title = _clean_text(paper.get("title") or item.get("title") or "")
            summary = _clean_text(paper.get("summary") or item.get("summary") or "")[:600]
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = paper.get("upvotes") or item.get("upvotes") or 0
            github = paper.get("githubRepo") or item.get("githubRepo") or ""
            stars = paper.get("githubStars") or item.get("githubStars") or 0

            records.append({
                "id": paper_id,
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        if keyword.strip():
            kw = keyword.strip().lower()
            records = [r for r in records if kw in (r["title"] + " " + r["summary"]).lower()]

        # Sắp xếp theo upvotes giảm dần
        records.sort(key=lambda x: int(x.get("upvotes") or 0), reverse=True)

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)

    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    try:
        if not query.strip():
            return "NO RESULTS"

        clamped_limit = max(1, min(limit, 50))
        params = {"q": query.strip(), "limit": clamped_limit}

        def _fetch():
            with httpx.Client(headers=DEFAULT_HEADERS, timeout=25.0) as client:
                resp = client.get(HF_SEARCH_URL, params=params)
                resp.raise_for_status()
                return resp.json()

        items = with_retry(_fetch, attempts=5, base=1.5, cap=30.0)
        if not items or not isinstance(items, list):
            return "NO RESULTS"

        records = []
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) and "paper" in item else item
            if not isinstance(paper, dict) or not paper.get("id"):
                continue

            paper_id = str(paper["id"]).strip()
            title = _clean_text(paper.get("title") or item.get("title") or "")
            # Ưu tiên ai_summary nếu có
            raw_summary = paper.get("ai_summary") or paper.get("summary") or item.get("summary") or ""
            summary = _clean_text(raw_summary)[:600]
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = paper.get("upvotes") or item.get("upvotes") or 0
            github = paper.get("githubRepo") or item.get("githubRepo") or ""
            stars = paper.get("githubStars") or item.get("githubStars") or 0

            records.append({
                "id": paper_id,
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)

    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _redact_key(text: str, key: str) -> str:
    """Che giấu EXA_API_KEY nếu lọt vào chuỗi thông báo lỗi."""
    if key and key in text:
        return text.replace(key, "[REDACTED_EXA_KEY]")
    return text


def _call_exa_mcp(tool_name: str, arguments: dict) -> str:
    """Gọi công cụ MCP Exa qua giao thức HTTP POST JSON-RPC."""
    api_key = (os.getenv("EXA_API_KEY") or "").strip()
    url = f"{EXA_URL}?exaApiKey={api_key}" if api_key else EXA_URL
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **DEFAULT_HEADERS,
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    def _post():
        with httpx.Client(headers=headers, timeout=35.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                raise RetryableError("Exa rate limited (HTTP 429)", retry_after=float(ra) if ra else 20.0)
            if resp.status_code in {500, 502, 503, 504}:
                raise RetryableError(f"Exa server error (HTTP {resp.status_code})")
            resp.raise_for_status()
            return resp.text

    raw_response = with_retry(_post, attempts=6, base=2.0, cap=60.0)

    # Phân tích SSE response (tìm dòng "data:")
    parsed_json = None
    for line in raw_response.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                parsed_json = json.loads(line[5:].strip())
                break
            except json.JSONDecodeError:
                continue

    if not parsed_json:
        # Thử parse trực tiếp nếu là plain json
        try:
            parsed_json = json.loads(raw_response)
        except json.JSONDecodeError:
            raise RuntimeError(f"Cannot parse Exa response: {raw_response[:200]}")

    if "error" in parsed_json:
        err = parsed_json["error"]
        err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if "rate limit" in err_msg.lower():
            raise RetryableError(f"Exa rate limited: {err_msg}", retry_after=20.0)
        raise RuntimeError(f"Exa JSON-RPC error: {err_msg}")

    result = parsed_json.get("result", {})

    # Kiểm tra cờ giới hạn tốc độ ẩn của Exa trong _meta
    meta = result.get("_meta") or {}
    if meta.get("rate_limited") or meta.get("is_rate_limited") or "rate limit" in str(meta).lower():
        raise RetryableError("Exa rate limited in result._meta", retry_after=20.0)

    content_items = result.get("content", [])
    text_parts = []
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "text":
            txt = item.get("text", "")
            # Phát hiện nếu văn bản là thông báo giới hạn tốc độ
            if "rate limit" in txt.lower() and len(txt) < 300:
                raise RetryableError(f"Exa rate limit message in text: {txt}", retry_after=20.0)
            text_parts.append(txt)

    combined_text = "\n\n".join(text_parts).strip()
    return combined_text


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    api_key = (os.getenv("EXA_API_KEY") or "").strip()
    try:
        if not query.strip():
            return "NO RESULTS"

        obj = objective.strip() or f"find comprehensive technical papers and blogs about {query.strip()}"
        clamped_num = max(1, min(num_results, 10))

        text = _call_exa_mcp("web_search_exa", {
            "query": query.strip(),
            "objective": obj,
            "numResults": clamped_num,
        })

        if not text:
            return "NO RESULTS"
        return text

    except Exception as exc:
        msg = f"ERROR: {type(exc).__name__}: {exc}"
        return _redact_key(msg, api_key)


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    api_key = (os.getenv("EXA_API_KEY") or "").strip()
    try:
        if not url.strip():
            return "NO RESULTS"

        text = _call_exa_mcp("web_fetch_exa", {
            "urls": [url.strip()],
        })

        if not text:
            return "NO RESULTS"
        # Truncate to ~12000 chars theo GUIDE 1.4
        return text[:12000]

    except Exception as exc:
        msg = f"ERROR: {type(exc).__name__}: {exc}"
        return _redact_key(msg, api_key)


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
