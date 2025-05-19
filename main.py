#!/usr/bin/env python3
"""
Enhanced Political News Scraper with parallel RSS+HTML fetch,
🔥 flags on the top 6, three-paragraph summaries, and listing of others.
"""

import argparse
import asyncio
import datetime
import os
import sqlite3
import logging
import time
import hashlib
import sys

import feedparser
import httpx
import yaml
import requests
import openai

from bs4 import BeautifulSoup
from rapidfuzz.fuzz import token_set_ratio
from tenacity import retry, stop_after_attempt, wait_exponential

# ───────────────────────── CONFIGURATION ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(__file__)
CONF = yaml.safe_load(open(os.path.join(ROOT, "config/sources.yml")))
DB_PATH = os.path.join(ROOT, "data/deals.sqlite")
openai.api_key = os.getenv("OPENAI_API_KEY")

# Discord webhooks
hooks = os.getenv("DISCORD_WEBHOOKS") or os.getenv("DISCORD_WEBHOOK", "")
WEBHOOKS = [h.strip() for h in hooks.split(",") if h.strip()]
logger.info(f"Webhooks: {WEBHOOKS}")

# Use a realistic browser UA to avoid 403s on .gov pages
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.0.0 Safari/537.36"
    )
}

# ────────────────────────── ITEM & DB ────────────────────────────
class Item(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hash = self._calc_hash()

    def _calc_hash(self):
        data = f"{self.get('title','')}|{self.get('url','')}"
        return hashlib.md5(data.encode()).hexdigest()

def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS seen_items (
        hash TEXT PRIMARY KEY,
        type TEXT,
        title TEXT,
        posted_at TEXT,
        expires_at TEXT
      )
    """)
    return conn

def is_item_seen(conn, item):
    now = datetime.datetime.utcnow()
    raw_type = item.get("type", "political_news")
    # normalize any unknown types (e.g. 'rss', 'html') into our political_news bucket
    ft = raw_type if raw_type in CONF["feed_types"] else "political_news"
    cfg = CONF["feed_types"].get(ft, {})
    row = conn.execute(
        "SELECT posted_at, expires_at FROM seen_items WHERE hash=?",
        (item.hash,)
    ).fetchone()
    if not row:
        return False
    posted, expires = map(datetime.datetime.fromisoformat, row)
    if now > expires:
        return False
    # similarity check to avoid near-duplicates
    similar = conn.execute(
        "SELECT title FROM seen_items WHERE type=? AND hash!=?",
        (ft, item.hash)
    ).fetchall()
    thresh = cfg.get("similarity_threshold", 90)
    for (other_title,) in similar:
        if token_set_ratio(item["title"], other_title) > thresh:
            return True
    return False

# ────────────────────────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
async def fetch_html_content(url: str, client: httpx.AsyncClient) -> str:
    r = await client.get(url, timeout=20)
    r.raise_for_status()
    return r.text

async def fetch_feed(src: dict, client: httpx.AsyncClient) -> list[Item]:
    """Fetch an RSS feed URL, skip if not XML, parse up to 20 entries."""
    try:
        resp = await client.get(src["url"], timeout=20)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "xml" not in ctype and "rss" not in ctype:
            logger.warning(f"Skipping non-XML feed {src['name']} ({ctype})")
            return []
        feed = feedparser.parse(resp.text)
        if feed.bozo:
            logger.warning(f"Malformed feed {src['name']}: {feed.bozo_exception}")
        items = []
        for e in feed.entries[:20]:
            title = e.get("title", "")[:120]
            link  = e.get("link", "")
            body  = (e.get("summary", "") or "")[:300]
            if not (title and link):
                continue
            items.append(Item(
                title=title,
                url=link,
                body=body,
                source=src["name"],
                type="rss",
                tags=src.get("tags", []),
                fetched=datetime.datetime.utcnow().isoformat()
            ))
        return items
    except Exception as e:
        logger.error(f"Error fetching RSS {src['name']}: {e}")
        return []

def parse_html(src: dict, html: str) -> list[Item]:
    """Parse HTML page with a CSS selector into items."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for el in soup.select(src["selector"]):
        title = el.get("alt") or el.get_text(strip=True) or src["name"]
        title = title[:120]
        body = "".join(el.stripped_strings)[:300]
        a = el.select_one("a[href]")
        link = a["href"] if a else src["url"]
        out.append(Item(
            title=title,
            url=link,
            body=body,
            source=src["name"],
            type=src.get("type", "html"),
            tags=src.get("tags", []),
            fetched=datetime.datetime.utcnow().isoformat()
        ))
    return out

# ─────────────────────── SUMMARY UTIL ──────────────────────────
def summarize(text: str) -> str:
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role":"system", "content":"Summarize this political news article into three concise paragraphs."},
            {"role":"user",   "content":text}
        ],
        temperature=0.5,
        max_tokens=600
    )
    return resp.choices[0].message.content.strip()

