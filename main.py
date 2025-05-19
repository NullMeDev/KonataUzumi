#!/usr/bin/env python3
"""
Enhanced Political News Scraper with 🔥 flags on top 6, summaries for trending items,
and short listings for the rest.
"""

import argparse
import asyncio
import datetime
import os
import sqlite3
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
    ft = item.get("type","political_news")
    cfg = CONF["feed_types"][ft]
    row = conn.execute(
        "SELECT posted_at, expires_at FROM seen_items WHERE hash=?",
        (item.hash,)
    ).fetchone()
    if not row:
        return False
    posted, expires = map(datetime.datetime.fromisoformat, row)
    if now > expires:
        return False
    # avoid near-duplicates
    similar = conn.execute(
        "SELECT title FROM seen_items WHERE type=? AND hash!=?",
        (ft, item.hash)
    ).fetchall()
    for (other,) in similar:
        if token_set_ratio(item["title"], other) > cfg["similarity_threshold"]:
            return True
    return True

# ────────────────────────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(2, 30))
async def fetch_html(url, client: httpx.AsyncClient):
    r = await client.get(url, timeout=15)
    r.raise_for_status()
    return r.text

def scrape_rss(src):
    logger.info(f"RSS → {src['name']}")
    feed = feedparser.parse(src["url"])
    if feed.bozo:
        logger.error(f"RSS error {feed.bozo_exception}")
        return []
    out = []
    for e in feed.entries[:20]:
        title = e.get("title","")[:120]
        link  = e.get("link","")
        body  = (e.get("summary","") or "")[:500]
        if not (title and link): continue
        it = Item(
          title=title, url=link, body=body,
          source=src["name"], type="rss",
          tags=src.get("tags",[]), fetched=datetime.datetime.utcnow().isoformat()
        )
        out.append(it)
    return out

async def scrape_html(src, client):
    logger.info(f"HTML → {src['name']}")
    html = await fetch_html(src["url"], client)
    soup = BeautifulSoup(html, "lxml")
    out = []
    for el in soup.select(src["selector"]):
        txt = el.get_text(strip=True)[:120] or src["name"]
        link = el.select_one("a[href]")
        url  = link["href"] if link else src["url"]
        it = Item(
          title=txt, url=url,
          body="".join(el.stripped_strings)[:500],
          source=src["name"], type="html",
          tags=src.get("tags",[]), fetched=datetime.datetime.utcnow().isoformat()
        )
        out.append(it)
    return out

# ────────────────────────────────────────────────────────────────────
def summarize(text):
    resp = openai.ChatCompletion.create(
      model="gpt-3.5-turbo",
      messages=[
        {"role":"system","content":"You summarize political news into three paragraphs."},
        {"role":"user","content":text}
      ],
      temperature=0.5,
      max_tokens=600
    )
    return resp.choices[0].message.content.strip()

# ─────────────────────────── MAIN ─────────────────────────────
def post_to_discord(trending, others):
    now = datetime.datetime.utcnow().isoformat()
    embed = {
      "title": f"📰 {len(trending)} trending + {len(others)} more",
      "color": CONF["colors"]["default"],
      "timestamp": now,
      "fields": []
    }
    # Trending first
    for item, summary in trending:
      embed["fields"].append({
        "name": f"🔥 {item['title']}",
        "value": f"{summary}\n{item['url']}",
        "inline": False
      })
    # Then the rest
    for item in others:
      embed["fields"].append({
        "name": item["title"],
        "value": item["url"],
        "inline": False
      })
    payload = {"embeds":[embed]}
    for hook in WEBHOOKS:
      try:
        r = requests.post(hook, json=payload)
        if r.status_code!=204:
          logger.error(f"Post failed {r.status_code} {r.text}")
      except Exception as e:
        logger.error(f"Error posting: {e}")
      time.sleep(1)

def main(dry_run=False, max_items=None, skip_summary=False):
    conn = db_connect()

    # 1) collect
    rss_items = []
    for src in CONF.get("rss",[]):
        try:
            items = scrape_rss(src)
            rss_items += [i for i in items if not is_item_seen(conn,i)]
        except Exception as e:
            logger.error(e)

    # 2) html fallback
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = httpx.AsyncClient()
    tasks = [scrape_html(s, client) for s in CONF.get("html",[])]
    results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    html_items=[]
    for src,res in zip(CONF.get("html",[]),results):
        if isinstance(res,Exception):
            logger.error(res)
        else:
            html_items += [i for i in res if not is_item_seen(conn,i)]

    all_new = sorted(rss_items+html_items,
                     key=lambda i: i["fetched"], reverse=True)

    # limit total if asked
    if max_items:
        all_new = all_new[:max_items*2]  # fetch a few extra so trending picks make sense

    # split trending vs others
    trending = all_new[:6]
    others   = all_new[6:]

    # summarize only trending
    summaries = {}
    if not skip_summary:
      for it in trending:
        try:
          summaries[it.hash] = summarize(it["body"])
        except Exception as e:
          logger.error(f"Summarize failed: {e}")
          summaries[it.hash] = "(summary failed)"

    # dry-run?
    if dry_run:
      print("🔥 Trending:")
      for it in trending:
        s = summaries.get(it.hash, "(no summary)")
        print(f"🔥 {it['title']}\n{s}\n{it['url']}\n")
      print("— Other new articles:")
      for it in others:
        print(f"{it['title']} — {it['url']}")
      return

    # post live
    post_to_discord(
      trending=[(it, summaries.get(it.hash,"")) for it in trending],
      others=others
    )

if __name__=="__main__":
    # treat direct call as dry-run by default
    args = sys.argv
    main(
      dry_run="--dry-run" in args,
      max_items=None,
      skip_summary="--skip-summary" in args
    )
