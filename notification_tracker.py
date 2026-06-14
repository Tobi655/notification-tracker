#!/usr/bin/env python3
"""
Website Notification Tracker -> Telegram Bot
=============================================

Checks one or more "notification" sections (configured in sites_config.json)
for changes, and sends an alert to a Telegram chat when something new shows
up. Designed to be run every ~5 minutes (via GitHub Actions cron, a VM cron
job, or its own --loop mode).

Environment variables required:
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    TELEGRAM_CHAT_ID     - your chat id (from /getUpdates)

Usage:
    python notification_tracker.py                 # run one check cycle
    python notification_tracker.py --inspect        # debug: print what the
                                                      # configured selectors match
    python notification_tracker.py --loop            # run forever, every 5 min
    python notification_tracker.py --loop --interval 300
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "sites_config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "tracker.log")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

REQUEST_TIMEOUT = 25          # seconds per request attempt
MAX_RETRIES = 3               # attempts per fetch before giving up
RETRY_BACKOFF = 6             # seconds, multiplied by attempt number
FAILURE_ALERT_THRESHOLD = 3   # consecutive failed cycles before alerting user

# A "normal browser" header set. Many government / WAF-protected sites block
# requests that don't look like a real browser (missing or python-requests
# user-agent, missing Accept headers, etc.)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Phrases that indicate the response is a block/challenge page rather than
# the real content.
BLOCK_MARKERS = [
    "access denied",
    "are you a robot",
    "captcha",
    "cloudflare",
    "request blocked",
    "forbidden",
    "attention required",
    "checking your browser",
    "just a moment",
    "rate limit",
    "temporarily unavailable",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tracker")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class FetchBlockedError(Exception):
    """Raised when the site appears to be blocking / challenging the request."""


class SelectorNotFoundError(Exception):
    """Raised when the configured CSS selector matches nothing on the page."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def load_json(path, default):
    """Load JSON, tolerating a missing or corrupted file."""
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.warning("Corrupt JSON in %s (%s) - starting fresh", path, e)
        return default
    except OSError as e:
        log.warning("Could not read %s (%s) - using default", path, e)
        return default


def save_json(path, data):
    """Write JSON atomically so a crash mid-write can't corrupt the file."""
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError as e:
        log.error("Failed to save %s: %s", path, e)


def looks_blocked(text, status_code):
    """Heuristic check for bot-block / challenge pages."""
    if status_code in (401, 403, 429, 503):
        return True
    try:
        lower = text[:3000].lower()
    except Exception:
        return False
    return any(marker in lower for marker in BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# Fetching (with retries + anti-block fallbacks)
# ---------------------------------------------------------------------------
def fetch_with_requests(url):
    """Fetch a URL using plain requests, retrying on common failure modes."""
    last_exc = None
    session = requests.Session()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=REQUEST_TIMEOUT,
                verify=True,
                allow_redirects=True,
            )

            if looks_blocked(resp.text, resp.status_code):
                raise FetchBlockedError(
                    f"Blocked / challenge page (HTTP {resp.status_code})"
                )

            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or resp.encoding
            return resp.text

        # --- Certificate problems: some .gov.in / .nic.in hosts have
        # misconfigured intermediate certs. Retry once without verification
        # so the content can still be read; we still try verified first.
        except requests.exceptions.SSLError as e:
            log.warning("[%s] SSL error (%s) - retrying without cert verification", url, e)
            try:
                resp = session.get(
                    url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT, verify=False
                )
                if looks_blocked(resp.text, resp.status_code):
                    raise FetchBlockedError(
                        f"Blocked / challenge page (HTTP {resp.status_code})"
                    )
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or resp.encoding
                return resp.text
            except Exception as e2:
                last_exc = e2

        except requests.exceptions.ConnectTimeout as e:
            last_exc = e
            log.warning("[%s] Connect timeout (attempt %d/%d)", url, attempt, MAX_RETRIES)

        except requests.exceptions.ReadTimeout as e:
            last_exc = e
            log.warning("[%s] Read timeout (attempt %d/%d)", url, attempt, MAX_RETRIES)

        except requests.exceptions.ConnectionError as e:
            # DNS failure, connection refused, connection reset, etc.
            last_exc = e
            log.warning("[%s] Connection error (attempt %d/%d): %s", url, attempt, MAX_RETRIES, e)

        except requests.exceptions.TooManyRedirects as e:
            last_exc = e
            log.warning("[%s] Too many redirects: %s", url, e)
            break  # retrying will not help

        except requests.exceptions.HTTPError as e:
            last_exc = e
            log.warning("[%s] HTTP error (attempt %d/%d): %s", url, attempt, MAX_RETRIES, e)

        except FetchBlockedError as e:
            last_exc = e
            log.warning("[%s] %s (attempt %d/%d)", url, e, attempt, MAX_RETRIES)

        except requests.exceptions.RequestException as e:
            # Catch-all for anything else requests can raise
            last_exc = e
            log.warning("[%s] Request error (attempt %d/%d): %s", url, attempt, MAX_RETRIES, e)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)

    raise last_exc or FetchBlockedError("Unknown fetch failure")


