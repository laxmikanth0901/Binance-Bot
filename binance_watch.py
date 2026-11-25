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
EXCHANGE_URL = "https://api.binance.com/api/v3/exchangeInfo"


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


def fetch_current():
    articles = get_json(ANN_URL)["data"]["catalogs"][0]["articles"]
    ann = {str(a["code"]): a["title"] for a in articles}
    symbols = [s["symbol"] for s in get_json(EXCHANGE_URL)["symbols"]
               if s.get("status") == "TRADING"]
    return ann, symbols


def check():
    state = load_state()
    ann, symbols = fetch_current()

    if state is None:  # first run: remember everything, don't spam
        save_state({"ann": list(ann), "symbols": symbols})
        print(f"First run: stored {len(ann)} announcements and {len(symbols)} pairs.")
        return

    seen_ann, seen_sym = set(state["ann"]), set(state["symbols"])
    for code, title in ann.items():
        if code not in seen_ann:
            notify("New Binance listing announced", title)
    new_pairs = [s for s in symbols if s not in seen_sym]
    if new_pairs:
        notify("New Binance pairs live", ", ".join(new_pairs[:15]))

    save_state({"ann": list(seen_ann | set(ann)), "symbols": list(seen_sym | set(symbols))})
    print(time.strftime("%H:%M"), "checked, nothing new" if not new_pairs else "new pairs found")


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
