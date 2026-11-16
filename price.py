#!/usr/bin/env python3
"""
Kitco spot-price scraper -> prices PER GRAM -> JSON (+ SQLite).

Supports both pages:
    https://www.kitco.com/price                    (card layout, "Updated 09:01:00 AM")
    https://www.kitco.com/price/precious-metals    (table layout, "Nov 15, 2026 21:26")

Speed notes:
  * requests.Session      -> keep-alive, retries, gzip
  * no HTML parser        -> two precompiled regexes strip tags (much faster than bs4/lxml)
  * early exit            -> stops at the first page that yields all 5 metals
  * headless browser      -> last-resort fallback only
  * executemany upsert    -> one DB round-trip, no duplicates

Install:
    pip install requests
    pip install playwright && playwright install chromium   # optional fallback

Usage:
    python kitco_scraper.py                         # print JSON (per gram)
    python kitco_scraper.py -o prices.json
    python kitco_scraper.py --db prices.db -i 60    # poll every 60 s
    python kitco_scraper.py --source metals         # force /price/precious-metals
    python kitco_scraper.py --tz America/New_York   # if /price times turn out to be ET
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCES = {
    "price": "https://www.kitco.com/price",
    "metals": "https://www.kitco.com/price/precious-metals",
}
# Default timezone of the timestamps printed on each page.
# /price: server-rendered card time looked like UTC (09:01 while the ET table said 04:01).
# /precious-metals: page says "All times EST". Override with --tz if needed.
DEFAULT_TZ = {"price": "UTC", "metals": "America/New_York"}

GRAMS_PER_TROY_OZ = 31.1034768
MIN_ROWS = 5  # gold, silver, platinum, palladium, rhodium
log = logging.getLogger("kitco")

# --------------------------------------------------------------------------- #
# HTTP session (created once, reused)
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
    ),
)
_session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
)

# --------------------------------------------------------------------------- #
# Regexes (compiled once)
# --------------------------------------------------------------------------- #
NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?"

# /price card, after tag-stripping:
#   Gold Updated 09:01:00 AM $ 3,858.60 Bid -24.80 ▼ 0.64% ... $ 3,860.60 Ask Low $ 3,832.20 High $ 3,902.30
CARD_RE = re.compile(
    r"(?P<metal>Gold|Silver|Platinum|Palladium|Rhodium)\s*Updated\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp][Mm])\s*"
    rf"\$?\s*(?P<bid>{NUM})\s*Bid\s*"
    rf"(?P<chg>[+\-−]?\s*{NUM})\s*(?P<arrow>[▲▼])?\s*(?P<pct>{NUM})\s*%"
    rf".{{0,200}}?\$?\s*(?P<ask>{NUM})\s*Ask\s*Low\s*\$?\s*(?P<low>{NUM})\s*High\s*\$?\s*(?P<high>{NUM})",
    re.S,
)

# /price/precious-metals table:
#   GOLD Nov 15, 2026 21:26 3,883.70 3,885.70 0.300 0.01% 3,859.90 3,902.30
LEGACY_NUM = r"-?\d{1,3}(?:,\d{3})*\.\d{2}"
LEGACY_RE = re.compile(
    r"(?P<metal>GOLD|SILVER|PLATINUM|PALLADIUM|RHODIUM)\s*"
    r"(?P<date>[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})\s*"
    r"(?P<time>\d{1,2}:\d{2})\s*"
    rf"(?P<bid>{LEGACY_NUM})\s*(?P<ask>{LEGACY_NUM})\s*"
    r"(?:(?P<chg>-?\d+\.\d{3})\s*)?"
    r"\(?(?P<pct>-?\d+\.\d{2})%\)?\s*"
    rf"(?P<low>{LEGACY_NUM})\s*(?P<high>{LEGACY_NUM})"
)

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _num(s: str | None) -> float | None:
    if not s:
        return None
    return float(re.sub(r"[,\s]", "", s).replace("−", "-"))


def _per_gram(v: float | None) -> float | None:
    """price per troy ounce -> price per gram"""
    return None if v is None else round(v / GRAMS_PER_TROY_OZ, 4)


def _html_to_text(html: str) -> str:
    return htmllib.unescape(_TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html)))


def _clock_to_utc(clock: str, tz: ZoneInfo, now: datetime) -> str | None:
    """'09:01:00 AM' (no date on page) -> today's date in `tz`, rolled back if in the future."""
    clock = _SPACES_RE.sub(" ", clock.strip().upper()).replace("AM", " AM").replace("PM", " PM")
    clock = _SPACES_RE.sub(" ", clock)
    for fmt in ("%I:%M:%S %p", "%I:%M %p"):
        try:
            t = datetime.strptime(clock, fmt).time()
            break
        except ValueError:
            continue
    else:
        return None
    dt = datetime.combine(now.astimezone(tz).date(), t, tzinfo=tz).astimezone(timezone.utc)
    if dt > now + timedelta(minutes=2):  # quote from "yesterday"
        dt -= timedelta(days=1)
    return dt.isoformat()


