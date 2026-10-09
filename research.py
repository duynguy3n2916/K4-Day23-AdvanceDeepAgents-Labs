"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401
from collections import Counter  # noqa: F401
from pathlib import Path

from agents import FINALIZER_PATH, NOTES_DIR, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent  # noqa: F401
from model import make_model  # noqa: F401
from sandbox import download, open_sandbox, upload  # noqa: F401

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    if not topic or not str(topic).strip():
        return "topic"
    s = re.sub(r"[^\w]+", "-", str(topic).strip().lower()).strip("-")
    s = s[:60].rstrip("-")
    return s if s else "topic"


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return (
        f"Please conduct an in-depth academic deep research survey on the topic: '{topic}'.\n\n"
        "Follow your 8-step protocol strictly:\n"
        "1. Plan with write_todos and split into at least 3 independent sub-questions.\n"
        "2. Delegate each sub-question to a researcher subagent with the task tool (at least 3 subagent calls).\n"
        f"3. Review the collected notes in {NOTES_DIR}.\n"
        f"4. Merge notes into {SOURCES_PATH} (ensuring at least 3 distinct source families among arxiv, hf-daily, hf-search, web).\n"
        f"5. Write the comprehensive report body to {REPORT_PATH} following REPORT_TEMPLATE.md (do NOT write ## References).\n"
        f"6. Run python3 {FINALIZER_PATH} using the execute tool to regenerate References and synchronize sources.json.\n"
        f"7. Run python3 {VALIDATOR_PATH} using the execute tool until it prints OK.\n"
        "8. Spot-check 2-3 key claims with the citation-checker subagent.\n"
    )


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}."""
    tool_counts = Counter()
    subagent_calls = 0
    input_tokens = 0
    output_tokens = 0

    for msg in messages:
        # Lấy tool_calls
        tool_calls = []
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls = msg.tool_calls
        elif isinstance(msg, dict) and "tool_calls" in msg:
            tool_calls = msg["tool_calls"]

        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name:
                tool_counts[name] += 1
                if name == "task":
                    subagent_calls += 1

        # Lấy tokens
        usage = getattr(msg, "usage_metadata", None) or (msg.get("usage_metadata") if isinstance(msg, dict) else None)
        if isinstance(usage, dict):
            input_tokens += int(usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
            output_tokens += int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)

    return {
        "model": str(model_name),
        "elapsed_s": round(float(elapsed), 1),
        "subagent_calls": subagent_calls,
        "tool_calls": dict(tool_counts),
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
        },
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path."""
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report_bytes = files.get(REPORT_PATH)
    sources_bytes = files.get(SOURCES_PATH)

    if not report_bytes or not report_bytes.strip():
        raise RuntimeError(f"Report is missing or empty at {REPORT_PATH}")

    if not sources_bytes or not sources_bytes.strip():
        raise RuntimeError(f"sources.json is missing or empty at {SOURCES_PATH}")

    try:
        sources_data = json.loads(sources_bytes.decode("utf-8"))
        if not isinstance(sources_data, list):
            raise ValueError("sources.json must be a JSON list")
    except Exception as exc:
        raise RuntimeError(f"sources.json is invalid JSON: {exc}")

    slug = slugify(topic)
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    summary_info = summarize(messages, elapsed, model_name)
    distinct_families = sorted(list({
        s.get("source") for s in sources_data if isinstance(s, dict) and s.get("source")
    }))

    meta = {
        "topic": topic,
        **summary_info,
        "n_sources": len(sources_data),
        "source_families": distinct_families,
    }

    md_path = reports_dir / f"{slug}.md"
    sources_path = reports_dir / f"{slug}.sources.json"
    meta_path = reports_dir / f"{slug}.meta.json"

    sources_path.write_text(json.dumps(sources_data, ensure_ascii=False, indent=2), encoding="utf-8")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_bytes(report_bytes)

    return md_path


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    clean_topic = str(topic).strip()
    if not clean_topic:
        print("Usage: python research.py \"<topic>\"", file=sys.stderr)
        return 2

    try:
        model = make_model()
    except Exception as exc:
        print(f"Error initializing model: {exc}", file=sys.stderr)
        return 1

    model_name = os.getenv("LAB_MODEL") or getattr(model, "model_name", None) or getattr(model, "model", None) or str(model)
    start_time = time.monotonic()

    print(f"Starting research on topic: {clean_topic!r} using model: {model_name}")

    with open_sandbox() as backend:
        try:
            # Tạo thư mục làm việc trong sandbox
            backend.execute(f"mkdir -p {NOTES_DIR} {WORKDIR}/report")

            # Tải validator và finalizer lên sandbox
            upload(backend, {
                VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(),
                FINALIZER_PATH: FINALIZER_SOURCE.read_bytes(),
            })

            agent = build_lead_agent(backend, model)
            prompt = build_prompt(clean_topic)

            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 1000},
            )

            messages = result.get("messages", []) if isinstance(result, dict) else []
            elapsed = time.monotonic() - start_time

            saved_path = save_outputs(backend, clean_topic, messages, elapsed, model_name)
            print(f"SUCCESS: Report saved to {saved_path}")
            return 0

        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
