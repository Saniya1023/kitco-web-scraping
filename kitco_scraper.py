#!/usr/bin/env python3
"""
Kitco "World Spot Price" scraper -> JSON (+ optional SQLite storage).

Strategy (fastest first, each falls through to the next):
  1. Plain HTTP GET + parse the server-rendered page text   (~100-300 ms)
  2. Parse embedded __NEXT_DATA__ JSON (if the page ships it)
  3. Headless browser (Playwright) for fully client-rendered content

Install:
    pip install requests selectolax
    pip install playwright && playwright install chromium   # optional fallback

Usage:
    python kitco_scraper.py                         # print JSON
    python kitco_scraper.py -o prices.json          # write JSON file
    python kitco_scraper.py --db prices.db          # upsert into SQLite
    python kitco_scraper.py --db prices.db -i 60    # poll every 60 seconds
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from urllib3.util.retry import Retry

URL = "https://www.kitco.com/price/precious-metals"
SOURCE_TZ = ZoneInfo("America/New_York")  # page says "All times EST"
log = logging.getLogger("kitco")

# One shared session = connection reuse (keep-alive) + automatic retries.
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
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
    }
)

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
NUM = r"-?\d{1,3}(?:,\d{3})*\.\d{2}"
ROW_RE = re.compile(
    r"(?P<metal>GOLD|SILVER|PLATINUM|PALLADIUM|RHODIUM)\s*"
    r"(?P<date>[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})\s*"
    r"(?P<time>\d{1,2}:\d{2})\s*"
    rf"(?P<bid>{NUM})\s*(?P<ask>{NUM})\s*"
    r"(?:(?P<chg>-?\d+\.\d{3})\s*)?"
    r"\(?(?P<pct>-?\d+\.\d{2})%\)?\s*"
    rf"(?P<low>{NUM})\s*(?P<high>{NUM})"
)


def _f(s: str | None) -> float | None:
    return float(s.replace(",", "")) if s else None


def _to_iso_utc(date_s: str, time_s: str) -> str | None:
    try:
        clean = re.sub(r"\s+", " ", date_s)
        dt = datetime.strptime(f"{clean} {time_s}", "%b %d, %Y %H:%M")
        return dt.replace(tzinfo=SOURCE_TZ).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def parse_text(html: str) -> list[dict]:
    """Strategy 1: structure-agnostic. Resilient to class-name/layout changes."""
    tree = HTMLParser(html)
    for n in tree.css("script,style,noscript"):
        n.decompose()
    text = tree.body.text(separator=" ") if tree.body else tree.text(separator=" ")

    rows: dict[str, dict] = {}
    for m in ROW_RE.finditer(text):
        metal = m["metal"].lower()
        if metal in rows:  # page repeats the table (World + New York): keep first
            continue
        rows[metal] = {
            "metal": metal,
            "quote_time_utc": _to_iso_utc(m["date"], m["time"]),
            "quote_time_source": f"{m['date']} {m['time']} EST",
            "bid": _f(m["bid"]),
            "ask": _f(m["ask"]),
            "change": _f(m["chg"]),
            "change_pct": _f(m["pct"]),
            "low": _f(m["low"]),
            "high": _f(m["high"]),
        }
    return list(rows.values())


def parse_next_data(html: str) -> list[dict]:
    """Strategy 2: walk __NEXT_DATA__ for objects that look like quotes."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    wanted = {"gold", "silver", "platinum", "palladium", "rhodium"}
    found: dict[str, dict] = {}

    def walk(o):
        if isinstance(o, dict):
            name = str(o.get("name") or o.get("symbol") or o.get("metal") or "")
            key = name.lower().replace("xau", "gold").replace("xag", "silver")
            key = next((w for w in wanted if w in key), None)
            if key and "bid" in o and "ask" in o and key not in found:
                found[key] = {
                    "metal": key,
                    "quote_time_utc": o.get("timestamp") or o.get("ts"),
                    "quote_time_source": None,
                    "bid": _f(str(o["bid"])),
                    "ask": _f(str(o["ask"])),
                    "change": _f(str(o.get("change"))) if o.get("change") is not None else None,
                    "change_pct": _f(str(o.get("changePercentage") or o.get("changePct")))
                    if (o.get("changePercentage") or o.get("changePct")) is not None else None,
                    "low": _f(str(o.get("low"))) if o.get("low") is not None else None,
                    "high": _f(str(o.get("high"))) if o.get("high") is not None else None,
                }
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return list(found.values())


def fetch_html_http() -> str:
    r = _session.get(URL, timeout=(5, 15))
    r.raise_for_status()
    return r.text


def fetch_html_browser() -> str:
    """Strategy 3: only used if the cheap paths return nothing."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # Skip heavy assets -> much faster render.
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_(),
        )
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_function(
            "document.body.innerText.includes('GOLD') && /\\d,\\d{3}\\.\\d{2}/.test(document.body.innerText)",
            timeout=20_000,
        )
        html = page.content()
        browser.close()
        return html


def scrape() -> dict:
    rows: list[dict] = []
    method = "http-text"
    try:
        html = fetch_html_http()
        rows = parse_text(html)
        if len(rows) < 4:
            method = "next-data"
            rows = parse_next_data(html) or rows
    except requests.RequestException as e:
        log.warning("HTTP fetch failed: %s", e)

    if len(rows) < 4:
        method = "browser"
        log.info("Falling back to headless browser")
        rows = parse_text(fetch_html_browser())

    if not rows:
        raise RuntimeError("No spot-price rows found; page layout may have changed")

    return {
        "source": URL,
        "table": "World Spot Price",
        "currency": "USD",
        "unit": "troy oz",
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "data": rows,
    }


# --------------------------------------------------------------------------- #
# Storage (SQLite shown; swap for Postgres/MySQL with the same row shape)
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_prices (
    metal          TEXT NOT NULL,
    quote_time_utc TEXT NOT NULL,
    bid REAL, ask REAL, change REAL, change_pct REAL, low REAL, high REAL,
    scraped_at_utc TEXT NOT NULL,
    PRIMARY KEY (metal, quote_time_utc)
) WITHOUT ROWID;
"""


def save_sqlite(path: str, payload: dict) -> int:
    rows = [
        (
            r["metal"], r["quote_time_utc"] or payload["scraped_at_utc"],
            r["bid"], r["ask"], r["change"], r["change_pct"], r["low"], r["high"],
            payload["scraped_at_utc"],
        )
        for r in payload["data"]
    ]
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # Idempotent: same (metal, quote_time) just refreshes the values.
        cur = con.executemany(
            """INSERT INTO spot_prices VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(metal, quote_time_utc) DO UPDATE SET
                 bid=excluded.bid, ask=excluded.ask, change=excluded.change,
                 change_pct=excluded.change_pct, low=excluded.low,
                 high=excluded.high, scraped_at_utc=excluded.scraped_at_utc""",
            rows,
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-o", "--out", help="write JSON to this file")
    ap.add_argument("--db", help="SQLite file to upsert into")
    ap.add_argument("-i", "--interval", type=int, default=0, help="poll every N seconds (0 = run once)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    while True:
        try:
            payload = scrape()
            js = json.dumps(payload, indent=2)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(js)
            if args.db:
                log.info("Upserted %d rows", save_sqlite(args.db, payload))
            if not args.out and not args.db:
                print(js)
        except Exception as e:  # keep the poll loop alive
            log.error("Scrape failed: %s", e)
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())