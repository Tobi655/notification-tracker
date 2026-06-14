# Website Notification Tracker - Setup Guide

This package checks the "Public Notices" section of the NEET (NTA) website
every ~5 minutes and sends you a Telegram message whenever something new
appears. It's built so you can add more websites later by editing one config
file.

Files included:
- `notification_tracker.py` - the script (does everything)
- `sites_config.json` - list of websites/sections to watch (edit this to add more sites)
- `state.json` - the script's memory of what it last saw (starts empty)
- `requirements.txt` - Python packages needed
- `.github/workflows/tracker.yml` - makes GitHub run the script every 5 min, for free
- `.gitignore`

---

## 1. Create your Telegram bot

1. In Telegram, search for **@BotFather** and open a chat with it.
2. Send `/newbot`, give it a name and a username (must end in "bot").
3. BotFather replies with a **token** that looks like
   `123456789:AAExampleTokenStringHere`. Save it.
4. Now send any message (e.g. "hi") to your new bot - just open it and type
   something. This is required so Telegram knows where to send replies.
5. In your browser, open:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   (replace `<YOUR_TOKEN>` with your real token).
6. Look for `"chat":{"id": 123456789, ...}` in the response. That number is
   your **chat id**. Save it.

You now have two values: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

---

## 2. Put the code on GitHub (recommended free hosting)

1. Create a free GitHub account if you don't have one: https://github.com/join
2. Create a **new public repository** (e.g. `notification-tracker`).
   - It needs to be **public** for the free unlimited Actions minutes.
   - (A private repo also works, but the free tier is capped at 2,000
     Actions minutes/month - usually enough, but public is safest for true
     24/7 every-5-min checking.)
3. Upload all the files from this package, keeping the folder structure
   (the `.github/workflows/tracker.yml` file must stay in that exact path).

---

## 3. Add your Telegram credentials as GitHub Secrets

1. In your repo, go to **Settings -> Secrets and variables -> Actions**.
2. Click **New repository secret** and add:
   - Name: `TELEGRAM_BOT_TOKEN`  -> Value: (your token from step 1)
3. Add another secret:
   - Name: `TELEGRAM_CHAT_ID`  -> Value: (your chat id from step 1)

These are kept encrypted by GitHub and are only available to your workflow -
they won't show up in logs.

---

## 4. Turn on the workflow

1. Go to the **Actions** tab in your repo.
2. If prompted, click **"I understand my workflows, go ahead and enable
   them"**.
3. Click on **"Website Notification Tracker"** in the left sidebar, then
   click **Run workflow** (this is the `workflow_dispatch` trigger) to test
   it manually once.
4. Open the run and check the logs. On the **first ever run**, you should
   see something like:
   `[NEET (UG) 2026 - Public Notices] First run - baseline saved (N item(s))`
   That's normal - the first run just records the current state, with
   nothing to compare against yet.
5. From here on, it will run automatically every ~5 minutes via the cron
   schedule. Each run that detects a change will message your Telegram bot.

**Note on timing:** GitHub's `schedule` cron is "best effort" - during busy
periods runs can be delayed by a few minutes. For checking a notice board
every 5 minutes this is fine in practice, but if you need *guaranteed* exact
5-minute timing, see the "Always-on VM" alternative at the bottom.

---

## 5. Verify the selector is correct (important)

The config currently points at the "Public Notices" panel on the NEET
homepage using this selector:

```json
"selector": "#1648449005032-46466f25-2ebe"
```

Websites occasionally change their HTML, which can break this. To check it's
working, run locally (needs Python 3.9+):

```bash
pip install -r requirements.txt
python notification_tracker.py --inspect
```

This prints every item the selector currently matches, without sending any
Telegram messages or changing `state.json`. You should see the list of
"Public Notices" titles and PDF links.

### Fixing the selector if it shows 0 items

1. Open https://neet.nta.nic.in/ in Chrome/Edge.
2. Right-click the **"Public Notices"** heading or list -> **Inspect**.
3. In the dev tools, look for the closest ancestor element with an `id` or a
   distinctive `class` (e.g. `id="..."` or `class="vc_tta-panel ..."`).
4. Update `"selector"` in `sites_config.json` to match (CSS selector syntax,
   e.g. `#some-id` or `.some-class`).
5. Re-run `python notification_tracker.py --inspect` until it shows the right
   list of items.

