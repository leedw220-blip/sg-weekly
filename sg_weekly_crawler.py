#!/usr/bin/env python3
"""
SG Weekly News Crawler - Path B (Gemini free tier)
==================================================

Crawls 5 Singapore news sites every Sunday 10am SGT, ranks general-interest
stories by recency + RSS prominence over the last 7 days, summarizes via
Google Gemini (free tier, no credit card), and writes a JSON file ready to
paste into the iPhone app.

Sources & quotas:
  - The Straits Times       : 5 stories (general SG news)
  - Channel News Asia (CNA) : 5 stories (general SG news)
  - The Business Times      : 5 stories (property, retail, F&B, consumer)
  - Mothership              : 3 stories (viral, lifestyle)
  - The Smart Local (TSL)   : 3 stories (things to do, food)

Setup (free, no payment):
  1) Get a Gemini API key at https://aistudio.google.com/apikey (free, no card)
  2) pip install requests beautifulsoup4 feedparser google-genai python-dateutil
  3) export GEMINI_API_KEY=...

Free tier (Gemini 2.5 Flash-Lite): 15 requests/min, 1,000 requests/day.
This script uses ~21 requests once a week. Well within free limits.

Deploy via GitHub Actions: see .github/workflows/sunday.yml in the repo.

Output: out/week-YYYY-Wnn.json (paste into the iPhone app "+ Add new week")
"""

import os
import re
import json
import sys
import time
import datetime as dt
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser, tz
from google import genai
from google.genai import types

# ----------------------- config -----------------------

SGT = tz.gettz("Asia/Singapore")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
REQ_TIMEOUT = 25
GEMINI_MODEL = "gemini-2.5-flash-lite"  # free tier: 15 RPM, 1000 RPD

SOURCES = [
    {
        "key": "st",
        "label": "Straits Times",
        "homepage": "https://www.straitstimes.com",
        "rss": ["https://www.straitstimes.com/news/singapore/rss.xml"],
        "html_section": "https://www.straitstimes.com/singapore",
        "quota": 5,
    },
    {
        "key": "cna",
        "label": "CNA",
        "homepage": "https://www.channelnewsasia.com",
        "rss": ["https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416"],
        "html_section": "https://www.channelnewsasia.com/singapore",
        "quota": 5,
    },
    {
        "key": "bt",
        "label": "Business Times",
        "homepage": "https://www.businesstimes.com.sg",
        "rss": [
            "https://www.businesstimes.com.sg/rss/property",
            "https://www.businesstimes.com.sg/rss/consumer",
            "https://www.businesstimes.com.sg/rss/lifestyle",
        ],
        "html_section": "https://www.businesstimes.com.sg/property",
        "quota": 5,
    },
    {
        "key": "mother",
        "label": "Mothership",
        "homepage": "https://mothership.sg",
        "rss": ["https://mothership.sg/feed/"],
        "html_section": "https://mothership.sg/category/news/",
        "quota": 3,
    },
    {
        "key": "tsl",
        "label": "TSL",
        "homepage": "https://thesmartlocal.com",
        "rss": ["https://thesmartlocal.com/feed/"],
        "html_section": "https://thesmartlocal.com/",
        "quota": 3,
    },
]

# ----------------------- data model -----------------------

@dataclass
class Story:
    src: str
    label: str
    date: str
    headline: str
    url: str
    published_ts: float = 0.0
    prominence_score: int = 0
    excerpt: str = ""
    bullets: list = field(default_factory=list)

# ----------------------- fetch helpers -----------------------

def http_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            return r.text
        print(f"  ! {url} returned {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  ! {url} error: {e}", file=sys.stderr)
    return None


def parse_rss(rss_url: str, src_key: str, src_label: str, cutoff_ts: float) -> list:
    out = []
    try:
        feed = feedparser.parse(rss_url, request_headers={"User-Agent": USER_AGENT})
    except Exception as e:
        print(f"  ! RSS parse error {rss_url}: {e}", file=sys.stderr)
        return out

    for idx, entry in enumerate(feed.entries[:30]):
        try:
            published = entry.get("published") or entry.get("updated") or ""
            ts = dateparser.parse(published).astimezone(SGT).timestamp() if published else time.time()
        except Exception:
            ts = time.time()
        if ts < cutoff_ts:
            continue
        url = entry.get("link", "")
        if not url:
            continue
        headline = (entry.get("title") or "").strip()
        excerpt = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "")[:600].strip()
        out.append(Story(
            src=src_key,
            label=src_label,
            date=dt.datetime.fromtimestamp(ts, SGT).strftime("%Y-%m-%d"),
            headline=headline,
            url=url,
            published_ts=ts,
            prominence_score=max(0, 30 - idx),
            excerpt=excerpt,
        ))
    return out


