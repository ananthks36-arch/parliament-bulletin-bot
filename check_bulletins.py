"""Check Lok Sabha / Rajya Sabha Bulletin I & II for today, post any new ones to Slack."""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
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

BULLETIN_KEYS = {
    "bulletin1Url": "Bulletin-I",
    "bulletin2Url": "Bulletin-II",
}


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


def post_to_slack(client, channel, filename, title, pdf_bytes):
    client.files_upload_v2(
        channel=channel,
        filename=filename,
        title=title,
        content=pdf_bytes,
        initial_comment=title,
    )


def main():
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=slack_token)

    state = load_state()
    posted = set(state["posted_urls"])

    now = datetime.now(IST)
    day, month, year = now.day, now.month, now.year
    date_str = now.strftime("%d-%m-%Y")

    found_new = False

    for house_key, house in HOUSES.items():
        try:
            data = fetch_daily_calendar(house["api"], day, month, year)
        except Exception as e:
            print(f"[{house['label']}] failed to fetch daily calendar: {e}")
            continue

        for json_key, label in BULLETIN_KEYS.items():
            entry = data.get(json_key)
            if not entry or not entry.get("url"):
                continue
            url = entry["url"]
            if url in posted:
                continue

            print(f"[{house['label']}] new {label} found: {url}")
            try:
                pdf_bytes = download_pdf(url)
            except Exception as e:
                print(f"  failed to download: {e}")
                continue

            filename = f"{house_key.upper()}_{label.replace('-', '')}_{date_str}.pdf"
            title = f"{house['label']} {label} — {date_str}"

            try:
                post_to_slack(client, channel, filename, title, pdf_bytes)
            except SlackApiError as e:
                print(f"  failed to post to Slack: {e.response['error']}")
                continue

            posted.add(url)
            found_new = True
            print(f"  posted to Slack as {filename}")

    state["posted_urls"] = list(posted)
    save_state(state)

    if not found_new:
        print("No new bulletins this run.")


if __name__ == "__main__":
    main()
