# Parliament Bulletin Bot

Checks the official Parliament of India site (`sansad.in`) for today's Lok Sabha and
Rajya Sabha **List of Business**, **Revised List of Business**, **Bulletin-I**, and
**Bulletin-II**, and posts each PDF into a Slack channel as soon as it's published.
Bulletins typically land 1-2 hours after the House adjourns for the day; the Lists of
Business are usually out before/around the start of the sitting.

Runs on a GitHub Actions schedule. No server, no paid hosting — free on a public repo.

## How it works

`sansad.in` exposes a plain JSON endpoint per house that lists today's business
documents, each with a direct PDF link (`null` until published):

- Lok Sabha: `https://sansad.in/api_ls/ppHome/DailyCalendar?day=D&month=M&year=Y&locale=en`
- Rajya Sabha: `https://sansad.in/api_rs/ppHome/DailyCalendar?day=D&month=M&year=Y&locale=en`

The response also includes Questions List(s), Synopsis, and Papers to be Laid, which
this bot deliberately ignores — `check_bulletins.py` walks the response generically
(the two houses shape it differently: LS nests documents as single objects, RS as
lists) and keeps only entries whose name contains "list of business" or "bulletin".

It polls both every 15 minutes (03:00-16:59 UTC, i.e. ~8:30am-10:30pm IST) via a GitHub
Actions cron schedule, and for any document URL it hasn't posted before (tracked in
`state.json`, committed back to the repo after each run), it downloads the PDF and
uploads it to Slack.

## 1. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it (e.g. "Parliament Bulletin Bot") and pick your workspace.
3. In the app settings, go to **OAuth & Permissions** → scroll to **Scopes** →
   **Bot Token Scopes**, and add:
   - `files:write`
   - `chat:write`
4. Scroll up and click **Install to Workspace**, then **Allow**.
5. Copy the **Bot User OAuth Token** (starts with `xoxb-`) — you'll need it as a GitHub
   secret below. Treat it like a password; don't paste it anywhere public.
6. In Slack, open the channel you want bulletins posted to and invite the bot:
   `/invite @Parliament Bulletin Bot` (or whatever you named it).
7. Get the channel ID: in Slack, open the channel → click the channel name at the top →
   scroll to the bottom of the "About" tab → copy the **Channel ID** (starts with `C`).

## 2. Create the GitHub repo and push this code

```bash
cd parliament-bulletin-bot
git init
git add .
git commit -m "Parliament bulletin bot"
```

Create a new **public** repo on GitHub (Actions minutes are unlimited on public repos),
then:

```bash
git remote add origin https://github.com/<your-username>/parliament-bulletin-bot.git
git branch -M main
git push -u origin main
```

## 3. Add the token as a secret and the channel ID as a variable

In the GitHub repo: **Settings → Secrets and variables → Actions**.

- **Secrets** tab → **New repository secret** → name `PARLIAMENT_SLACK_BOT_TOKEN`,
  value the `xoxb-...` token from step 1. (Not named `SLACK_BOT_TOKEN` — that name was
  already taken in this repo.)
- **Variables** tab → **New repository variable** → name `SLACK_CHANNEL_ID`, value the
  channel ID from step 1. This one isn't secret, just an identifier, so it's a plain
  variable rather than an encrypted secret.

## 4. Allow the workflow to commit state back

The bot commits `state.json` after each run so it doesn't repost the same bulletin.
Enable this in: **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → Save.

## 5. Test it

Go to the **Actions** tab → **Check Parliament Bulletins** → **Run workflow** to trigger
it manually. Check the run logs — during Parliament's inter-session periods or before a
day's bulletins are published, it'll just print "not yet available" and exit cleanly,
which is expected.

Once the workflow runs successfully, it's fully automated on the schedule in
`.github/workflows/check-bulletins.yml`.

## Notes

- **Cost**: $0/month in the normal case — public repo, no browser automation, each run
  takes a few seconds. Comfortably within even a private repo's free 2,000 Actions
  minutes/month if you'd rather keep it private (adjust the repo secrets/permissions
  steps accordingly).
- **Schedule**: GitHub's cron scheduler isn't second-precise and can lag a few minutes
  under load, and GitHub auto-disables scheduled workflows after 60 days with no repo
  activity — push any commit (or just re-enable it from the Actions tab) if that happens.
- **Adjusting the polling window**: edit the `cron` line in
  `.github/workflows/check-bulletins.yml`. Times are UTC; IST is UTC+5:30.
- **Weekends/recess**: the script runs every day regardless of whether Parliament is
  sitting — the API just returns `null` bulletin URLs and the script exits without
  posting or erroring, so there's no need to track the session calendar separately.
