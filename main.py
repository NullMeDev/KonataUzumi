#!/usr/bin/env python3
"""
Enhanced Political News Scraper with JSON metrics, color embeds, and 3-paragraph summaries
"""

import argparse
import asyncio
import datetime
import json
import os
import sqlite3
import sys
import logging
import time
import hashlib

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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(__file__)
CONF = yaml.safe_load(open(os.path.join(ROOT, "config/sources.yml")))
DB_PATH = os.path.join(ROOT, "data/deals.sqlite")

# OpenAI API key for summaries
openai.api_key = os.getenv("OPENAI_API_KEY")

# Discord webhooks
env_hooks = os.getenv("DISCORD_WEBHOOKS", "")
single_hook = os.getenv("DISCORD_WEBHOOK", "")
if env_hooks:
    WEBHOOKS = [h.strip() for h in env_hooks.split(",") if h.strip()]
elif single_hook:
    WEBHOOKS = [single_hook]
else:
    WEBHOOKS = []

logger.info(f"Configured webhooks: {WEBHOOKS}")

# ────────────────────────── ITEM & DB ────────────────────────────
class Item(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calculate_hash()

    def calculate_hash(self):
        content = f"{self.get('title','')}{self.get('url','')}"
        self['hash'] = hashlib.md5(content.encode()).hexdigest()

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
    feed_type = item.get('type', 'political_news')
    cfg = CONF['feed_types'].get(feed_type, {})
    row = conn.execute(
        "SELECT posted_at, expires_at FROM seen_items WHERE hash=?",
        (item['hash'],)
    ).fetchone()
    if not row:
        return False
    posted_at = datetime.datetime.fromisoformat(row[0])
    expires_at = datetime.datetime.fromisoformat(row[1])
    if now > expires_at:
        return False
    # also check similarity against other seen titles to avoid duplicates
    similar_rows = conn.execute(
        "SELECT title FROM seen_items WHERE type=? AND hash!=?",
        (feed_type, item['hash'])
    ).fetchall()
    thresh = cfg.get('similarity_threshold', 90)
    for (other_title,) in similar_rows:
        if token_set_ratio(item['title'], other_title) > thresh:
            return True
    return True

# ─────────────────────── FETCH & SCRAPE ─────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
async def fetch_html(url, client: httpx.AsyncClient):
    logger.info(f"Fetching HTML from: {url}")
    r = await client.get(url, timeout=20)
    r.raise_for_status()
    return r.text

def scrape_rss(entry):
    logger.info(f"Processing RSS feed: {entry.get('url')}")
    feed = feedparser.parse(entry.get("url", ""))
    if feed.bozo:
        logger.error(f"RSS Feed Error: {feed.bozo_exception}")
        return []
    out = []
    for e in feed.entries[:20]:
        title = e.get("title", "")[:120]
        body = e.get("summary", "") or (e.get("content", [{}])[0].get("value", ""))
        body = body[:500]
        link = e.get("link", "")
        if not (title and link):
            continue
        item = Item({
            "title": title,
            "body": body,
            "url": link,
            "source": entry.get("name"),
            "type": "rss",
            "tags": entry.get("tags", []),
            "fetched": datetime.datetime.utcnow().isoformat()
        })
        out.append(item)
    return out

async def scrape_html(entry, client: httpx.AsyncClient):
    html = await fetch_html(entry["url"], client)
    soup = BeautifulSoup(html, "lxml")
    out = []
    for el in soup.select(entry.get("selector", "")):
        title = (el.get("alt") or el.get_text() or entry.get("name"))[:120]
        body = " ".join(el.stripped_strings)[:500]
        link = entry.get("url")
        a = el.select_one("a[href]")
        if a and a.get("href"):
            link = a["href"]
        item = Item({
            "title": title,
            "body": body,
            "url": link,
            "source": entry.get("name"),
            "type": entry.get("type", "html"),
            "tags": entry.get("tags", []),
            "fetched": datetime.datetime.utcnow().isoformat()
        })
        out.append(item)
    return out

# ─────────────────────── SUMMARY UTIL ──────────────────────────
def summarize_article(text: str) -> str:
    prompt = (
        "Summarize the following political news article into three concise paragraphs:\n\n"
        + text
    )
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are an assistant that summarizes news articles."},
            {"role": "user",   "content": prompt}
        ],
        temperature=0.5,
        max_tokens=600
    )
    return resp.choices[0].message.content.strip()

# ─────────────────────────── MAIN ─────────────────────────────
def post_to_discord(items, item_type):
    now = datetime.datetime.utcnow()
    payload = {
        "embeds": [{
            "title": f"Found {len(items)} new {item_type.replace('_',' ')} items",
            "color": CONF.get("colors", {}).get("default", 15844367),
            "timestamp": now.isoformat(),
            "fields": []
        }]
    }
    for i in sorted(items, key=lambda x: x['fetched'], reverse=True)[:10]:
        payload["embeds"][0]["fields"].append({
            "name": i["title"],
            "value": f"{i.get('body','No description')}\n{i['url']}",
            "inline": False
        })
    for hook in WEBHOOKS:
        try:
            r = requests.post(hook, json=payload)
            if r.status_code == 204:
                logger.info(f"✅ Successfully posted {item_type}")
            else:
                logger.error(f"❌ Failed to post: {r.status_code} {r.text}")
            time.sleep(1)
        except Exception as e:
            logger.error(f"❌ Error posting: {e}")

def main(dry_run=False):
    """
    Entry point for CLI and GitHub Actions.
    If dry_run is True, summary URLs are printed instead of posted.
    """
    # Connect to database
    conn = db_connect()

    # 1) Fetch RSS feeds
    rss_items = []
    for entry in CONF.get("rss", []):
        try:
            items = scrape_rss(entry)
            rss_items += [i for i in items if not is_item_seen(conn, i)]
        except Exception as e:
            logger.error(f"RSS error for {entry.get('name')}: {e}")

    # 2) Fetch HTML fallbacks
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = httpx.AsyncClient(timeout=20)
    tasks = [scrape_html(e, client) for e in CONF.get("html", [])]
    html_results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    html_items = []
    for entry, res in zip(CONF.get("html", []), html_results):
        if isinstance(res, Exception):
            logger.error(f"HTML error for {entry.get('name')}: {res}")
        else:
            html_items += [i for i in res if not is_item_seen(conn, i)]

    # 3) Summarize
    all_new = rss_items + html_items
    for item in all_new:
        try:
            item["body"] = summarize_article(item.get("body", ""))
        except Exception as e:
            logger.error(f"Summarization error {item.get('url')}: {e}")

    # 4) Post or dry-run
    if dry_run:
        for i in all_new:
            print(f"{i['title']} — {i['url']}")
    else:
        if all_new:
            post_to_discord(all_new, "political_news")

if __name__ == "__main__":
    # If invoked directly, treat as a dry-run when --dry-run is passed
    args = sys.argv
    main(dry_run="--dry-run" in args)
