"""Generate grounded document summaries locally with Qwen3 via Ollama.

No external AI service or AI API key is used. Failed jobs remain in state.json and
are retried on a later poll; PDF delivery is never blocked by summarization.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from pypdf import PdfReader
from slack_sdk import WebClient
from check_bulletins import STATE_FILE, download_pdf, save_state

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen2.5:3b")
MIN_EXTRACTED_CHARS = 500
MAX_SOURCE_CHARS = 70000
MAX_OCR_PAGES = 25
MAX_ATTEMPTS = 3
PAGE_CITATION = re.compile(
    r"\(pp?\.\s*\d+(?:\s*[-–]\s*\d+)?\)[.!]?\s*$", re.IGNORECASE
)


def extract_pdf_text(pdf_bytes):
    """Extract page-labelled text so every summary claim can cite its PDF page."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[Page {page_number}]\n{text}")
    return "\n\n".join(pages)


def ocr_pdf(pdf_bytes):
    """OCR scanned PDFs when Poppler and Tesseract are available on the runner."""
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "document.pdf"
        image_prefix = Path(tmp) / "page"
        pdf_path.write_bytes(pdf_bytes)
        subprocess.run(
            [
                "pdftoppm", "-jpeg", "-r", "130", "-f", "1", "-l",
                str(MAX_OCR_PAGES), str(pdf_path), str(image_prefix),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        pages = []
        image_paths = sorted(
            Path(tmp).glob("page-*.jpg"),
            key=lambda path: int(path.stem.rsplit("-", 1)[-1]),
        )
        for page_number, image_path in enumerate(image_paths, start=1):
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "eng"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.stdout.strip():
                pages.append(f"[Page {page_number}]\n{result.stdout.strip()}")
        return "\n\n".join(pages)


def source_excerpt(text):
    """Keep both ends when a long document exceeds the local model context budget."""
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    half = MAX_SOURCE_CHARS // 2
    return text[:half] + "\n\n[Middle pages omitted for context limit]\n\n" + text[-half:]


def build_prompt(job, document_text, comparison_text=None):
    label = job["label"]
    lowered = label.lower()
    if "revised list of business" in lowered:
        task = (
            "Compare the REVISED document with the ORIGINAL List of Business. State "
            "the meaningful additions, removals, reordered items, and timing changes. "
            "Then explain in plain English what members or observers should pay "
            "attention to. If a difference is not evidenced, do not claim it."
        )
    elif "part-ii" in lowered or "bulletin-ii" in lowered or "bulletin part-ii" in lowered:
        task = (
            "Summarize the important notices, deadlines, committee or member information, "
            "and practical follow-ups. Explain their parliamentary significance briefly."
        )
    else:
        task = (
            "Summarize the proceedings, decisions, bills, motions, and adjournment details. "
            "Explain their parliamentary significance briefly."
        )

    original_section = ""
    if comparison_text:
        original_section = f"\n\nORIGINAL LIST OF BUSINESS:\n{source_excerpt(comparison_text)}"

    return f"""You are summarizing an official Parliament of India document for a reader who wants
the practical meaning, not procedural boilerplate.

Document: {job['house']} {label}, dated {job['date']}.
Task: {task}

Rules:
- Use only the supplied document text. Do not invent names, events, outcomes, or context.
- Give 4-6 concise, single-line bullets in strict descending importance—not
  document/page order. Do not use sub-bullets.
- Scan the entire document, including its final pages, before selecting bullets.
  Explicitly look for "passed", "adopted", "negatived", "introduced", bills,
  statutory resolutions, and binding decisions; do not omit these in favour of
  routine papers, references, questions, committee listings, or adjournment.
- Rank passed/defeated/introduced bills and binding decisions first; then substantive
  motions and resolutions; major policy, financial, or regulatory matters; important
  committee findings; ministerial statements; matters raised by members; and routine
  procedure or adjournment last. For a Revised List of Business, rank the most
  consequential change from the original first.
- End every factual bullet with the supporting PDF page, such as (p. 3).
- You may add a short plain-English significance sentence, but label it "Context:" and
  keep it limited to what follows directly from the document.
- If the text is insufficient, say so explicitly.
- Start the response immediately with the first "- " bullet.
- Return only the reader-facing bullets and optional Context sentence. Never output
  analysis, chain-of-thought, <think> tags, a heading, or a preamble.

CURRENT/REVISED DOCUMENT:
{source_excerpt(document_text)}{original_section}

Return the reader-facing summary now.
"""


def generate_summary(prompt):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.0, "num_ctx": 32768, "num_predict": 900},
        },
        timeout=900,
    )
    response.raise_for_status()
    payload = response.json()
    raw = (payload.get("message") or {}).get("content", "").lstrip()
    # The prompt supplies the first bullet marker as a completion prefix. Ollama
    # returns only the continuation, so restore that marker before validation.
    if raw and not raw.startswith(("- ", "• ")):
        raw = "- " + raw
    return validate_summary(clean_model_output(raw))


def clean_model_output(text):
    """Remove any reasoning markup before text can reach Slack."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    cleaned = re.sub(r"```(?:markdown)?|```", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("**", "")
    return cleaned.strip()


def validate_summary(text):
    """Allow only finished, cited bullets; reject reasoning and prose monologues."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [line for line in lines if line.startswith(("- ", "• "))]
    contexts = [line for line in lines if line.startswith("Context:")]
    if not 4 <= len(bullets) <= 6:
        raise ValueError("summary must contain 4-6 bullets")
    if len(lines) != len(bullets) + len(contexts) or len(contexts) > 1:
        raise ValueError("summary contains non-bullet commentary")
    return "\n".join(lines)


def text_from_url(url):
    pdf_bytes = download_pdf(url)
    text = extract_pdf_text(pdf_bytes)
    if len(text.strip()) < MIN_EXTRACTED_CHARS:
        print("  PDF has little embedded text; using OCR fallback")
        text = ocr_pdf(pdf_bytes) or text
    return text.strip()


def post_summary(client, job, summary):
    if not job.get("thread_ts"):
        raise ValueError("PDF message timestamp missing; refusing a standalone AI post")
    text = f"*AI-generated local summary — verify against the attached PDF*\n{summary[:3600]}"
    client.chat_postMessage(
        channel=job["channel"], text=text, thread_ts=job["thread_ts"]
    )


def main():
    state = json.loads(STATE_FILE.read_text())
    pending = list(state.get("pending_summaries", []))
    if not pending:
        print("No pending summaries.")
        return

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    remaining = []
    for job in pending:
        print(f"Summarizing {job['house']} {job['label']} — {job['date']}")
        try:
            document_text = text_from_url(job["url"])
            if not document_text:
                raise ValueError("no extractable text found")
            comparison_text = None
            if job.get("comparison_url"):
                comparison_text = text_from_url(job["comparison_url"])
            summary = generate_summary(build_prompt(job, document_text, comparison_text))
            if not summary:
                raise ValueError("local model returned an empty summary")
            post_summary(client, job, summary)
            print("  summary posted to Slack")
        except Exception as error:
            job["attempts"] = int(job.get("attempts", 0)) + 1
            print(f"  summary attempt {job['attempts']} failed: {error}")
            if job["attempts"] < MAX_ATTEMPTS:
                remaining.append(job)
            else:
                print("  giving up after three attempts; PDF was already delivered")

    state["pending_summaries"] = remaining
    save_state(state)


if __name__ == "__main__":
    main()