---

## 6. Adding more websites

Edit `sites_config.json` and add another object to the array, e.g.:

```json
{
  "name": "Some Other Site - Notifications",
  "url": "https://example.com/notifications",
  "selector": "#notification-list",
  "item_selector": "a"
}
```

- `selector` = the CSS selector for the container that holds the
  notifications/links.
- `item_selector` = what to extract inside that container (default `"a"` for
  links - usually correct since notices are almost always links to PDFs or
  detail pages). If the items aren't links, you could use something like
  `"li"` instead.

Run `--inspect` again to confirm before relying on it.

---

## 7. How the "blocking" handling works

Government / institutional sites often run anti-bot protection (Cloudflare,
WAFs, rate limits, certificate quirks). The script handles this in layers:

1. **Realistic browser headers** - sends a real Chrome User-Agent,
   `Accept`, `Accept-Language`, etc., instead of the default
   `python-requests` identity that many WAFs block outright.
2. **Retries with backoff** - connection errors, timeouts, and 5xx errors
   are retried up to 3 times with increasing delays.
3. **SSL certificate issues** - some `.nic.in`/`.gov.in` hosts have
   misconfigured certificate chains; if verified HTTPS fails with an SSL
   error, it retries once without verification so you still get the content
   (logged as a warning).
4. **Block/challenge detection** - if the response looks like a CAPTCHA,
   "Access Denied", Cloudflare "Just a moment" page, or returns HTTP
   401/403/429/503, it's treated as a block, not a successful fetch.
5. **cloudscraper fallback** - if plain `requests` is blocked, it
   automatically retries using `cloudscraper`, which mimics a real browser's
   TLS/JS-challenge handling and gets past many basic Cloudflare checks.
6. **Selector-not-found handling** - if the page loads fine but the expected
   section is missing (site redesign), this is reported separately from a
   network block, so you know to update the selector.
7. **Repeated-failure alerts** - if a site fails **3 checks in a row**
   (~15 minutes), you get **one** Telegram warning saying the checker is
   stuck (so you're not silently missing updates), and another message when
   it recovers. It won't spam you every cycle.
8. **State file safety** - `state.json` is written atomically (via a temp
   file + rename) so a crash mid-write can't corrupt your saved history.
9. **Telegram send retries** - network errors when sending the alert are
   retried; messages over Telegram's 4096-character limit are automatically
   split into multiple messages.

If a site uses very heavy JavaScript rendering (content only appears after
JS runs) and even `cloudscraper` can't get it, the next step would be adding
a headless-browser fetch (Playwright). That's heavier (needs browser
binaries) and isn't included by default to keep this free/lightweight, but
ask if you hit that case and need it added for a specific site.

---

## 8. Alternative: always-on VM (true 24/7, exact 5-min interval)

If you want guaranteed exact timing instead of GitHub's best-effort cron:

1. Sign up for **Oracle Cloud Free Tier** (always-free ARM VM, no time
   limit, requires a card for verification but the always-free shapes are
   not billed).
2. Create an "Always Free" VM instance (Ubuntu).
3. SSH in, install Python, copy these files over.
4. Set the two environment variables (e.g. in `/etc/environment` or a
   `.env` loaded by a systemd unit):
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
5. Run it continuously:
   ```bash
   python3 notification_tracker.py --loop --interval 300
   ```
6. Use `systemd` (or `nohup` + `screen`/`tmux`) so it keeps running after you
   disconnect and restarts if the VM reboots.

This is more setup but gives you a real always-on process instead of relying
on GitHub's scheduler.

---

## 9. Quick troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No Telegram message at all, ever | Check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` secrets are set correctly; make sure you messaged your bot at least once before fetching `getUpdates`. |
| "First run - baseline saved" every time | `state.json` isn't being committed/persisted between runs - check the workflow's commit step succeeded (Actions log). |
| `--inspect` shows 0 items | Selector is wrong/outdated - see Section 5. |
| Repeated "⚠️ ... failed N times in a row" | Site may be blocking the GitHub Actions IP range specifically, or is down. Try running `--inspect` from your own machine/VM to compare. |
| Workflow doesn't run on schedule | Confirm the repo isn't archived/inactive (GitHub disables scheduled workflows after 60 days of repo inactivity - push any small commit to reset this). |
