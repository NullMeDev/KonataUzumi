#!/usr/bin/env python3
"""
Political News Bot — fully on OpenAI, fast & cached.
"""

import os
import sqlite3
import hashlib
import logging
import asyncio
import datetime
import yaml
import httpx
import feedparser
import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import token_set_ratio
from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI

# CONFIG
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
ROOT = os.path.dirname(__file__)
CONF = yaml.safe_load(open(os.path.join(ROOT, "config/sources.yml")))
DB_PATH = os.path.join(ROOT, "data/news.sqlite")

# OpenAI client
oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Discord webhooks
WEBHOOKS = []
for key in ("DISCORD_WEBHOOKS","DISCORD_WEBHOOK"):
    if os.getenv(key):
        WEBHOOKS += [u.strip() for u in os.getenv(key).split(",")]

# HTTP client headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/100.0.0.0 Safari/537.36"
    )
}

# DB setup
def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS seen_items (
        hash TEXT PRIMARY KEY,
        title TEXT,
        url TEXT,
        tags TEXT,
        fetched TEXT,
        summary TEXT,
        etag TEXT
      )
    """)
    return conn

# Fetch RSS with ETag
@retry(stop=stop_after_attempt(3), wait=wait_exponential(1,10))
async def fetch_feed(src, client):
    conn = src["_conn"]
    row = conn.execute("SELECT etag FROM seen_items WHERE url=?", (src["url"],)).fetchone()
    headers = {"If-None-Match": row[0]} if row and row[0] else {}
    r = await client.get(src["url"], headers=headers, timeout=20)
    if r.status_code == 304:
        return []
    r.raise_for_status()
    etag = r.headers.get("ETag")
    feed = feedparser.parse(r.text)
    out = []
    for e in feed.entries[:20]:
        h = hashlib.md5((e.get("title","")+e.get("link","")).encode()).hexdigest()
        out.append({
            "hash": h,
            "title": e.get("title","")[:120],
            "url": e.get("link",""),
            "body": (e.get("summary","") or "")[:300],
            "tags": ",".join(src.get("tags", [])),
            "fetched": datetime.datetime.utcnow().isoformat(),
            "etag": etag
        })
    return out

# Fetch HTML fallback
@retry(stop=stop_after_attempt(3), wait=wait_exponential(1,10))
async def fetch_html(src, client):
    r = await client.get(src["url"], timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for el in soup.select(src["selector"]):
        t = (el.get("alt") or el.get_text())[:120]
        b = " ".join(el.stripped_strings)[:300]
        link = el.select_one("a[href]")["href"] if el.select_one("a[href]") else src["url"]
        h = hashlib.md5((t+link).encode()).hexdigest()
        out.append({
            "hash": h,
            "title": t,
            "url": link,
            "body": b,
            "tags": ",".join(src.get("tags", [])),
            "fetched": datetime.datetime.utcnow().isoformat(),
            "etag": None
        })
    return out

# Dedupe
def dedupe(conn, items):
    return [i for i in items if not conn.execute("SELECT 1 FROM seen_items WHERE hash=?", (i["hash"],)).fetchone()]

# Batch summarize
def batch_summarize(items):
    joined = "\n---\n".join(it["body"] for it in items)
    prompt = f"Summarize the following {len(items)} articles into three concise paragraphs each, separated by '---':\n{joined}"
    resp = oai.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        max_tokens=3600
    )
    return [p.strip() for p in resp.choices[0].message.content.split("---")]

# Post to Discord
def post(trending, others):
    embed = {
        "title": f"📰 {len(trending)} trending + {len(others)} more",
        "color": CONF["colors"]["default"],
        "fields": []
    }
    for it, sm in trending:
        embed["fields"].append({"name": f"🔥 {it['title']}", "value": sm + "\n" + it["url"]})
    for it in others:
        embed["fields"].append({"name": it["title"], "value": it["url"]})
    for hook in WEBHOOKS:
        requests.post(hook, json={"embeds":[embed]})

# Main
def main():
    conn = db_connect()
    client = httpx.AsyncClient(headers=HEADERS)

    # bind conn for ETag lookups
    for src in CONF["rss"]:
        src["_conn"] = conn

    rss_tasks  = [fetch_feed(s, client) for s in CONF["rss"]]
    html_tasks = [fetch_html(s, client) for s in CONF["html"]]

    results = asyncio.get_event_loop().run_until_complete(
        asyncio.gather(*(rss_tasks + html_tasks))
    )

    n = len(rss_tasks)
    rss_items  = [i for batch in results[:n] for i in batch]
    html_items = [i for batch in results[n:] for i in batch]

    new_items = dedupe(conn, rss_items + html_items)
    new_items.sort(key=lambda x: x["fetched"], reverse=True)

    top6   = new_items[:6]
    others = new_items[6:]

    summaries = batch_summarize(top6) if top6 else []
    trending = list(zip(top6, summaries))

    # cache all in DB
    for it in new_items:
        summary = dict(trending).get(it["hash"])
        conn.execute(
            "INSERT OR IGNORE INTO seen_items VALUES(?,?,?,?,?,?,?)",
            (it["hash"], it["title"], it["url"], it["tags"],
             it["fetched"], summary, it["etag"])
        )
    conn.commit()

    if trending or others:
        post(trending, others)

if __name__ == "__main__":
    main()
