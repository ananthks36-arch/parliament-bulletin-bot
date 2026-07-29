"""Check Lok Sabha / Rajya Sabha business documents, post any new ones to Slack.

Only List of Business, Revised List of Business, and Bulletin-I/II are wanted —
Questions List(s), Synopsis, and Papers to be Laid are filtered out even though the
API returns them alongside the rest.

Checks a rolling window around today: previous sitting dates catch documents uploaded
after midnight, while future dates catch Lists of Business published in advance.
"""

import io
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests
from pypdf import PdfReader
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_MAX_CHARS = 40000  # cap PDF text sent for summarization

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


def extract_pdf_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def summarize_bulletin(anthropic_client, house_label, doc_label, pdf_bytes):
    """Summarize a Bulletin PDF's key items via Claude. Returns None on any failure
    (missing API key, empty text, API error) so a summarization problem never blocks
    posting the PDF itself."""
    if anthropic_client is None:
        return None
    text = extract_pdf_text(pdf_bytes).strip()
    if not text:
        return None
    try:
        resp = anthropic_client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    f"This is the {house_label} {doc_label} for today's sitting of "
                    "Parliament of India. Write a short summary as 4-8 bullet points "
                    "highlighting the most notable items — bills introduced or "
                    "discussed, important questions, motions, or notices. Skip "
                    "routine or procedural boilerplate. Be concise, plain text, no "
                    "markdown headers.\n\n"
                    f"Document text:\n{text[:SUMMARY_MAX_CHARS]}"
                ),
            }],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None
    except Exception as e:
        print(f"  summarization failed: {e}")
        return None


def post_to_slack(client, channel, filename, title, pdf_bytes, summary=None):
    comment = f"{title}\n\n{summary}" if summary else title
    client.files_upload_v2(
        channel=channel,
        filename=filename,
        title=title,
        content=pdf_bytes,
        initial_comment=comment,
    )


def main():
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=slack_token)

    anthropic_client = (
        anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        if os.environ.get("ANTHROPIC_API_KEY")
        else None
    )

    state = load_state()
    posted_urls = list(state["posted_urls"])
    posted = set(posted_urls)

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

            for label, url, document_date in documents:
                if url in posted:
                    print(f"[{house['label']}] already posted {label} for {document_date}")
                    continue

                print(f"[{house['label']}] new {label} found for {document_date}: {url}")
                try:
                    pdf_bytes = download_pdf(url)
                except Exception as e:
                    print(f"  failed to download: {e}")
                    continue

                date_str = document_date.strftime("%d-%m-%Y")
                safe_label = re.sub(r"[^A-Za-z0-9]+", "", label)
                filename = f"{house_key.upper()}_{safe_label}_{date_str}.pdf"
                title = f"{house['label']} {label} — {date_str}"
                if document_date > today:
                    title += " (published in advance, for that day's sitting)"

                summary = None
                if "bulletin" in label.lower():
                    summary = summarize_bulletin(anthropic_client, house["label"], label, pdf_bytes)

                try:
                    post_to_slack(client, channel, filename, title, pdf_bytes, summary)
                except SlackApiError as e:
                    print(f"  failed to post to Slack: {e.response['error']}")
                    continue

                posted.add(url)
                posted_urls.append(url)
                found_new = True
                print(f"  posted to Slack as {filename}")

    state["posted_urls"] = posted_urls
    save_state(state)

    if not found_new:
        print("No new documents this run.")


if __name__ == "__main__":
    main()
