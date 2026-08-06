"""Check Lok Sabha / Rajya Sabha business documents, post any new ones to Slack.

Only List of Business, Revised List of Business, and Bulletin-I/II are wanted —
Questions List(s), Synopsis, and Papers to be Laid are filtered out even though the
API returns them alongside the rest.

Checks a rolling window around today: previous sitting dates catch documents uploaded
after midnight, while future dates catch Lists of Business published in advance.
"""

import json
import hashlib
import io
from difflib import SequenceMatcher
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = Path(__file__).parent / "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HOUSES = {
    "ls": {
        "label": "Lok Sabha",
        "api": "https://sansad.in/api_ls/ppHome/DailyCalendar",
    },
    "rs": {
        "label": "Rajya Sabha",
        "api": "https://sansad.in/api_rs/ppHome/DailyCalendar",
    },
}


WANTED_NAME_SUBSTRINGS = ("list of business", "bulletin")

# List of Business for a sitting day is typically published the evening before, not
# on the day itself — so also check a few days ahead, not just today. Bulletins are
# never published in advance, so this is a no-op for them (the API just returns null
# for future dates until the day is adjourned).
LOOKAHEAD_DAYS = 3
LOOKBACK_DAYS = 3
REHASH_SIMILARITY_THRESHOLD = 0.99


def parse_document_date(value, fallback):
    """Return the API document date, tolerating the formats used by LS and RS."""
    if not value:
        return fallback
    date_text = str(value).split()[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_text, fmt).date()
        except ValueError:
            pass
    return fallback


def build_target_dates(today):
    """Include recent sitting dates and advance publication dates."""
    return [
        today + timedelta(days=offset)
        for offset in range(-LOOKBACK_DAYS, LOOKAHEAD_DAYS + 1)
    ]


def extract_documents(data, fallback_date=None):
    """Flatten every {name, url, ...} entry in the DailyCalendar response, keeping
    only List of Business / Revised List of Business / Bulletin-I / Bulletin-II.

    The API mixes single objects (e.g. bulletin1Url) and lists of objects (e.g.
    questionListUrls) across LS/RS, and some slots are null until published. This
    walks every top-level value generically rather than hardcoding each key, then
    filters by name so only the wanted document types survive.
    """
    docs = []

    def handle(item):
        if isinstance(item, dict):
            url, name = item.get("url"), item.get("name")
            if url and name and any(s in name.lower() for s in WANTED_NAME_SUBSTRINGS):
                docs.append((name, url, parse_document_date(item.get("date"), fallback_date)))

    for value in data.values():
        if isinstance(value, list):
            for item in value:
                handle(item)
        else:
            handle(value)
    return docs


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posted_urls": []}


def save_state(state):
    state["posted_urls"] = state["posted_urls"][-200:]
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def write_workflow_output(name, value):
    """Expose a value to later GitHub Actions steps; harmless during local runs."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def fetch_daily_calendar(api_url, day, month, year):
    resp = requests.get(
        api_url,
        params={"day": day, "month": month, "year": year, "locale": "en"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def download_pdf(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.content


def post_to_slack(client, channel, filename, title, pdf_bytes):
    """Upload immediately; summary generation deliberately happens afterwards."""
    return client.files_upload_v2(
        channel=channel,
        filename=filename,
        title=title,
        content=pdf_bytes,
        initial_comment=title,
    )


def get_upload_message_ts(response):
    """Best-effort extraction of the Slack message containing an uploaded file."""
    file_info = response.get("file")
    if not file_info:
        files = response.get("files") or []
        file_info = files[0] if files else {}
    for visibility in ("public", "private"):
        for shares in (file_info.get("shares") or {}).get(visibility, {}).values():
            if shares and shares[0].get("ts"):
                return shares[0]["ts"]
    return None


def should_summarize(label):
    lowered = label.lower()
    return "bulletin" in lowered or "revised list of business" in lowered


def find_original_list_url(documents):
    for label, url, _ in documents:
        if label.strip().lower() == "list of business":
            return url
    return None


def document_key(house_key, label, document_date):
    """Stable identity even when Sansad replaces a PDF with a fresh URL."""
    normalized_label = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"{house_key}:{document_date.isoformat()}:{normalized_label}"


def pdf_normalized_text(pdf_bytes):
    """Extract visible text while removing layout-only whitespace differences."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return re.sub(r"\s+", " ", text).strip().casefold()
    except Exception as error:
        print(f"  could not extract text for fingerprint: {error}")
        return ""


def pdf_text_fingerprint(pdf_bytes):
    normalized = pdf_normalized_text(pdf_bytes)
    source = normalized.encode("utf-8") if normalized else pdf_bytes
    return hashlib.sha256(source).hexdigest()


