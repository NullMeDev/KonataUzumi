#!/usr/bin/env python3
"""Enhanced Political News Scraper with JSON metrics, color embeds, and 3-paragraph summaries"""

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

openai.api_key = os.getenv("OPENAI_API_KEY")

env_hooks = os.getenv("DISCORD_WEBHOOKS", "")
single_hook = os.getenv("DISCORD_WEBHOOK", "")
if env_hooks:
    WEBHOOKS = [h.strip() for h in env_hooks.split(",") if h.strip()]
elif single_hook:
    WEBHOOKS = [single_hook]
else:
    WEBHOOKS = []

logger.info(f"Configured webhooks: {WEBHOOKS}")

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
    feed_type = item.get('type', 'deals')
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
    if feed_type == 'rss':
        similar = conn.execute(
            "SELECT title FROM seen_items WHERE type='rss' AND hash!=?",
            (item['hash'],)
        ).fetchall()
        thresh = cfg.get('similarity_threshold', 95)
        for (other_title,) in similar:
            if token_set_ratio(item['title'], other_title) > thresh:
                return True
    return True