# ─────────────────────────── POST ─────────────────────────────
def post_to_discord(trending: list[tuple[Item,str]], others: list[Item]):
    now = datetime.datetime.utcnow().isoformat()
    embed = {
        "title": f"📰 {len(trending)} trending + {len(others)} more",
        "color": CONF["colors"]["default"],
        "timestamp": now,
        "fields": []
    }
    for item, summary in trending:
        embed["fields"].append({
            "name": f"🔥 {item['title']}",
            "value": f"{summary}\n{item['url']}",
            "inline": False
        })
    for item in others:
        embed["fields"].append({
            "name": item["title"],
            "value": item["url"],
            "inline": False
        })
    payload = {"embeds": [embed]}
    for hook in WEBHOOKS:
        try:
            r = requests.post(hook, json=payload)
            if r.status_code != 204:
                logger.error(f"Post failed {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"Discord post error: {e}")
        time.sleep(1)

# ─────────────────────────── MAIN ─────────────────────────────
def main(dry_run=False, max_items=None, skip_summary=False):
    conn = db_connect()

    # prepare async client
    client = httpx.AsyncClient(headers=HEADERS)

    # schedule fetches
    rss_tasks  = [fetch_feed(src, client) for src in CONF.get("rss", [])]
    html_tasks = [fetch_html_content(src["url"], client) for src in CONF.get("html", [])]

    # run fetches in parallel
    results = asyncio.get_event_loop().run_until_complete(
        asyncio.gather(*(rss_tasks + html_tasks), return_exceptions=True)
    )

    # split results back
    rss_items = []
    html_items = []
    n_rss = len(rss_tasks)
    for idx, res in enumerate(results):
        if isinstance(res, Exception):
            continue
        if idx < n_rss:
            rss_items += [i for i in res if not is_item_seen(conn, i)]
        else:
            src = CONF["html"][idx - n_rss]
            html_items += [i for i in parse_html(src, res) if not is_item_seen(conn, i)]

    all_new = sorted(rss_items + html_items,
                     key=lambda i: i["fetched"], reverse=True)

    # optionally limit
    if max_items:
        all_new = all_new[: max_items * 2]

    # split top 6 vs rest
    trending = all_new[:6]
    others   = all_new[6:]

    # summarize trending
    summaries = {}
    if not skip_summary:
        for item in trending:
            try:
                summaries[item.hash] = summarize(item["body"])
            except Exception as e:
                logger.error(f"Summarize error {item['url']}: {e}")
                summaries[item.hash] = "(summary failed)"

    # dry-run prints
    if dry_run:
        print("🔥 Trending Articles:")
        for item in trending:
            print(f"🔥 {item['title']}")
            print(summaries.get(item.hash, "(no summary)"))
            print(item["url"], "\n")
        print("— Other New Articles:")
        for item in others:
            print(f"{item['title']} — {item['url']}")
        return

    # live post
    if trending or others:
        post_to_discord(
            trending=[(item, summaries.get(item.hash, "")) for item in trending],
            others=others
        )

if __name__ == "__main__":
    args = sys.argv
    main(
        dry_run="--dry-run" in args,
        max_items=None,
        skip_summary="--skip-summary" in args
    )
