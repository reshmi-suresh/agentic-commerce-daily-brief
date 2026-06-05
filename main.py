"""
Agentic Commerce News Agent
----------------------------
Fetches RSS feeds for a 44-company watchlist plus specialist publications,
ranks articles by relevance using Claude, dedupes against the last 7 days,
and sends a formatted email via Gmail SMTP.

Usage:
    python main.py
    python main.py --send

Requires in environment (or .env file):
    ANTHROPIC_API_KEY    — Anthropic API key
    GMAIL_USER           — sender address (e.g. reshmis93@gmail.com)
    GMAIL_APP_PASSWORD   — 16-char Google App Password (not your account password)
                           Create one at myaccount.google.com → Security → App passwords
                           (requires 2FA to be enabled on the account)
                           Omit both to run in print-only mode.
"""

import argparse
import os
import sys
import json
import re
import smtplib
import time

# Ensure UTF-8 output on Windows (emojis in progress lines)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
from dotenv import load_dotenv
from anthropic import Anthropic

from companies import ALL_COMPANIES
from prompts import RANKING_SYSTEM_PROMPT

load_dotenv()

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"
DEDUP_DAYS = 7
MAX_AGE_HOURS = 48

SENT_URLS_PATH = Path(__file__).parent / "sent_urls.json"
TO_EMAIL = "Reshmi.suresh@worldpay.com"
CC_EMAIL = "reshmis93@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Specialist feeds fetched once (not per-company)
SPECIALIST_FEEDS = [
    "https://pymnts.com/feed/",
    "https://www.finextra.com/rss/headlines.aspx",
    "https://www.paymentsdive.com/feeds/news/",
    "https://www.retaildive.com/feeds/news/",
    "https://www.modernretail.co/feed/",
    "https://techcrunch.com/feed/",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://venturebeat.com/feed/",
    "https://axios.com/feeds/feed.rss",
]

