#!/usr/bin/env python3
"""Watches Binance for newly ANNOUNCED spot listings and keeps you posted until they trade.

Run it every hour (GitHub Actions cron). What it does each run:
  1. Reads Binance's "New Cryptocurrency Listing" announcements. A title containing
     "Will List" plus a bracketed ticker, e.g. "Binance Will List Foo (FOO) ...",
     puts that coin on a watchlist.
  2. Opens the announcement and reads the exact spot-trading start time.
  3. Notifies you:
       - right away when a new coin is announced (if it opens within WINDOW_DAYS)
       - a combined reminder every ~REMIND_EVERY_HOURS hours (only coins opening
         within WINDOW_DAYS, or whose time couldn't be read)
       - once, about an hour before trading opens (even if you were notified before)
       - when the coin is actually trading ("now LIVE")
  4. Drops a coin once it is live, or if it never opened (OVERDUE_DAYS after its
     stated time, or WINDOW_DAYS after the announcement if no time was found).

Notification: Termux:API (termux-notification) if available, plus ntfy.sh
if the NTFY_TOPIC environment variable is set.

Usage:
  python binance_watch.py          # one check (cron / GitHub Actions)
  python binance_watch.py --loop   # check every hour, forever
"""
import calendar, json, os, re, subprocess, shutil, sys, time, urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
INTERVAL = 3600              # seconds (only used with --loop)
WINDOW_DAYS = 10             # only notify about coins opening within this many days
REMIND_EVERY_HOURS = 6       # reminder cadence (checked hourly, sent every ~6h)
PRE_ALERT_MINUTES = 90       # "about an hour before" alert fires once inside this window
OVERDUE_DAYS = 2             # stop tracking a coin this long after its stated time
IST_OFFSET = 5.5 * 3600      # shown times are in India time (plus UTC)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

ANN_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
           "?type=1&catalogId=48&pageNo=1&pageSize=15")
DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={}"
PAGE_URL = "https://www.binance.com/en/support/announcement/detail/{}"
EXCHANGE_HOSTS = ["data-api.binance.vision", "api.binance.com", "api1.binance.com",
                  "api2.binance.com", "api3.binance.com", "api4.binance.com"]

LIST_PHRASE = "will list"
TICKER_RE = re.compile(r"\(([A-Z0-9]{2,20})\)")
OPEN_RE = re.compile(r"open\s+trading", re.I)
DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")


def now():
    return time.time()


def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url):
    return json.loads(get_text(url))


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


# ---------- fetching ----------

def fetch_announcements():
    """{code: title} (newest first), or None if the request failed."""
    try:
        articles = get_json(ANN_URL)["data"]["catalogs"][0]["articles"]
        return {str(a["code"]): a["title"] for a in articles}
    except Exception as e:
        print("announcements failed:", e)
        return None


def fetch_live_assets():
    """Set of coins with a TRADING spot pair, or None if every host failed."""
    for host in EXCHANGE_HOSTS:
        try:
            data = get_json(f"https://{host}/api/v3/exchangeInfo")
            print("pairs fetched from", host)
            return {s["baseAsset"] for s in data["symbols"] if s.get("status") == "TRADING"}
        except Exception as e:
            print(f"{host} failed: {e}")
    return None


