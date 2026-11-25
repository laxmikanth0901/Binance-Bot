#!/usr/bin/env python3
"""Checks Binance every hour for new coin listings and notifies you.

Two sources are checked:
  1. Binance's "New Cryptocurrency Listing" announcements (early warning)
  2. exchangeInfo: new trading pairs that actually went live

Notification: Termux:API (termux-notification) if available, plus ntfy.sh
if you set NTFY_TOPIC below.

Usage:
  python binance_watch.py          # one check (use with cron)
  python binance_watch.py --loop   # check every hour, forever
"""
import json, os, subprocess, shutil, sys, time, urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # e.g. "my-binance-alerts-8f3k2" (install the ntfy app, subscribe to it)
INTERVAL = 3600          # seconds
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

ANN_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
           "?type=1&catalogId=48&pageNo=1&pageSize=15")
EXCHANGE_HOSTS = ["data-api.binance.vision", "api.binance.com", "api1.binance.com",
                  "api2.binance.com", "api3.binance.com", "api4.binance.com"]


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
    try:
        articles = get_json(ANN_URL)["data"]["catalogs"][0]["articles"]
        return {str(a["code"]): a["title"] for a in articles}
    except Exception as e:
        print("announcements failed:", e)
        return None


def fetch_symbols():
    for host in EXCHANGE_HOSTS:
        try:
            data = get_json(f"https://{host}/api/v3/exchangeInfo")
            print("pairs fetched from", host)
            return [s["symbol"] for s in data["symbols"] if s.get("status") == "TRADING"]
        except Exception as e:
            print(f"{host} failed: {e}")
    return None


def check():
    state = load_state() or {}
    ann, symbols = fetch_announcements(), fetch_symbols()
    if ann is None and symbols is None:
        print("Both sources failed.")
        sys.exit(1)

    new_state = dict(state)
    changed = False

    if ann is not None:
        old = state.get("ann")
        if old is not None:  # None means first run for this source: seed silently
            for code, title in ann.items():
                if code not in set(old):
                    notify("New Binance listing announced", title)
                    changed = True
        new_state["ann"] = list(set(old or []) | set(ann))

    if symbols is not None:
        old = state.get("symbols")
        if old is not None:
            new_pairs = [x for x in symbols if x not in set(old)]
            if new_pairs:
                notify("New Binance pairs live", ", ".join(new_pairs[:15]))
                changed = True
        new_state["symbols"] = list(set(old or []) | set(symbols))

    save_state(new_state)
    print(time.strftime("%H:%M"), "new items found" if changed else "checked, nothing new")


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
