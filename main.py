#!/usr/bin/env python3
"""
Political News Bot — ultra performant, cached, batched, and hybrid-summarizer.
Fetches RSS via HEAD/ETag checks, HTML in parallel, de-dupes, caches summaries,
batch-summarizes trending stories with OpenAI (fallback to local BART),
stores results and posts to Discord every 24 minutes.
"""

import os
import sys
import time
import sqlite3
import hashlib
import logging
import asyncio
import datetime

import yaml
import httpx
import feedparser
import requests
import transformers
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import token_set_ratio
from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI

# CONFIGURATION
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(__file__)
CONF = yaml.safe_load(open(os.path.join(ROOT, "config/sources.yml")))
DB_PATH = os.path.join(ROOT, "data/news.sqlite")

# Summarization clients
oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
local_summarizer = transformers.pipeline("summarization", model="facebook/bart-large-cnn")

# Discord webhooks
WEBHOOKS = []
for key in ("DISCORD_WEBHOOKS", "DISCORD_WEBHOOK"):
    val = os.getenv(key)
    if val:
        WEBHOOKS += [u.strip() for u in val.split(",")]

# HTTP headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/100.0.0.0 Safari/537.36"
    )
}

# DATABASE SETUP
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

# FETCH AND PARSE RSS WITH ETag
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
async def fetch_feed(src, client):
    conn = src["conn"]
    # conditional GET based on ETag
    row = conn.execute("SELECT etag FROM seen_items WHERE url=?", (src["url"],)).fetchone()
    headers = {"If-None-Match": row[0]} if row and row[0] else {}
    r = await client.get(src["url"], headers=headers, timeout=20)
    if r.status_code == 304:
        return []
    r.raise_for_status()
    etag = r.headers.get("ETag")
    feed = feedparser.parse(r.text)
    items = []
    for e in feed.entries[:20]:
        h = hashlib.md5((e.get("title","") + e.get("link","")).encode()).hexdigest()
        items.append({
            "hash": h,
            "title": e.get("title","")[:120],
            "url": e.get("link",""),
            "body": (e.get("summary","") or "")[:300],
            "tags": ",".join(src.get("tags", [])),
            "fetched": datetime.datetime.utcnow().isoformat(),
            "etag": etag
        })
    return items

# FETCH AND PARSE HTML FALLBACKS
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
async def fetch_html(src, client):
    r = await client.get(src["url"], timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for el in soup.select(src["selector"]):
        title = (el.get("alt") or el.get_text())[:120]
        body = " ".join(el.stripped_strings)[:300]
        link = el.select_one("a[href]")["href"] if el.select_one("a[href]") else src["url"]
        h = hashlib.md5((title + link).encode()).hexdigest()
        out.append({
            "hash": h,
            "title": title,
            "url": link,
            "body": body,
            "tags": ",".join(src.get("tags", [])),
            "fetched": datetime.datetime.utcnow().isoformat(),
            "etag": None
        })
    return out

# DEDUPE AGAINST DB
def dedupe_and_filter(conn, items):
    new = []
    for i in items:
        if not conn.execute("SELECT 1 FROM seen_items WHERE hash=?", (i["hash"],)).fetchone():
            new.append(i)
    return new

# BATCH SUMMARIZATION VIA OPENAI
def batch_summarize(items):
    joined = "\n---\n".join([it["body"] for it in items])
    prompt = f"Summarize the following {len(items)} articles into three paragraphs each, separated by '---':\n{joined}"
    resp = oai.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        max_tokens=3600
    )
    parts = resp.choices[0].message.content.split("---")
    return [p.strip() for p in parts]

# POST TO DISCORD
def post_to_discord(trending, others):
    embed = {
        "title": f"📰 {len(trending)} hot + {len(others)} more",
        "color": CONF["colors"]["default"],
        "fields": []
    }
    for it, sm in trending:
        embed["fields"].append({"name": f"🔥 {it['title']}", "value": sm + "\n" + it["url"]})
    for it in others:
        embed["fields"].append({"name": it["title"], "value": it["url"]})
    for hook in WEBHOOKS:
        requests.post(hook, json={"embeds":[embed]})

# MAIN LOGIC
def main():
    conn = db_connect()
    client = httpx.AsyncClient(headers=HEADERS)

    # attach DB connection to each RSS source
    for s in CONF["rss"]:
        s["conn"] = conn

    # schedule fetch tasks
    rss_tasks = [fetch_feed(s, client) for s in CONF["rss"]]
    html_tasks = [fetch_html(s, client) for s in CONF["html"]]

    results = asyncio.get_event_loop().run_until_complete(
        asyncio.gather(*(rss_tasks + html_tasks))
    )

    # collect and dedupe
    n = len(rss_tasks)
    rss_items = [i for batch in results[:n] for i in batch]
    html_items = [i for batch in results[n:] for i in batch]
    new_items = dedupe_and_filter(conn, rss_items + html_items)
    new_items.sort(key=lambda x: x["fetched"], reverse=True)

    # top 6 vs others
    top6 = new_items[:6]
    others = new_items[6:]

    # try batch summarization, fallback to local if it fails
    try:
        summaries = batch_summarize(top6)
    except Exception as e:
        logger.error(f"Batch summarize failed: {e}, falling back to local")
        summaries = [local_summarizer(it["body"], max_length=150)[0]["summary_text"] for it in top6]

    trending = list(zip(top6, summaries))

    # cache all new items (with summaries for top6)
    for it in new_items:
        summary = dict(trending).get(it["hash"])
        conn.execute(
            "INSERT OR IGNORE INTO seen_items VALUES(?,?,?,?,?,?,?)",
            (it["hash"], it["title"], it["url"], it["tags"],
             it["fetched"], summary, it.get("etag"))
        )
    conn.commit()

    # post if there’s anything new
    if trending or others:
        post_to_discord(trending, others)

if __name__ == "__main__":
    main()