def _flatten(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        for v in node.values():
            if isinstance(v, (dict, list)):
                _flatten(v, out)
    elif isinstance(node, list):
        for v in node:
            _flatten(v, out)


def fetch_article_text(code):
    """Plain text of an announcement, or None. Tries the data endpoint, then the web page."""
    try:
        body = get_json(DETAIL_URL.format(code))["data"]["body"]
        try:
            out = []
            _flatten(json.loads(body), out)
            return "".join(out)
        except Exception:
            return re.sub(r"<[^>]+>", " ", str(body))
    except Exception as e:
        print(f"article {code} (api) failed: {e}")
    try:
        return get_text(PAGE_URL.format(code))
    except Exception as e:
        print(f"article {code} (page) failed: {e}")
        return None


# ---------- parsing ----------

def listing_tickers(title):
    """Tickers of coins this announcement lists ([] if it isn't a spot listing)."""
    if LIST_PHRASE not in title.lower():
        return []
    out = []
    for t in TICKER_RE.findall(title):
        if any(c.isalpha() for c in t) and t not in out:
            out.append(t)
    return out


def parse_release(text):
    """Epoch seconds (UTC) of the first date-time after 'open trading', or None."""
    for m in OPEN_RE.finditer(text):
        d = DT_RE.search(text, m.end(), m.end() + 600)
        if d:
            return calendar.timegm(time.strptime(f"{d.group(1)} {d.group(2)}", "%Y-%m-%d %H:%M"))
    return None


def resolve_release(e, title_to_code):
    """Fill in e['release'] by reading the announcement (retried each run until found)."""
    if e.get("release") is not None:
        return
    code = e.get("code") or title_to_code.get(e["title"])
    if not code:
        return
    e["code"] = code
    text = fetch_article_text(code)
    if text:
        r = parse_release(text)
        if r:
            e["release"] = r
        else:
            print(f"{code}: no trading time found in the article")


# ---------- formatting ----------

def fmt_time(epoch):
    return (time.strftime("%d %b %H:%M", time.gmtime(epoch + IST_OFFSET)) + " IST ("
            + time.strftime("%H:%M", time.gmtime(epoch)) + " UTC)")


def fmt_delta(sec):
    d, rem = divmod(int(abs(sec)) // 60, 1440)
    h, m = divmod(rem, 60)
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def when(e):
    r = e.get("release")
    if r is None:
        return "release time not found in the announcement"
    diff = r - now()
    if diff > 0:
        return f"opens {fmt_time(r)}, in {fmt_delta(diff)}"
    return f"was due {fmt_time(r)} ({fmt_delta(diff)} ago), not trading yet"


def line(t, e):
    return f"{t}: {when(e)}\n{e['title']}"


def in_window(e):
    r = e.get("release")
    return r is None or r - now() <= WINDOW_DAYS * 86400


def today_utc():
    return time.strftime("%Y-%m-%d", time.gmtime(now()))


def days_since(date_str):
    return int((now() - calendar.timegm(time.strptime(date_str, "%Y-%m-%d"))) // 86400)


# ---------- the run ----------

def check():
    state = load_state() or {}
    first = "pending" not in state          # first run of this version: seed the watchlist
    seen = set(state.get("ann") or [])
    pending = dict(state.get("pending") or {})
    last_reminder = state.get("last_reminder", 0)
    t_now = now()

    ann = fetch_announcements()
    live = fetch_live_assets()
    if ann is None and live is None:
        print("Both sources failed.")
        sys.exit(1)

    title_to_code = {title: code for code, title in (ann or {}).items()}

    # 1) announcements -> watchlist
    fresh = []                              # coins newly announced this run
    if ann is not None:
        for code, title in ann.items():
            tickers = listing_tickers(title)
            is_new = code not in seen
            if tickers and (is_new or first):
                for t in tickers:
                    if t in pending:
                        continue
                    if live and t in live:
                        if is_new and not first:    # announced and already trading: don't stay silent
                            notify(f"{t} is now LIVE on Binance",
                                   f"{t} was just announced and is already trading on spot.\n{title}")
                        continue
                    pending[t] = {"title": title, "code": code, "release": None,
                                  "pre": False, "since": today_utc()}
                    if is_new and not first:
                        fresh.append(t)
        seen |= set(ann)

    # 2) read exact trading times from the announcements
    for e in pending.values():
        resolve_release(e, title_to_code)

    # 3) coins that are now trading
    if live is not None:
        for t in list(pending):
            if t in live:
                notify(f"{t} is now LIVE on Binance",
                       f"{t} is trading on spot now.\n{pending[t]['title']}")
                del pending[t]
    fresh = [t for t in fresh if t in pending]

    # 4) coins that never opened
    for t in list(pending):
        e = pending[t]
        r = e.get("release")
        overdue = (t_now > r + OVERDUE_DAYS * 86400) if r is not None \
            else days_since(e["since"]) > WINDOW_DAYS
        if overdue:
            notify(f"Stopped tracking {t}",
                   f"{t} still isn't trading and the window has passed.\n{e['title']}")
            del pending[t]
    fresh = [t for t in fresh if t in pending]

    alerted = set()                         # coins already notified this run

    # 5) brand-new announcements (only if opening within the window)
    for t in fresh:
        e = pending[t]
        if in_window(e):
            notify("New Binance coin announced", line(t, e))
            alerted.add(t)
            last_reminder = t_now           # this counts as a notice; next reminder in ~6h
            r = e.get("release")
            if r is not None and 0 < r - t_now <= PRE_ALERT_MINUTES * 60:
                e["pre"] = True

    # 6) about an hour before trading opens (once per coin)
    for t, e in pending.items():
        r = e.get("release")
        if t in alerted or e.get("pre") or r is None:
            continue
        if 0 < r - t_now <= PRE_ALERT_MINUTES * 60:
            notify(f"{t} opens in about {fmt_delta(r - t_now)}", line(t, e))
            e["pre"] = True
            alerted.add(t)
            last_reminder = t_now           # counts as a notice; next reminder in ~6h

    # 7) periodic reminder (one combined message)
    if t_now - last_reminder >= REMIND_EVERY_HOURS * 3600 - 1800:
        waiting = [t for t, e in pending.items() if t not in alerted and in_window(e)]
        if waiting:
            notify("Upcoming Binance listings",
                   "\n\n".join(line(t, pending[t]) for t in waiting))
            last_reminder = t_now

    new_state = {"ann": sorted(seen), "last_reminder": last_reminder}
    if not (first and ann is None):         # don't mark seeded if we never saw the feed
        new_state["pending"] = pending
    save_state(new_state)
    print(time.strftime("%H:%M"), "pending:",
          {t: (fmt_time(e["release"]) if e.get("release") else "time unknown")
           for t, e in pending.items()} or "none")


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
