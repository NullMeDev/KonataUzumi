# Political News Bot

A zero-cost, fully serverless Discord bot that scrapes political news from government and credible media sources, deduplicates & filters for freshness, summarizes each article into three concise paragraphs, and posts color-coded embeds into your Discord channel every 24 minutes.

## 🔍 Features

- **Multi-source scraping**  
  - **RSS feeds** via `feedparser` for government, mainstream, and credible outlets  
  - **HTML scraping** for `.gov` sites lacking RSS  
- **Deduplication & TTL**  
  - SQLite (`data/deals.sqlite`) tracks seen items by hash & title similarity  
  - Time-To-Live (1 day) for re-posting expired items  
- **Summarization**  
  - Three-paragraph article summaries via OpenAI  
- **Discord embeds**  
  - Customizable colors per category (via YAML)  
- **JSON metrics**  
- **Local dev & CI**  
  - **Makefile**, **Dockerfile**, **tests/**  
- **GitHub Actions** runs every 24 minutes  

## ⚙️ Configuration

1. **Discord Webhook(s)**  
   ```bash
   export DISCORD_WEBHOOKS="https://discord.com/api/…"
   ```
2. **OpenAI API Key**  
   ```bash
   export OPENAI_API_KEY="sk-…"
   ```
3. **Source & Color YAML**  
   Edit `config/sources.yml` with your feeds and scrapers.

## 🚀 Quick Start (Local)

```bash
make deps
make test
make dry-run
make run
```
