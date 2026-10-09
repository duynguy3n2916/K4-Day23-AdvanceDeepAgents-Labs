"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"


import re

_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")  # [3], [1, 2], [1-3]; ignore [3](link)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_URL_REGEX = re.compile(r"https?://\S+")


def _extract_group_numbers(group_str):
    numbers = []
    for part in re.split(r"\s*,\s*", group_str):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            if part.isdigit():
                numbers.append(int(part))
    return numbers


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK)."""
    problems = []

    # 1. Kiểm tra danh sách sources
    if not sources or not isinstance(sources, list):
        return ["no sources in sources.json"]

    seen_urls = {}
    valid_source_nums = set()

    for idx, entry in enumerate(sources):
        if not isinstance(entry, dict):
            problems.append(f"source index {idx} is not an object")
            continue

        n = entry.get("n")
        if not isinstance(n, int):
            problems.append(f"source {entry} has non-integer n: {n!r}")
        else:
            valid_source_nums.add(n)

        url = entry.get("url")
        if not url or not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"source n={n!r} has invalid url: {url!r}")
        else:
            if url in seen_urls:
                problems.append(f"duplicate url {url!r} in sources n={seen_urls[url]} and n={n}")
            else:
                seen_urls[url] = n

    # 2. Tách body và references theo tiêu đề "## References"
    matches = list(_REF_HEADING.finditer(report_text))
    if not matches:
        problems.append("missing heading '## References'")
        return problems

    ref_match = matches[-1]
    body = report_text[:ref_match.start()]
    references_section = report_text[ref_match.end():]

    # 3. Lấy tất cả [n] trong body (bỏ qua code block)
    segments = _CODE.split(body)
    cited_numbers = set()
    for i, seg in enumerate(segments):
        if i % 2 == 1:
            continue  # Bỏ qua khối code
        for match in _GROUP.finditer(seg):
            for num in _extract_group_numbers(match.group(1)):
                cited_numbers.add(num)

    for num in sorted(cited_numbers):
        if num not in valid_source_nums:
            problems.append(f"[{num}] cited in body but missing from sources.json")

    for num in sorted(valid_source_nums):
        if num not in cited_numbers:
            problems.append(f"source [{num}] never cited in body")

    # 4. Kiểm tra từng dòng trong phần ## References
    ref_lines = [line.strip() for line in references_section.splitlines() if line.strip()]
    sources_by_n = {s["n"]: s for s in sources if isinstance(s.get("n"), int)}
    seen_ref_ns = set()

    for line in ref_lines:
        m = re.match(r"^\[(\d+)\]", line)
        if not m:
            continue
        n_val = int(m.group(1))
        if n_val in seen_ref_ns:
            problems.append(f"duplicate reference entry for [{n_val}]")
        seen_ref_ns.add(n_val)

        if n_val not in sources_by_n:
            problems.append(f"reference [{n_val}] exists in ## References but not in sources.json")
            continue

        # Kiểm tra URL trong dòng reference
        urls_in_line = _URL_REGEX.findall(line)
        expected_url = sources_by_n[n_val].get("url")
        if len(urls_in_line) == 0:
            problems.append(f"reference [{n_val}] does not contain any URL")
        elif len(urls_in_line) > 1:
            problems.append(f"reference [{n_val}] contains multiple URLs: {urls_in_line}")
        else:
            ref_url = urls_in_line[0].rstrip(").,;")
            if ref_url != expected_url:
                problems.append(f"reference [{n_val}] url mismatch: {ref_url!r} != {expected_url!r}")

    # Kiểm tra xem có nguồn nào bị thiếu dòng reference không
    for num in sorted(valid_source_nums):
        if num not in seen_ref_ns:
            problems.append(f"source [{num}] is missing from ## References section")

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
