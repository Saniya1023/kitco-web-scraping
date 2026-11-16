#!/usr/bin/env python3
"""
Kitco "World Spot Price" scraper (BeautifulSoup version) -> JSON (+ SQLite).

Optimisations used:
  * requests.Session  -> connection reuse (keep-alive), retries, gzip
  * lxml parser       -> 3-5x faster than Python's built-in html.parser
  * HTML slicing      -> parse only the "World Spot Price" section, not the whole page
  * precompiled regex -> compiled once, reused on every run
  * executemany upsert-> one DB round-trip for all rows, no duplicates

Install:
    pip install requests beautifulsoup4 lxml
    pip install playwright && playwright install chromium   # optional fallback

Usage:
    python kitco_scraper_bs4.py                       # print JSON
    python kitco_scraper_bs4.py -o prices.json        # write JSON file
    python kitco_scraper_bs4.py --db prices.db        # upsert into SQLite
    python kitco_scraper_bs4.py --db prices.db -i 60  # poll every 60 s
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
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://www.kitco.com/price/precious-metals"
SOURCE_TZ = ZoneInfo("America/New_York")  # page says "All times EST"
MIN_ROWS = 5                              # gold, silver, platinum, palladium, rhodium
log = logging.getLogger("kitco")

# --------------------------------------------------------------------------- #
# HTTP session (created once, reused for every request)
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
# Regex for one table row, compiled once
# Example text:  GOLD Nov 15, 2026 21:26 3,883.70 3,885.70 0.300 0.01% 3,859.90 3,902.30
# --------------------------------------------------------------------------- #
NUM = r"-?\d{1,3}(?:,\d{3})*\.\d{2}"
ROW_RE = re.compile(
    r"(?P<metal>GOLD|SILVER|PLATINUM|PALLADIUM|RHODIUM)\s*"
    r"(?P<date>[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})\s*"
    r"(?P<time>\d{1,2}:\d{2})\s*"
    rf"(?P<bid>{NUM})\s*(?P<ask>{NUM})\s*"
    r"(?:(?P<chg>-?\d+\.\d{3})\s*)?"          # optional: rhodium has no absolute change
    r"\(?(?P<pct>-?\d+\.\d{2})%\)?\s*"
    rf"(?P<low>{NUM})\s*(?P<high>{NUM})"
)
SPACES_RE = re.compile(r"\s+")


def _num(s: str | None) -> float | None:
    """'1,473.00' -> 1473.0 ; None -> None"""
    return float(s.replace(",", "")) if s else None


def _to_iso_utc(date_s: str, time_s: str) -> str | None:
    """'Nov 15, 2026' + '21:26' (New York time) -> '2026-11-16T02:26:00+00:00'"""
    try:
        dt = datetime.strptime(f"{SPACES_RE.sub(' ', date_s)} {time_s}", "%b %d, %Y %H:%M")
        return dt.replace(tzinfo=SOURCE_TZ).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _slice_world_section(html: str) -> str:

    start = html.find("World Spot Price")
    if start == -1:
        return html
    end = html.find("New York Spot Price", start)
    return html[start:end] if end != -1 else html[start:]


def _extract_rows(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()  # remove non-visible content
    text = soup.get_text(separator=" ")

    rows: dict[str, dict] = {}
    for m in ROW_RE.finditer(text):
        metal = m["metal"].lower()
        if metal in rows:  # keep the first occurrence only
            continue
        rows[metal] = {
            "metal": metal,
            "quote_time_utc": _to_iso_utc(m["date"], m["time"]),
            "quote_time_source": f"{m['date']} {m['time']} EST",
            "bid": _num(m["bid"]),
            "ask": _num(m["ask"]),
            "change": _num(m["chg"]),
            "change_pct": _num(m["pct"]),
            "low": _num(m["low"]),
            "high": _num(m["high"]),
        }
    return rows


def parse_html(html: str) -> list[dict]:
    rows = _extract_rows(_slice_world_section(html))
    if len(rows) < MIN_ROWS:  # slicing may have cut too much -> retry on full page
        rows = _extract_rows(html)
    return list(rows.values())


def validate(rows: list[dict]) -> list[dict]:
    """Drop obviously broken rows so bad data never reaches the database."""
    good = []
    for r in rows:
        if r["bid"] is None or r["ask"] is None or r["bid"] <= 0 or r["bid"] > r["ask"]:
            log.warning("Dropping invalid row: %s", r)
            continue
        good.append(r)
    return good


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_html_http() -> str:
    r = _session.get(URL, timeout=(5, 15))
    r.raise_for_status()
    return r.text


def fetch_html_browser() -> str:
    """Fallback only: real headless browser, heavy assets blocked for speed."""
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
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_function(
            "document.body.innerText.includes('GOLD') "
            "&& /\\d,\\d{3}\\.\\d{2}/.test(document.body.innerText)",
            timeout=20_000,
        )
        html = page.content()
        browser.close()
        return html


def scrape() -> dict:
    rows: list[dict] = []
    method = "http"
    try:
        rows = validate(parse_html(fetch_html_http()))
    except requests.RequestException as e:
        log.warning("HTTP fetch failed: %s", e)

    if len(rows) < MIN_ROWS:
        method = "browser"
        log.info("Only %d rows via HTTP; falling back to headless browser", len(rows))
        rows = validate(parse_html(fetch_html_browser()))

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
# Storage
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
    ap.add_argument("-i", "--interval", type=int, default=0, help="poll every N seconds (0 = once)")
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
            payload = scrape()
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