# URL suffixes/patterns to drop
_BAD_URL_SUFFIXES = ("/news", "/press", "/feed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#?\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_url_valid(url: str) -> bool:
    """Return False for URLs that are too short, bare roots, or known feed/press paths."""
    if not url or len(url) < 50:
        return False
    # Strip query string for suffix check
    path = url.split("?")[0].rstrip("/")
    if any(path.endswith(suffix) for suffix in _BAD_URL_SUFFIXES):
        return False
    # Bare domain root: scheme + "://" + host only (no real path)
    without_scheme = re.sub(r"^https?://", "", path)
    if "/" not in without_scheme:
        return False
    return True


def is_recent(entry) -> bool:
    """
    Return True if the entry is within MAX_AGE_HOURS.
    If published_parsed is missing or zero (common for Google News), keep the entry
    rather than silently dropping potentially valid articles.
    """
    pp = getattr(entry, "published_parsed", None)
    if pp is None:
        return True
    ts = time.mktime(pp)
    if ts == 0:
        return True
    age_hours = (time.time() - ts) / 3600
    return age_hours <= MAX_AGE_HOURS


def extract_json(text: str):
    """Pull the first JSON array or object out of a text string."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")

    start = text.find("[")
    end = text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Sent-URL dedupe store  (sent_urls.json)
# ---------------------------------------------------------------------------

def load_sent_urls() -> dict[str, str]:
    if SENT_URLS_PATH.exists():
        with open(SENT_URLS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_sent_urls(sent_urls: dict[str, str]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    pruned = {
        url: ts
        for url, ts in sent_urls.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    with open(SENT_URLS_PATH, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, indent=2)


def filter_sent_articles(articles: list[dict], sent_urls: dict[str, str]) -> list[dict]:
    return [a for a in articles if a.get("url", "") not in sent_urls]


def record_sent_urls(sent_urls: dict[str, str], articles: list[dict]) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    for art in articles:
        url = art.get("url", "")
        if url:
            sent_urls[url] = now
    return sent_urls


# ---------------------------------------------------------------------------
# Step 1 — RSS Search
# ---------------------------------------------------------------------------

def fetch_rss_feed(url: str) -> list:
    """Fetch a single RSS feed. Returns list of entries, or [] on any error."""
    try:
        parsed = feedparser.parse(url)
        return parsed.entries or []
    except Exception:
        return []


def entry_to_article(entry, company: str) -> dict | None:
    """Convert a feedparser entry to our article dict. Returns None if invalid."""
    url = getattr(entry, "link", "") or ""
    if not is_url_valid(url):
        return None
    if not is_recent(entry):
        return None

    title = strip_html(getattr(entry, "title", "") or "")
    summary = strip_html(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
    # Truncate summary to keep ranker payload manageable
    if len(summary) > 300:
        summary = summary[:297] + "..."

    pp = getattr(entry, "published_parsed", None)
    if pp:
        try:
            published_date = datetime(*pp[:6]).strftime("%Y-%m-%d")
        except Exception:
            published_date = ""
    else:
        published_date = ""

    return {
        "title": title,
        "url": url,
        "snippet": summary,
        "company": company,
        "published_date": published_date,
    }


def fetch_all_articles(companies: list[str]) -> list[dict]:
    """
    Fetch RSS feeds for all companies plus specialist feeds.
    Returns a deduplicated list of article dicts.
    """
    seen_urls: set[str] = set()
    articles: list[dict] = []

    def add_entry(entry, company: str):
        art = entry_to_article(entry, company)
        if art and art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            articles.append(art)

    # Per-company feeds
    print("  Fetching per-company RSS feeds …")
    for company in companies:
        q_google = re.sub(r"\s+", "+", company) + "+AI+payments+commerce"
        q_bing = re.sub(r"\s+", "+", company) + "+agentic+commerce"
        google_url = f"https://news.google.com/rss/search?q={q_google}&hl=en-GB&gl=GB&ceid=GB:en"
        bing_url = f"https://www.bing.com/news/search?q={q_bing}&format=rss"

        before = len(articles)
        for entry in fetch_rss_feed(google_url):
            add_entry(entry, company)
        for entry in fetch_rss_feed(bing_url):
            add_entry(entry, company)
        gained = len(articles) - before
        print(f"    {company}: +{gained}")

    # Specialist feeds
    print("  Fetching specialist feeds …")
    for feed_url in SPECIALIST_FEEDS:
        before = len(articles)
        for entry in fetch_rss_feed(feed_url):
            add_entry(entry, "")
        gained = len(articles) - before
        label = feed_url.split("/")[2]
        print(f"    {label}: +{gained}")

    return articles


# ---------------------------------------------------------------------------
# Step 2 — Rank
# ---------------------------------------------------------------------------

def rank_articles(all_articles: list[dict]) -> dict:
    """
    Single Claude call: score all candidate articles, pick top ≤15 scoring ≥5.
    Returns {"blurb": "...", "articles": [...]}.
    """
    today = datetime.now().strftime("%A %d %B %Y")
    articles_json = json.dumps(all_articles, indent=2)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=RANKING_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Here are {len(all_articles)} candidate articles "
                    f"from the last 48 hours:\n\n{articles_json}\n\n"
                    f"Score each against the agentic commerce rubric. Select the top ~10 "
                    f"(hard cap 15) scoring ≥ 5. Return the result as JSON."
                ),
            }
        ],
    )

    result = {"blurb": "Nothing notable in agentic commerce today.", "articles": []}
    for block in response.content:
        if block.type == "text":
            text = block.text
            text = re.sub(r"```(?:json)?\s*", "", text)
            text = text.replace("```", "")
            start = text.find("{")
            end = text.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    parsed = json.loads(text[start:end])
                    if isinstance(parsed, dict) and "articles" in parsed:
                        result = parsed
                    else:
                        print(f"[debug ranker] unexpected shape: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}")
                except json.JSONDecodeError as exc:
                    print(f"[debug ranker] JSONDecodeError: {exc}")
            else:
                print("[debug ranker] no JSON object found in response")
            break

    return result


# ---------------------------------------------------------------------------
# Step 3 — Format
# ---------------------------------------------------------------------------

def build_subject() -> str:
    return f"Agentic Commerce Daily — {datetime.now().strftime('%a %d %b %Y')}"


def build_body(blurb: str, articles: list[dict]) -> str:
    lines = [blurb.strip(), ""]

    if articles:
        count = len(articles)
        noun = "story" if count == 1 else "stories"
        lines.append(f"{count} {noun} from the last 48 h, ranked by relevance.")
        lines.append("")
        for i, art in enumerate(articles, 1):
            lines.append(f"{i}. {art.get('title', 'Untitled')}")
            lines.append(f"   {art.get('url', '')}")
            desc = art.get("description") or art.get("snippet", "")
            if desc:
                lines.append(f"   {desc}")
            lines.append("")
    else:
        lines.append(
            "Nothing notable today — all sources fetched, "
            "nothing cleared the relevance bar."
        )
        lines.append("")

    return "\n".join(lines)


def print_email_preview(subject: str, body: str) -> None:
    divider = "─" * 60
    print(f"Subject : {subject}")
    print(f"To      : {TO_EMAIL}")
    print(f"Cc      : {CC_EMAIL}")
    print()
    print(divider)
    print()
    print(body)
    print(divider)


# ---------------------------------------------------------------------------
# Step 4 — Send via Gmail SMTP
# ---------------------------------------------------------------------------

def send_email_smtp(subject: str, body: str) -> bool:
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_password:
        print("⚠️   GMAIL_USER or GMAIL_APP_PASSWORD not set — print-only mode, email not sent.")
        return False

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = TO_EMAIL
    msg["Cc"] = CC_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    recipients = [TO_EMAIL, CC_EMAIL]

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipients, msg.as_string())
        return True
    except smtplib.SMTPException as exc:
        print(f"❌   SMTP error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Agentic Commerce News Agent")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send the email via SMTP (default: print-only dry run)",
    )
    args = parser.parse_args()

    print()
    print("🤖  Agentic Commerce News Agent")
    print(f"📅  {datetime.now().strftime('%A %d %B %Y, %H:%M')}")
    print(f"🏢  Watchlist: {len(ALL_COMPANIES)} companies")
    print(f"📬  Mode: {'send' if args.send else 'dry run (--send to actually send)'}")
    print()

    # ── Load dedupe store ────────────────────────────────────────────────────
    sent_urls = load_sent_urls()
    print(f"📋  {len(sent_urls)} URLs in dedupe store (rolling {DEDUP_DAYS}-day window)")
    print()

    # ── Search via RSS ───────────────────────────────────────────────────────
    print("🔍  Fetching RSS feeds …")
    all_articles = fetch_all_articles(ALL_COMPANIES)
    print()
    print(f"✅  {len(all_articles)} unique articles collected (date-filtered, URL-filtered, deduped)")

    # ── Dedupe against sent history ──────────────────────────────────────────
    fresh_articles = filter_sent_articles(all_articles, sent_urls)
    skipped = len(all_articles) - len(fresh_articles)
    if skipped:
        print(
            f"🔁  {skipped} article{'s' if skipped != 1 else ''} skipped "
            f"(sent within last {DEDUP_DAYS} days)"
        )
    print(f"🆕  {len(fresh_articles)} fresh articles going to ranker")

    # ── Build subject ────────────────────────────────────────────────────────
    subject = build_subject()

    # ── Handle empty pipeline ────────────────────────────────────────────────
    if not fresh_articles:
        body = (
            "Nothing notable in agentic commerce today — "
            "all sources fetched, no new relevant articles found."
        )
        print()
        print_email_preview(subject, body)
        if args.send:
            print()
            print("📤  Sending 'nothing notable' email via SMTP …")
            if send_email_smtp(subject, body):
                print("✅  Sent.")
        else:
            print()
            print("📋  Dry run — pass --send to actually send.")
        return

    # ── Rank ────────────────────────────────────────────────────────────────
    print()
    print("🏆  Ranking by agentic commerce relevance …")
    try:
        result = rank_articles(fresh_articles)
    except Exception as exc:
        print(f"❌  Ranking failed: {exc}")
        raise

    blurb = result.get("blurb", "")
    all_scored = sorted(
        result.get("articles", []),
        key=lambda a: a.get("score", 0),
        reverse=True,
    )

    # ── Debug: print every scored article ────────────────────────────────────
    print()
    print(f"📊  All scored articles ({len(all_scored)} total):")
    for art in all_scored:
        score = art.get("score", "?")
        passed = "✓" if isinstance(score, (int, float)) and score >= 5 else "✗"
        company = art.get("company", "?")
        title = art.get("title", "Untitled")[:80]
        print(f"  {passed} [{score:>2}] {company}: {title}")

    # ── Filter and cap ───────────────────────────────────────────────────────
    top_articles = [a for a in all_scored if a.get("score", 0) >= 5][:15]
    print()
    print(f"✅  {len(top_articles)} article{'s' if len(top_articles) != 1 else ''} passed score ≥ 5 (cap 15)")

    # ── Format & preview ─────────────────────────────────────────────────────
    body = build_body(blurb, top_articles)
    print()
    print_email_preview(subject, body)

    # ── Send (or dry-run) ────────────────────────────────────────────────────
    if args.send:
        print()
        print("📤  Sending via Gmail SMTP …")
        sent = send_email_smtp(subject, body)
        if sent:
            sent_urls = record_sent_urls(sent_urls, top_articles)
            save_sent_urls(sent_urls)
            print(f"✅  Email sent. {len(top_articles)} URLs added to dedupe store.")
        else:
            print("⚠️   Email not sent — dedupe store not updated.")
    else:
        print()
        print("📋  Dry run — pass --send to actually send.")


if __name__ == "__main__":
    main()