def fetch_with_cloudscraper(url):
    """Fallback fetch using cloudscraper (handles many Cloudflare-style
    JS/anti-bot challenges that plain requests cannot)."""
    if not HAS_CLOUDSCRAPER:
        raise FetchBlockedError("cloudscraper is not installed")

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        if looks_blocked(resp.text, resp.status_code):
            raise FetchBlockedError(f"cloudscraper also blocked (HTTP {resp.status_code})")
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text
    except requests.exceptions.RequestException as e:
        raise FetchBlockedError(f"cloudscraper request failed: {e}")


def fetch_page(url):
    """Try plain requests first; fall back to cloudscraper if that fails."""
    try:
        return fetch_with_requests(url)
    except Exception as e:
        log.warning("[%s] Plain requests failed (%s) - trying cloudscraper fallback", url, e)
        try:
            return fetch_with_cloudscraper(url)
        except Exception as e2:
            log.error("[%s] All fetch methods failed: %s", url, e2)
            raise


# ---------------------------------------------------------------------------
# Parsing / extraction
# ---------------------------------------------------------------------------
def extract_items(html, selector, item_selector="a"):
    """
    Extract a list of {text, href} dicts from the section matching `selector`.

    If the selector matches nothing -> SelectorNotFoundError (page structure
    probably changed, or the selector was never correct - run with --inspect
    to debug).

    If the section is found but contains no matching `item_selector` tags,
    fall back to using the section's raw text so changes can still be
    detected.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        raise SelectorNotFoundError(f"Could not parse HTML: {e}")

    container = soup.select_one(selector)
    if container is None:
        raise SelectorNotFoundError(f"Selector '{selector}' matched nothing on the page")

    items = []
    for tag in container.select(item_selector):
        text = " ".join(tag.get_text(strip=True).split())
        href = tag.get("href", "") or ""
        if not text and not href:
            continue
        items.append({"text": text, "href": href})

    if not items:
        text = " ".join(container.get_text(strip=True).split())
        if not text:
            raise SelectorNotFoundError(
                f"Selector '{selector}' matched an empty element"
            )
        items = [{"text": text, "href": ""}]

    return items


def hash_items(items):
    payload = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    """Send a message to the configured Telegram chat, splitting long
    messages and retrying on transient errors."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - cannot send: %s", text[:200])
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # Telegram's hard limit is 4096 chars; stay comfortably under it.
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]

    all_ok = True
    for chunk in chunks:
        sent = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    sent = True
                    break

                log.error("Telegram API error %s: %s", resp.status_code, resp.text[:300])
                if resp.status_code in (400, 401, 403, 404):
                    # Bad token, bad chat id, or malformed request -
                    # retrying the same thing will not help.
                    break

            except requests.exceptions.RequestException as e:
                log.warning("Telegram send failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)

        if not sent:
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Core per-site check
# ---------------------------------------------------------------------------
def check_site(site, state):
    name = site["name"]
    url = site["url"]
    selector = site["selector"]
    item_selector = site.get("item_selector", "a")

    site_state = state.setdefault(
        name, {"hash": None, "items": [], "failures": 0, "blocked_alerted": False}
    )

    try:
        html = fetch_page(url)
        items = extract_items(html, selector, item_selector)

    except SelectorNotFoundError as e:
        log.error("[%s] %s", name, e)
        _handle_failure(
            site_state, name,
            f"Page structure may have changed - selector not found.\n{e}"
        )
        return

    except Exception as e:
        log.error("[%s] Fetch failed: %s", name, e)
        _handle_failure(
            site_state, name,
            f"Could not fetch the page after {MAX_RETRIES} retries "
            f"(with cloudscraper fallback).\n{type(e).__name__}: {e}"
        )
        return

    # --- success: reset failure tracking, notify if we were previously down
    if site_state["failures"] >= FAILURE_ALERT_THRESHOLD and site_state.get("blocked_alerted"):
        send_telegram_message(f"✅ {name}\nBack online - checks are succeeding again.")
    site_state["failures"] = 0
    site_state["blocked_alerted"] = False

    new_hash = hash_items(items)
    old_hash = site_state.get("hash")

    if old_hash is None:
        log.info("[%s] First run - baseline saved (%d item(s))", name, len(items))
        site_state["hash"] = new_hash
        site_state["items"] = items
        return

    if new_hash == old_hash:
        log.info("[%s] No change (%d item(s))", name, len(items))
        return

    old_items = site_state.get("items", [])
    old_keys = {(i["text"], i["href"]) for i in old_items}
    new_entries = [i for i in items if (i["text"], i["href"]) not in old_keys]

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🔔 Update detected: {name}", f"Time: {timestamp}", f"Page: {url}", ""]

    if new_entries:
        lines.append("New / changed entries:")
        for i in new_entries[:15]:
            line = f"• {i['text']}"
            if i["href"]:
                line += f"\n  {i['href']}"
            lines.append(line)
        if len(new_entries) > 15:
            lines.append(f"...and {len(new_entries) - 15} more")
    else:
        lines.append("The section's content changed (edit/reorder) - check the page.")

    send_telegram_message("\n".join(lines))
    log.info("[%s] Change detected, alert sent (%d new entr%s)",
             name, len(new_entries), "y" if len(new_entries) == 1 else "ies")

    site_state["hash"] = new_hash
    site_state["items"] = items


def _handle_failure(site_state, name, reason):
    site_state["failures"] = site_state.get("failures", 0) + 1
    log.warning("[%s] consecutive failure count = %d", name, site_state["failures"])

    if site_state["failures"] == FAILURE_ALERT_THRESHOLD and not site_state.get("blocked_alerted"):
        send_telegram_message(
            f"⚠️ {name}\n"
            f"The checker has failed {FAILURE_ALERT_THRESHOLD} times in a row.\n"
            f"{reason}\n"
            "It will keep retrying automatically and will notify you again "
            "when it recovers."
        )
        site_state["blocked_alerted"] = True


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------
def run_once():
    sites = load_json(CONFIG_FILE, [])
    if not sites:
        log.error("No sites configured in %s - nothing to do", CONFIG_FILE)
        return

    state = load_json(STATE_FILE, {})

    for site in sites:
        try:
            check_site(site, state)
        except Exception as e:
            # Last-resort catch-all so one bad site never kills the whole run
            log.exception("[%s] Unexpected error: %s", site.get("name", "?"), e)

    save_json(STATE_FILE, state)


def run_inspect():
    """Debug helper: show exactly what each configured selector currently
    matches, so selectors can be fixed before relying on them."""
    sites = load_json(CONFIG_FILE, [])
    if not sites:
        print(f"No sites configured in {CONFIG_FILE}")
        return

    for site in sites:
        print(f"\n=== {site['name']} ===")
        print(f"URL:      {site['url']}")
        print(f"Selector: {site['selector']}")
        try:
            html = fetch_page(site["url"])
            items = extract_items(html, site["selector"], site.get("item_selector", "a"))
            print(f"-> {len(items)} item(s) matched:\n")
            for i in items[:20]:
                print(f"  - {i['text'][:120]}  -> {i['href']}")
            if len(items) > 20:
                print(f"  ... and {len(items) - 20} more")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Website notification tracker -> Telegram")
    parser.add_argument("--inspect", action="store_true",
                         help="Print what the configured selectors currently match, then exit")
    parser.add_argument("--loop", action="store_true",
                         help="Run forever, checking every --interval seconds")
    parser.add_argument("--interval", type=int, default=300,
                         help="Seconds between checks in --loop mode (default 300 = 5 min)")
    args = parser.parse_args()

    if args.inspect:
        run_inspect()
        return

    if args.loop:
        log.info("Starting continuous loop (interval=%ds). Press Ctrl+C to stop.", args.interval)
        while True:
            try:
                run_once()
            except Exception:
                log.exception("Unexpected error in run_once()")
            time.sleep(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
