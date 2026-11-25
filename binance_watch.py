#!/usr/bin/env python3
"""Watches Binance for newly ANNOUNCED coins and nags you until they are live.

How it works:
  1. Reads Binance's "New Cryptocurrency Listing" announcements.
  2. A title containing "Will List" with a ticker in brackets, e.g.
     "Binance Will List Foo (FOO) ...", puts that coin on a watchlist ("pending").
  3. Every run, each coin still on the watchlist gets a reminder.
  4. Once the coin has a live TRADING pair on Binance, you get a "now live"
     alert and it is removed from the watchlist.
  5. A coin still not trading after MAX_PENDING_DAYS is dropped (with a notice),
     so a cancelled listing can't nag you forever.

Notification: Termux:API (termux-notification) if available, plus ntfy.sh
if the NTFY_TOPIC environment variable is set.

Usage:
  python binance_watch.py          # one check (use with cron / GitHub Actions)
  python binance_watch.py --loop   # check every hour, forever
"""
import json, os, re, subprocess, shutil, sys, time, urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
INTERVAL = 3600            # seconds (only used with --loop)
MAX_PENDING_DAYS = 10      # stop reminding after this many days
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

ANN_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
           "?type=1&catalogId=48&pageNo=1&pageSize=15")
EXCHANGE_HOSTS = ["data-api.binance.vision", "api.binance.com", "api1.binance.com",
                  "api2.binance.com", "api3.binance.com", "api4.binance.com"]

LIST_PHRASE = "will list"                       # what a spot-listing title contains
TICKER_RE = re.compile(r"\(([A-Z0-9]{2,20})\)")  # ticker in brackets, e.g. (FOO)


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def notify(title, msg):
    print(f"[NOTIFY] {title}: {msg}")
    if shutil.which("termux-notification"):
        subprocess.run(["termux-notification", "--title", title, "--content", msg])
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode(),
                headers={"Title": title, "Priority": "high"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("ntfy failed:", e)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def fetch_announcements():
    """Returns {code: title} (newest first), or None if the request failed."""
    try:
        articles = get_json(ANN_URL)["data"]["catalogs"][0]["articles"]
        return {str(a["code"]): a["title"] for a in articles}
    except Exception as e:
        print("announcements failed:", e)
        return None


def fetch_live_assets():
    """Returns the set of coins that have a TRADING pair, or None if all hosts failed."""
    for host in EXCHANGE_HOSTS:
        try:
            data = get_json(f"https://{host}/api/v3/exchangeInfo")
            print("pairs fetched from", host)
            return {s["baseAsset"] for s in data["symbols"] if s.get("status") == "TRADING"}
        except Exception as e:
            print(f"{host} failed: {e}")
    return None


def listing_tickers(title):
    """Tickers of coins being listed by this announcement ([] if it isn't a listing)."""
    if LIST_PHRASE not in title.lower():
        return []
    out = []
    for t in TICKER_RE.findall(title):
        if any(c.isalpha() for c in t) and t not in out:
            out.append(t)
    return out


def days_since(date_str):
    t = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
    return int((time.time() - t) // 86400)


def check():
    state = load_state() or {}
    first = "pending" not in state          # first run of this version: seed the watchlist
    seen = set(state.get("ann") or [])
    pending = dict(state.get("pending") or {})

    ann = fetch_announcements()
    live = fetch_live_assets()
    if ann is None and live is None:
        print("Both sources failed.")
        sys.exit(1)

    # 1) announcements -> watchlist
    fresh = set()                           # coins added this run (already alerted)
    if ann is not None:
        for code, title in ann.items():
            tickers = listing_tickers(title)
            is_new = code not in seen
            if tickers and (is_new or first):
                added = [t for t in tickers
                         if t not in pending and not (live and t in live)]
                for t in added:
                    pending[t] = {"title": title, "since": time.strftime("%Y-%m-%d")}
                if is_new and not first and added:
                    notify("New Binance coin announced", title)
                    fresh.update(added)
        seen |= set(ann)

    # 2) watchlist -> "now live" alerts
    if live is not None:
        for t in list(pending):
            if t in live:
                notify(f"{t} is now LIVE on Binance",
                       f"{t} is trading now. Announcement: {pending[t]['title']}")
                del pending[t]

    # 3) watchlist -> reminders (and expiry)
    waiting = []
    for t in list(pending):
        if days_since(pending[t]["since"]) > MAX_PENDING_DAYS:
            notify(f"Stopped tracking {t}",
                   f"{t} still isn't trading after {MAX_PENDING_DAYS} days. "
                   f"Announcement: {pending[t]['title']}")
            del pending[t]
        elif t not in fresh:
            waiting.append(t)
    if waiting:
        notify("Announced, not trading yet",
               "\n".join(f"{t}: {pending[t]['title']}" for t in waiting))

    new_state = {"ann": sorted(seen)}
    if not (first and ann is None):         # don't mark seeded if we never saw the feed
        new_state["pending"] = pending
    save_state(new_state)
    print(time.strftime("%H:%M"), "pending:", sorted(pending) or "none")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            try:
                check()
            except Exception as e:
                print("check failed:", e)
            time.sleep(INTERVAL)
    else:
        check()
