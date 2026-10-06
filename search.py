"""Web search for ProAI: Tavily (agar TAVILY_API_KEY ho) warna DuckDuckGo (free, no key)."""

import os
import time
import asyncio
import logging
import datetime
from urllib.parse import urlparse

import httpx

log = logging.getLogger("proai.search")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY")
DAILY_LIMIT = int(os.environ.get("SEARCH_DAILY_LIMIT", "300"))
_cache, _count = {}, {"day": None, "n": 0}


async def _tavily(q, n, news):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.tavily.com/search",
                         headers={"Authorization": f"Bearer {TAVILY_KEY}"},
                         json={"query": q, "max_results": n, "search_depth": "basic",
                               "topic": "news" if news else "general"})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x["url"], "snippet": (x.get("content") or "")[:600]}
                for x in r.json().get("results", []) if x.get("url")]


async def _ddg(q, n, news):
    def run():
        from ddgs import DDGS
        with DDGS() as d:
            if news:
                rows = list(d.news(q, max_results=n))
                return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("body") or "")[:600]}
                        for r in rows if r.get("url")]
            rows = list(d.text(q, max_results=n))
            return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": (r.get("body") or "")[:600]}
                    for r in rows if r.get("href")]
    return await asyncio.to_thread(run)


async def web_search(query, n=5, news=False):
    q = " ".join(str(query).split())[:300]
    if not q:
        return []
    key = (q.lower(), news)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    today = datetime.date.today().isoformat()
    if _count["day"] != today:
        _count.update(day=today, n=0)
    if _count["n"] >= DAILY_LIMIT:
        raise RuntimeError("Aaj ki search limit khatam ho gayi")
    _count["n"] += 1
    rows = None
    if TAVILY_KEY:
        try:
            rows = await _tavily(q, n, news)
        except Exception:
            log.exception("Tavily fail, DuckDuckGo try kar raha hu")
    if rows is None:
        rows = await _ddg(q, n, news)
    _cache[key] = (time.time(), rows)
    return rows


def format_for_llm(rows):
    return "\n\n".join(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(rows, 1))


def format_sources(rows, k=3):
    seen, out = set(), []
    for r in rows:
        host = urlparse(r["url"]).netloc.removeprefix("www.")
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        out.append(f"{len(out) + 1}. {host}: {r['url']}")
        if len(out) >= k:
            break
    return "\n\n🔎 Sources:\n" + "\n".join(out) if out else ""