def text_similarity(first, second):
    if not first or not second:
        return 0.0
    return SequenceMatcher(None, first.split(), second.split()).ratio()


def main():
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=slack_token)

    state = load_state()
    posted_urls = list(state["posted_urls"])
    posted = set(posted_urls)
    pending_summaries = list(state.get("pending_summaries", []))
    pending_urls = {job["url"] for job in pending_summaries}
    posted_documents = dict(state.get("posted_documents", {}))

    today = datetime.now(IST).date()
    target_dates = build_target_dates(today)

    found_new = False

    for house_key, house in HOUSES.items():
        for target_date in target_dates:
            print(f"[{house['label']}] checking sitting date {target_date}")
            try:
                data = fetch_daily_calendar(
                    house["api"], target_date.day, target_date.month, target_date.year
                )
            except Exception as e:
                print(f"[{house['label']}] failed to fetch {target_date}: {e}")
                continue

            documents = extract_documents(data, target_date)
            if not documents:
                print(f"[{house['label']}] no wanted documents for {target_date}")
            original_list_url = find_original_list_url(documents)

            for label, url, document_date in documents:
                identity = document_key(house_key, label, document_date)
                if url in posted:
                    print(f"[{house['label']}] already posted {label} for {document_date}")
                    posted_documents.setdefault(identity, {"url": url})
                    continue

                print(f"[{house['label']}] new {label} found for {document_date}: {url}")
                try:
                    pdf_bytes = download_pdf(url)
                except Exception as e:
                    print(f"  failed to download: {e}")
                    continue

                fingerprint = pdf_text_fingerprint(pdf_bytes)
                normalized_text = pdf_normalized_text(pdf_bytes)
                previous = posted_documents.get(identity)
                is_updated_version = False
                if previous:
                    previous_fingerprint = previous.get("text_fingerprint")
                    previous_text = ""
                    comparison_failed = False
                    if previous.get("url") and previous_fingerprint != fingerprint:
                        try:
                            previous_bytes = download_pdf(previous["url"])
                            previous_fingerprint = pdf_text_fingerprint(previous_bytes)
                            previous_text = pdf_normalized_text(previous_bytes)
                        except Exception as error:
                            print(f"  could not compare previous version: {error}")
                            comparison_failed = True

                    posted.add(url)
                    posted_urls.append(url)
                    similarity = text_similarity(previous_text, normalized_text)
                    if (
                        not previous_fingerprint
                        or previous_fingerprint == fingerprint
                        or comparison_failed
                        or similarity >= REHASH_SIMILARITY_THRESHOLD
                    ):
                        print(
                            "  suppressed replacement URL with unchanged/near-identical "
                            f"text (similarity={similarity:.4f})"
                        )
                        posted_documents[identity] = {
                            "url": url,
                            "text_fingerprint": fingerprint,
                        }
                        continue
                    is_updated_version = True
                    print("  document text changed; posting a clearly labelled update")

                date_str = document_date.strftime("%d-%m-%Y")
                safe_label = re.sub(r"[^A-Za-z0-9]+", "", label)
                filename = f"{house_key.upper()}_{safe_label}_{date_str}.pdf"
                title = f"{house['label']} {label} — {date_str}"
                if document_date > today:
                    title += " (published in advance, for that day's sitting)"
                if is_updated_version:
                    title += " (updated version)"

                try:
                    upload = post_to_slack(client, channel, filename, title, pdf_bytes)
                except SlackApiError as e:
                    print(f"  failed to post to Slack: {e.response['error']}")
                    continue

                posted.add(url)
                if url not in posted_urls:
                    posted_urls.append(url)
                posted_documents[identity] = {
                    "url": url,
                    "text_fingerprint": fingerprint,
                }
                found_new = True
                print(f"  posted to Slack as {filename}")

                if should_summarize(label) and url not in pending_urls:
                    pending_summaries.append(
                        {
                            "url": url,
                            "comparison_url": (
                                original_list_url
                                if "revised list of business" in label.lower()
                                else None
                            ),
                            "house": house["label"],
                            "label": label,
                            "date": date_str,
                            "channel": channel,
                            "thread_ts": get_upload_message_ts(upload),
                            "attempts": 0,
                        }
                    )
                    pending_urls.add(url)
                    print("  queued for local Qwen summary")

    state["posted_urls"] = posted_urls
    state["posted_documents"] = posted_documents
    state["pending_summaries"] = pending_summaries
    save_state(state)
    write_workflow_output("needs_summary", str(bool(pending_summaries)).lower())

    if not found_new:
        print("No new documents this run.")


if __name__ == "__main__":
    main()
