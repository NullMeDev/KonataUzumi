#!/usr/bin/env python3
"""
Political News Bot — live mode only
Fetches RSS+HTML in parallel, flags 🔥 top 6, summarizes them via OpenAI v1,
lists the rest, and posts directly to Discord.
"""

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
from openai import OpenAI
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import token_set_ratio
from tenacity import retry, stop_after_attempt, wait_exponential

# ───────────────────── CONFIGURATION ─────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(__file__)
CONF = yaml.safe_load(open(os.path.join(ROOT, "config/sources.yml")))
DB_PATH = os.path.join(ROOT, "data/deals.sqlite")

# instantiate new OpenAI v1 client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# collect Discord webhooks
WEBHOOKS = []
for key in ("DISCORD_WEBHOOKS", "DISCORD_WEBHOOK"):
    val = os.getenv(key)
    if val:
        WEBHOOKS += [h.strip() for h in val.split(",") if h.strip()]

# browser User-Agent to avoid 403s on .gov pages
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.0.0 Safari/537.36"
    )
}

# ─────────────────── ITEM & DB ───────────────────────────
class Item(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.hash = hashlib.md5(f"{self.get('title')}|{self.get('url')}".encode()).hexdigest()


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
    raw = item.get("type", "political_news")
    ft = raw if raw in CONF["feed_types"] else "political_news"
    cfg = CONF["feed_types"][ft]
    row = conn.execute(
        "SELECT posted_at,expires_at FROM seen_items WHERE hash=?", (item.hash,)
    ).fetchone()
    if not row:
        return False
    posted, expires = map(datetime.datetime.fromisoformat, row)
    if now > expires:
        return False
    # similarity check to avoid near-duplicates
    for (other,) in conn.execute(
        "SELECT title FROM seen_items WHERE type=? AND hash!=?", (ft, item.hash)
    ):
        if token_set_ratio(item["title"], other) > cfg["similarity_threshold"]:
            return True
    return False


# ───────────────────── FETCH & PARSE ──────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(2, 30))
async def fetch_html(url, client: httpx.AsyncClient) -> str:
    r = await client.get(url, timeout=20)
    r.raise_for_status()
    return r.text


async def fetch_rss(src: dict, client: httpx.AsyncClient) -> list[Item]:
    try:
        r = await client.get(src["url"], timeout=20)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if "xml" not in ctype and "rss" not in ctype:
            logger.warning(f"Skipping non-XML {src['name']} ({ctype})")
            return []
        feed = feedparser.parse(r.text)
        if feed.bozo:
            logger.warning(f"Malformed {src['name']}: {feed.bozo_exception}")
        items = []
        for e in feed.entries[:20]:
            t = e.get("title", "")[:120]
            u = e.get("link", "")
            b = (e.get("summary", "") or "")[:300]
            if not (t and u):
                continue
            items.append(
                Item(
                    title=t,
                    url=u,
                    body=b,
                    source=src["name"],
                    type="rss",
                    tags=src.get("tags", []),
                    fetched=datetime.datetime.utcnow().isoformat(),
                )
            )
        return items
    except Exception as e:
        logger.error(f"RSS fetch error {src['name']}: {e}")
        return []


def parse_html(src: dict, html: str) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for el in soup.select(src["selector"]):
        t = el.get("alt") or el.get_text(strip=True) or src["name"]
        t = t[:120]
        b = "".join(el.stripped_strings)[:300]
        a = el.select_one("a[href]")
        u = a["href"] if a else src["url"]
        out.append(
            Item(
                title=t,
                url=u,
                body=b,
                source=src["name"],
                type="html",
                tags=src.get("tags", []),
                fetched=datetime.datetime.utcnow().isoformat(),
            )
        )
    return out


# ───────────────────── SUMMARIZATION ──────────────────────
def summarize(text: str) -> str:
    response = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Summarize this political news article into three concise paragraphs."},
            {"role": "user", "content": text},
        ],
        temperature=0.5,
        max_tokens=600,
    )
    return response.choices[0].message.content.strip()


# ───────────────────── POST TO DISCORD ────────────────────
def post(trending: list[tuple[Item, str]], others: list[Item]):
    now = datetime.datetime.utcnow().isoformat()
    embed = {
        "title": f"📰 {len(trending)} trending + {len(others)} more",
        "color": CONF["colors"]["default"],
        "timestamp": now,
        "fields": [],
    }
    for it, sm in trending:
        embed["fields"].append({"name": f"🔥 {it['title']}", "value": f"{sm}\n{it['url']}", "inline": False})
    for it in others:
        embed["fields"].append({"name": it["title"], "value": it["url"], "inline": False})
    for hook in WEBHOOKS:
        try:
            r = requests.post(hook, json={"embeds": [embed]})
            if r.status_code != 204:
                logger.error(f"Post failed {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"Discord post error: {e}")
        time.sleep(1)


# ─────────────────────────── MAIN ───────────────────────────
def main():
    conn = db_connect()
    client = httpx.AsyncClient(headers=HEADERS)

    rss_tasks = [fetch_rss(s, client) for s in CONF.get("rss", [])]
    html_futs = [fetch_html(src["url"], client) for src in CONF.get("html", [])]

    fetched = asyncio.get_event_loop().run_until_complete(
        asyncio.gather(*(rss_tasks + html_futs), return_exceptions=True)
    )

    n = len(rss_tasks)
    rss_items = []
    html_items = []
    for i, res in enumerate(fetched):
        if isinstance(res, Exception):
            continue
        if i < n:
            rss_items += [it for it in res if not is_item_seen(conn, it)]
        else:
            src = CONF["html"][i - n]
            html_items += [it for it in parse_html(src, res) if not is_item_seen(conn, it)]

    all_new = sorted(rss_items + html_items, key=lambda it: it["fetched"], reverse=True)
    trending = all_new[:6]
    others = all_new[6:]

    # summarize each trending item, but don’t let failures abort
    summaries = {}
    for it in trending:
        try:
            summaries[it.hash] = summarize(it["body"])
        except Exception as e:
            logger.error(f"Summarization failed for {it['url']}: {e}")
            summaries[it.hash] = "(summary unavailable)"

    if trending or others:
        post([(it, summaries.get(it.hash, "")) for it in trending], others)


if __name__ == "__main__":
    main()