def _date_clock_to_utc(date_s: str, time_s: str, tz: ZoneInfo) -> str | None:
    try:
        dt = datetime.strptime(f"{_SPACES_RE.sub(' ', date_s)} {time_s}", "%b %d, %Y %H:%M")
        return dt.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _row(metal, quote_utc, quote_src, bid, ask, chg, pct, low, high) -> dict:
    return {
        "metal": metal.lower(),
        "quote_time_utc": quote_utc,
        "quote_time_source": quote_src,
        # per gram (USD)
        "bid_usd_per_gram": _per_gram(bid),
        "ask_usd_per_gram": _per_gram(ask),
        "change_usd_per_gram": _per_gram(chg),
        "change_percent": pct,
        "low_usd_per_gram": _per_gram(low),
        "high_usd_per_gram": _per_gram(high),
        # original Kitco quote per troy ounce (USD), for checking
        "bid_usd_per_oz": bid,
        "ask_usd_per_oz": ask,
    }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_html(html: str, tz: ZoneInfo, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    text = _html_to_text(html)
    rows: dict[str, dict] = {}

    for m in CARD_RE.finditer(text):
        metal = m["metal"].lower()
        if metal in rows:  # first occurrence = precious-metals section
            continue
        chg = _num(m["chg"])
        pct = _num(m["pct"])
        if chg is not None and pct is not None and (chg < 0 or (chg == 0 and m["arrow"] == "▼")):
            pct = -abs(pct)
        rows[metal] = _row(
            metal, _clock_to_utc(m["time"], tz, now), f"{m['time'].strip()} {tz.key}",
            _num(m["bid"]), _num(m["ask"]), chg, pct, _num(m["low"]), _num(m["high"]),
        )
        if len(rows) == MIN_ROWS:
            break

    if len(rows) < MIN_ROWS:  # try the table layout
        for m in LEGACY_RE.finditer(text):
            metal = m["metal"].lower()
            if metal in rows:
                continue
            rows[metal] = _row(
                metal, _date_clock_to_utc(m["date"], m["time"], tz),
                f"{m['date']} {m['time']} {tz.key}",
                _num(m["bid"]), _num(m["ask"]), _num(m["chg"]), _num(m["pct"]),
                _num(m["low"]), _num(m["high"]),
            )
    return list(rows.values())


def validate(rows: list[dict]) -> list[dict]:
    good = []
    for r in rows:
        bid, ask = r["bid_usd_per_gram"], r["ask_usd_per_gram"]
        if bid is None or ask is None or bid <= 0 or bid > ask:
            log.warning("Dropping invalid row: %s", r)
            continue
        good.append(r)
    return good


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_html_http(url: str) -> str:
    r = _session.get(url, timeout=(5, 15))
    r.raise_for_status()
    return r.text


def fetch_html_browser(url: str) -> str:
    """Fallback only: headless browser with heavy assets blocked."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_(),
        )
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_function(
            "/(Gold|GOLD)/.test(document.body.innerText) "
            "&& /\\d,\\d{3}\\.\\d{2}/.test(document.body.innerText)",
            timeout=20_000,
        )
        html = page.content()
        browser.close()
        return html


def scrape(source: str = "auto", tz_override: str | None = None) -> dict:
    keys = ["price", "metals"] if source == "auto" else [source]
    rows: list[dict] = []
    used, method = keys[0], "http"

    for key in keys:  # plain HTTP first, stop at the first page that gives all metals
        tz = ZoneInfo(tz_override or DEFAULT_TZ[key])
        try:
            rows = validate(parse_html(fetch_html_http(SOURCES[key]), tz))
        except requests.RequestException as e:
            log.warning("HTTP fetch failed for %s: %s", key, e)
            continue
        used = key
        if len(rows) >= MIN_ROWS:
            break

    if len(rows) < MIN_ROWS:
        method, used = "browser", keys[0]
        log.info("Only %d rows via HTTP; falling back to headless browser", len(rows))
        tz = ZoneInfo(tz_override or DEFAULT_TZ[used])
        rows = validate(parse_html(fetch_html_browser(SOURCES[used]), tz))

    if not rows:
        raise RuntimeError("No spot-price rows found; page layout may have changed")

    return {
        "source": SOURCES[used],
        "currency": "USD",
        "unit": "gram",
        "grams_per_troy_oz": GRAMS_PER_TROY_OZ,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "data": rows,
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_prices_gram (
    metal          TEXT NOT NULL,
    quote_time_utc TEXT NOT NULL,
    bid_usd_per_gram REAL, ask_usd_per_gram REAL, change_usd_per_gram REAL,
    change_percent REAL, low_usd_per_gram REAL, high_usd_per_gram REAL,
    scraped_at_utc TEXT NOT NULL,
    PRIMARY KEY (metal, quote_time_utc)
) WITHOUT ROWID;
"""


def save_sqlite(path: str, payload: dict) -> int:
    rows = [
        (
            r["metal"], r["quote_time_utc"] or payload["scraped_at_utc"],
            r["bid_usd_per_gram"], r["ask_usd_per_gram"], r["change_usd_per_gram"],
            r["change_percent"], r["low_usd_per_gram"], r["high_usd_per_gram"],
            payload["scraped_at_utc"],
        )
        for r in payload["data"]
    ]
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        cur = con.executemany(
            """INSERT INTO spot_prices_gram VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(metal, quote_time_utc) DO UPDATE SET
                 bid_usd_per_gram=excluded.bid_usd_per_gram,
                 ask_usd_per_gram=excluded.ask_usd_per_gram,
                 change_usd_per_gram=excluded.change_usd_per_gram,
                 change_percent=excluded.change_percent,
                 low_usd_per_gram=excluded.low_usd_per_gram,
                 high_usd_per_gram=excluded.high_usd_per_gram,
                 scraped_at_utc=excluded.scraped_at_utc""",
            rows,
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-o", "--out", help="write JSON to this file")
    ap.add_argument("--db", help="SQLite file to upsert into")
    ap.add_argument("-i", "--interval", type=int, default=0, help="poll every N seconds (0 = once)")
    ap.add_argument("--source", choices=["auto", "price", "metals"], default="auto",
                    help="auto = /price first, then /price/precious-metals")
    ap.add_argument("--tz", help="timezone of the times printed on the page (e.g. America/New_York)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.interval and args.interval < 30:
        ap.error("--interval must be at least 30 seconds (be polite to the site)")

    while True:
        try:
            payload = scrape(args.source, args.tz)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
            if args.db:
                log.info("Upserted %d rows", save_sqlite(args.db, payload))
            if not args.out and not args.db:
                print(json.dumps(payload, indent=2))
        except Exception as e:  # one bad poll must not kill the loop
            log.error("Scrape failed: %s", e)
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())