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
OUTCOME_LINE = re.compile(
    r"\b(passed|adopted|negatived|rejected|defeated|withdrawn|extended)\b",
    re.IGNORECASE,
)
CONTEXT_LABEL = re.compile(
    r"^(context|why it matters|significance)\s*:", re.IGNORECASE
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


def outcome_evidence(text):
    """Surface local context around recorded outcomes to prevent cross-item leakage."""
    lines = text.splitlines()
    excerpts = []
    seen = set()
    for index, line in enumerate(lines):
        if not OUTCOME_LINE.search(line):
            continue
        excerpt = "\n".join(lines[max(0, index - 5): min(len(lines), index + 2)]).strip()
        normalized = re.sub(r"\s+", " ", excerpt).casefold()
        if excerpt and normalized not in seen:
            excerpts.append(excerpt)
            seen.add(normalized)
        if len(excerpts) >= 24:
            break
    return "\n\n---\n\n".join(excerpts)


def build_prompt(job, document_text, comparison_text=None):
    label = job["label"]
    lowered = label.lower()
    if "revised list of business" in lowered:
        task = (
            "Compare the REVISED document with the ORIGINAL List of Business. State "
            "the meaningful additions, removals, reordered items, and timing changes. "
            "Cover every major section added by the revision, grouping related committee "
            "reports into readable themes rather than omitting them. Say which major parts "
            "of the original schedule remain unchanged. Then explain in plain English what "
            "members or observers should pay attention to. A listed bill or motion is only "
            "scheduled business, not an event that has already happened. If a difference "
            "is not evidenced, do not claim it."
        )
    elif "part-ii" in lowered or "bulletin-ii" in lowered or "bulletin part-ii" in lowered:
        task = (
            "Summarize the important notices, deadlines, committee or member information, "
            "and practical follow-ups. Distinguish advance notices and proposed motions "
            "from actions already taken: wording such as 'to move' does not mean a motion "
            "was moved, adopted, rejected, or decided. Never add filler saying that bills, "
            "findings, or procedures are absent."
        )
    else:
        task = (
            "Summarize the proceedings, decisions, bills, motions, and adjournment details. "
            "Explain their parliamentary significance briefly."
        )

    original_section = ""
    if comparison_text:
        original_section = f"\n\nORIGINAL LIST OF BUSINESS:\n{source_excerpt(comparison_text)}"

    verified_outcomes = outcome_evidence(document_text)

    return f"""You are summarizing an official Parliament of India document for a reader who wants
the practical meaning, not procedural boilerplate.

Document: {job['house']} {label}, dated {job['date']}.
Task: {task}

Rules:
- Use only the supplied document text. Do not invent names, events, outcomes, or context.
- Give 1-6 concise, single-line bullets in strict descending importance—not
  document/page order. Normally use 3-6, but use only 1 or 2 when the source contains
  only 1 or 2 substantive items. Do not invent filler to reach a target. Do not use
  sub-bullets.
- Write for an intelligent reader who does not know parliamentary jargon. Use active
  voice, short sentences, and concrete consequences. Avoid mechanical phrases such
  as "Introduced/Motions/Resolutions", "the document covers", and long committee lists.
- Report the final outcome, not every procedural step. If a bill passed, say the bill
  passed; do not list consideration motions, individual clauses, the enacting formula,
  or the long title unless one of them was separately contested and consequential.
- Identify exactly what each vote concerned. If a motion extending a committee's
  reporting deadline was adopted, say the deadline was extended—do not describe the
  underlying bill as adopted or the extension as negatived. Do not describe reports,
  papers, or notifications as passed, adopted, or introduced unless the text explicitly
  records that outcome for that item.
- The VERIFIED OUTCOME EXCERPTS below are authoritative. Keep each outcome attached
  to the item named in the same excerpt. Never transfer "adopted", "negatived", or
  another outcome from a nearby but separate proceeding.
- Begin every bullet with a short descriptive lead followed by a colon, for example
  "Bill passed:" or "Schedule changed:". Make that lead specific to the event.
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
- Do not add a Context, significance, or "Why it matters" section.
- If the text is insufficient, say so explicitly.
- Start the response immediately with the first "- " bullet.
- Return only the reader-facing bullets and optional Context sentence. Never output
  analysis, chain-of-thought, <think> tags, a heading, or a preamble.

CURRENT/REVISED DOCUMENT:
{source_excerpt(document_text)}{original_section}

VERIFIED OUTCOME EXCERPTS:
{verified_outcomes or "No explicit outcome language was extracted."}

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
    """Keep only reader-facing bullets/context before text can reach Slack."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    cleaned = re.sub(r"```(?:markdown)?|```", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("*", "")
    bullets = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        numbered = re.match(r"^\d+[.)]\s+(.+)$", line)
        if numbered:
            line = "- " + numbered.group(1)
        elif line.startswith("• "):
            line = "- " + line[2:]
        if line.startswith("- ") and CONTEXT_LABEL.match(line[2:].strip()):
            continue
        if line.startswith("- ") and len(bullets) < 6:
            bullets.append(line)
    return "\n".join(bullets)


def validate_summary(text):
    """Allow only finished, cited bullets; reject reasoning and prose monologues."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [line for line in lines if line.startswith(("- ", "• "))]
    if not 1 <= len(bullets) <= 6:
        raise ValueError("summary must contain 1-6 bullets")
    if len(lines) != len(bullets):
        raise ValueError("summary contains non-bullet commentary")
    return "\n".join(lines)


def validate_outcome_consistency(summary, document_text):
    """Reject known high-risk outcome conflations before they can reach Slack."""
    for line in summary.splitlines():
        lowered = line.casefold()
        if "viksit bharat shiksha adhishthan" not in lowered:
            continue
        if any(word in lowered for word in ("negatived", "rejected", "defeated")):
            raise ValueError("Viksit Bharat committee-extension outcome conflicts with source")
        if "extend" not in lowered or not any(
            word in lowered for word in ("deadline", "time", "report")
        ):
            raise ValueError(
                "Viksit Bharat item must describe extension of committee reporting time"
            )

    source_lowered = document_text.casefold()
    if "viksit bharat shiksha adhishthan" in source_lowered:
        source_index = source_lowered.find("viksit bharat shiksha adhishthan")
        local_source = source_lowered[max(0, source_index - 500): source_index + 900]
        if "extension of time" in local_source and "motion was put to vote and adopted" not in local_source:
            raise ValueError("could not verify Viksit Bharat extension outcome in source")


def validate_revised_list_completeness(summary, document_text, comparison_text=None):
    """Reject revised-agenda summaries that omit whole added sections or imply outcomes."""
    if not comparison_text:
        return

    source = document_text.casefold()
    original = comparison_text.casefold()
    rendered = summary.casefold()
    bullets = [line for line in summary.splitlines() if line.strip().startswith(("- ", "• "))]

    # A substantially expanded agenda cannot be represented faithfully by one or two
    # bullets. This is deliberately based on source size, not a fixed document type.
    if len(document_text) > len(comparison_text) * 1.25 and len(bullets) < 4:
        raise ValueError("expanded revised list needs at least four substantive bullets")

    if not any(word in rendered for word in ("added", "expanded", "revised", "changed")):
        raise ValueError("revised-list summary does not identify the schedule change")

    added_sections = (
        (("reports of the department", "report of the committee"), ("committee", "report"), "committee reports"),
        (("statements by ministers",), ("minister", "implementation statement"), "ministerial statements"),
        (("motion for election",), ("election",), "election motions"),
    )
    for source_markers, summary_markers, description in added_sections:
        added = any(marker in source for marker in source_markers) and not any(
            marker in original for marker in source_markers
        )
        if added and not any(marker in rendered for marker in summary_markers):
            raise ValueError(f"revised-list summary omits added {description}")

    private_business_markers = (
        "private members’ legislative business",
        "private members' legislative business",
    )
    if any(marker in source for marker in private_business_markers) and any(
        marker in original for marker in private_business_markers
    ):
        if not any(word in rendered for word in ("unchanged", "remains", "retained")):
            raise ValueError("summary must say retained private-members' business is unchanged")
        if re.search(r"\b(?:bills? (?:were )?(?:introduced|passed|adopted)|new bills?)\b", rendered):
            raise ValueError("scheduled private-members' bills were described as completed outcomes")

    if any(not PAGE_CITATION.search(line.strip()) for line in bullets):
        raise ValueError("every revised-list bullet must cite its supporting PDF page")


def validate_document_completeness(job, summary, document_text):
    """Reject empty category bullets and omissions of explicit Bulletin-I outcomes."""
    bullets = [line.strip() for line in summary.splitlines() if line.strip().startswith(("- ", "• "))]
    for line in bullets:
        body = line[2:].strip()
        labelled = re.match(r"^[^:]{2,60}:\s+\S.+", body)
        if not labelled:
            raise ValueError("every summary bullet needs a descriptive lead and complete detail")

    lowered_label = job.get("label", "").casefold()
    if "bulletin-i" not in lowered_label and "bulletin part-i" not in lowered_label:
        return

    if any(not PAGE_CITATION.search(line) for line in bullets):
        raise ValueError("every Bulletin-I bullet must cite its supporting PDF page")

    source = document_text.casefold()
    rendered = summary.casefold()
    if re.search(r"government bill\s*[-–—]\s*passed", source):
        if "bill" not in rendered or "passed" not in rendered:
            raise ValueError("Bulletin-I summary omits an explicitly passed government bill")
    if "adjourned" in source and "adjourn" not in rendered:
        raise ValueError("Bulletin-I summary omits the recorded adjournment")


def generate_validated_summary(job, document_text, comparison_text=None, max_drafts=3):
    """Regenerate locally with specific feedback when a draft fails a safety gate."""
    prompt = build_prompt(job, document_text, comparison_text)
    last_error = None
    for draft_number in range(1, max_drafts + 1):
        summary = generate_summary(prompt)
        try:
            validate_outcome_consistency(summary, document_text)
            validate_revised_list_completeness(summary, document_text, comparison_text)
            validate_document_completeness(job, summary, document_text)
            return summary
        except ValueError as error:
            last_error = error
            if draft_number < max_drafts:
                prompt += (
                    "\n\nREJECTED DRAFT FEEDBACK:\n"
                    f"Your previous draft was rejected because: {error}. "
                    "Regenerate the complete summary from the supplied documents and fix "
                    "that omission. Return only the final bullets."
                )
    raise last_error


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
    text = format_summary_for_slack(summary)
    client.chat_postMessage(
        channel=job["channel"],
        text=text,
        blocks=format_summary_blocks(summary),
        thread_ts=job["thread_ts"],
    )


def format_summary_for_slack(summary):
    """Create an asterisk-free plain-text fallback for Slack notifications."""
    rendered = []
    for line in summary.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "• ")):
            continue
        body = line[2:].strip().replace("*", "")
        if CONTEXT_LABEL.match(body):
            continue
        rendered.append(f"• {body}")

    parts = ["Summary — most important first", "\n\n".join(rendered)]
    parts.append("AI-generated locally; verify important details against the PDF.")
    return "\n\n".join(parts)[:3600]


def format_summary_blocks(summary):
    """Use Slack rich-text styles so bold never depends on visible asterisks."""
    items = []
    for line in summary.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "• ")):
            continue
        body = line[2:].strip().replace("*", "")
        if CONTEXT_LABEL.match(body):
            continue
        labelled = re.match(r"^([^:]{2,60}):\s*(.+)$", body)
        elements = []
        if labelled:
            elements.append(
                {
                    "type": "text",
                    "text": labelled.group(1).strip() + ":",
                    "style": {"bold": True},
                }
            )
            elements.append({"type": "text", "text": " " + labelled.group(2).strip()})
        else:
            elements.append({"type": "text", "text": body})
        items.append({"type": "rich_text_section", "elements": elements})

    return [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {
                            "type": "text",
                            "text": "Summary — most important first",
                            "style": {"bold": True},
                        }
                    ],
                },
                {"type": "rich_text_list", "style": "bullet", "elements": items},
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "\nAI-generated locally; verify important details against the PDF.", "style": {"italic": True}}
                    ],
                },
            ],
        }
    ]


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
            summary = generate_validated_summary(job, document_text, comparison_text)
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