def scrape_homepage_fallback(section_url: str, src_key: str, src_label: str, cutoff_ts: float) -> list:
    html = http_get(section_url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    base = "{}://{}".format(urlparse(section_url).scheme, urlparse(section_url).netloc)

    for idx, a in enumerate(soup.select("a[href]")):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        if not href or len(text) < 25 or len(text) > 220:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        if not re.search(r"/20\d{2}/|-\d{6,}|/\d{6,}|/articles?/|/news/|/story/", full):
            continue
        seen.add(full)
        out.append(Story(
            src=src_key, label=src_label,
            date=dt.datetime.now(SGT).strftime("%Y-%m-%d"),
            headline=text, url=full,
            published_ts=time.time(),
            prominence_score=max(0, 100 - idx),
        ))
        if len(out) >= 25:
            break
    return out


def collect_for_source(src: dict, cutoff_ts: float) -> list:
    print(f"[{src['label']}] fetching...")
    stories = []
    for rss_url in src.get("rss", []):
        stories.extend(parse_rss(rss_url, src["key"], src["label"], cutoff_ts))
    if not stories:
        print(f"  RSS empty, falling back to HTML")
        stories = scrape_homepage_fallback(src["html_section"], src["key"], src["label"], cutoff_ts)

    by_url = {}
    for s in stories:
        if s.url not in by_url:
            by_url[s.url] = s
        elif s.prominence_score > by_url[s.url].prominence_score:
            by_url[s.url] = s
    stories = list(by_url.values())

    now = time.time()
    def score(s: Story) -> float:
        age_days = max(0, (now - s.published_ts) / 86400.0)
        recency = max(0.0, 1.0 - (age_days / 7.0))
        prom = s.prominence_score / 30.0
        return recency * 0.6 + prom * 0.4
    stories.sort(key=score, reverse=True)
    chosen = stories[: src["quota"]]
    print(f"  selected {len(chosen)} of {len(stories)} candidates")
    return chosen


# ----------------------- article body fetch -----------------------

def fetch_article_text(url: str) -> str:
    html = http_get(url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form"]):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 40]
    body = "\n\n".join(paras[:25])
    return body[:6000]


# ----------------------- summarization via Gemini -----------------------

SYSTEM_PROMPT = (
    "You write tight, plain-English bullet summaries of Singapore news for a "
    "general weekly briefing. Output 3-5 bullets, each one short sentence "
    "(max 22 words). Lead with the most concrete fact. Use everyday language "
    "a regular Singaporean reader would use \u2014 no finance jargon, no "
    "marketing language, no editorializing, no source attribution in the "
    "bullets. Output ONLY valid JSON: a list of strings."
)

def summarize_with_gemini(client: genai.Client, story: Story, body: str) -> list:
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Headline: {story.headline}\n\n"
        f"Article body (may be truncated):\n{body or story.excerpt or '(no body available)'}\n\n"
        f"Return JSON list of 3-5 bullet strings, e.g. [\"...\",\"...\",\"...\"]."
    )
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=400,
                temperature=0.4,
            ),
        )
        text = (resp.text or "").strip()
        # Strip code fences if model added them anyway
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
        bullets = json.loads(text)
        if not isinstance(bullets, list):
            raise ValueError("not a list")
        bullets = [str(b).strip() for b in bullets if str(b).strip()][:5]
        if len(bullets) < 3:
            bullets += ["(See article for additional details.)"] * (3 - len(bullets))
        return bullets
    except Exception as e:
        print(f"  ! summarize failed for {story.url}: {e}", file=sys.stderr)
        return [
            story.excerpt[:160] if story.excerpt else "Summary unavailable.",
            "See the article for full details.",
            "(Auto-summarization failed; please review manually.)",
        ]


# ----------------------- main -----------------------

def iso_week_id(d: dt.date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_label(d: dt.date) -> str:
    monday = d - dt.timedelta(days=d.weekday())
    sunday = monday + dt.timedelta(days=6)
    if monday.month == sunday.month:
        return f"Week of {monday.strftime('%b %-d')}-{sunday.strftime('%-d, %Y')}"
    return f"Week of {monday.strftime('%b %-d')}-{sunday.strftime('%b %-d, %Y')}"


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: set GEMINI_API_KEY (get one free at https://aistudio.google.com/apikey)", file=sys.stderr)
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    now_sgt = dt.datetime.now(SGT)
    cutoff_ts = (now_sgt - dt.timedelta(days=7)).timestamp()

    all_cards = []
    for src in SOURCES:
        try:
            stories = collect_for_source(src, cutoff_ts)
        except Exception as e:
            print(f"  ! {src['label']} collection failed: {e}", file=sys.stderr)
            stories = []
        for s in stories:
            print(f"  - summarizing: {s.headline[:80]}")
            body = fetch_article_text(s.url)
            s.bullets = summarize_with_gemini(client, s, body)
            all_cards.append({
                "src": s.src,
                "date": s.date,
                "headline": s.headline,
                "bullets": s.bullets,
                "url": s.url,
            })
            time.sleep(4.5)  # stay under 15 RPM free-tier limit

    week = {
        "weekId": iso_week_id(now_sgt.date()),
        "label": week_label(now_sgt.date()),
        "fetchedAt": now_sgt.isoformat(),
        "cards": all_cards,
    }

    out_dir = Path("out")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"week-{week['weekId']}.json"
    out_path.write_text(json.dumps(week, indent=2, ensure_ascii=False))
    print(f"\nWrote {out_path} with {len(all_cards)} cards.")
    print("Paste contents into the iPhone app via '+ Add new week'.")


if __name__ == "__main__":
    main()
