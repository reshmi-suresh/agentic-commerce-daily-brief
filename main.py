"""
Agentic Commerce News Agent — Phase 2
--------------------------------------
Searches the web for agentic-commerce news across a 44-company watchlist,
ranks articles by relevance, dedupes against the last 7 days, and sends
a formatted email via Gmail SMTP.

Usage:
    python main.py

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

# Ensure UTF-8 output on Windows (emojis in progress lines)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

from companies import ALL_COMPANIES
from prompts import SEARCH_SYSTEM_PROMPT, RANKING_SYSTEM_PROMPT

load_dotenv()

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 5
DEDUP_DAYS = 7

SENT_URLS_PATH = Path(__file__).parent / "sent_urls.json"
TO_EMAIL = "Reshmi.suresh@worldpay.com"
CC_EMAIL = "reshmis93@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chunk_list(lst: list, size: int):
    """Yield successive chunks of `size` from `lst`."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def extract_json(text: str):
    """
    Pull the first JSON array or object out of a text string.
    Handles markdown code fences gracefully.
    """
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")

    # Try array first
    start = text.find("[")
    end = text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    # Fall back to object
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
# Format: { "https://...": "2026-05-26T09:00:00+00:00", ... }
# Pruned to a rolling DEDUP_DAYS window on every save.

def load_sent_urls() -> dict[str, str]:
    """Return {url: iso_timestamp} from sent_urls.json, or {} if missing."""
    if SENT_URLS_PATH.exists():
        with open(SENT_URLS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_sent_urls(sent_urls: dict[str, str]) -> None:
    """Prune entries older than DEDUP_DAYS, then write to disk."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    pruned = {
        url: ts
        for url, ts in sent_urls.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    with open(SENT_URLS_PATH, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, indent=2)


def filter_sent_articles(articles: list[dict], sent_urls: dict[str, str]) -> list[dict]:
    """Drop articles whose URLs were already sent within the last DEDUP_DAYS."""
    return [a for a in articles if a.get("url", "") not in sent_urls]


def record_sent_urls(sent_urls: dict[str, str], articles: list[dict]) -> dict[str, str]:
    """Stamp new article URLs with the current UTC time."""
    now = datetime.now(timezone.utc).isoformat()
    for art in articles:
        url = art.get("url", "")
        if url:
            sent_urls[url] = now
    return sent_urls


# ---------------------------------------------------------------------------
# Step 1 — Search
# ---------------------------------------------------------------------------

def search_news_for_batch(companies: list[str]) -> list[dict]:
    """
    Ask Claude (with server-side web_search) to find news from the last 24 h
    for a small group of companies. Returns a list of article dicts.
    """
    company_list = ", ".join(companies)
    today = datetime.now().strftime("%Y-%m-%d")

    with client.messages.stream(
        model=MODEL,
        max_tokens=4096,
        system=SEARCH_SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Search for news published in the last 24 hours about "
                    f"each of these companies: {company_list}.\n\n"
                    f"Return a JSON array of all relevant articles you find."
                ),
            }
        ],
    ) as stream:
        final = stream.get_final_message()

    # Debug: show raw response so we can see what Claude actually returned
    text_blocks = [b for b in final.content if b.type == "text"]
    if text_blocks:
        preview = text_blocks[0].text[:300].replace("\n", " ")
        print(f"           [debug] stop={final.stop_reason} raw={len(text_blocks[0].text)}chars: {preview}")
    else:
        block_types = [b.type for b in final.content]
        print(f"           [debug] no text block — content types: {block_types}")

    articles = []
    for block in final.content:
        if block.type == "text":
            result = extract_json(block.text)
            if isinstance(result, list):
                articles = result
            elif isinstance(result, dict) and "articles" in result:
                articles = result["articles"]
            break

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
                    f"found in the last 24 hours:\n\n{articles_json}\n\n"
                    f"Score each against the agentic commerce rubric. Select the top ~10 "
                    f"(hard cap 15) scoring ≥ 5. Return the result as JSON."
                ),
            }
        ],
    )

    result = {"blurb": "Nothing notable in agentic commerce today.", "articles": []}
    for block in response.content:
        if block.type == "text":
            parsed = extract_json(block.text)
            if isinstance(parsed, dict):
                result = parsed
            break

    return result


# ---------------------------------------------------------------------------
# Step 3 — Format
# ---------------------------------------------------------------------------

def build_subject() -> str:
    return f"Agentic Commerce Daily — {datetime.now().strftime('%a %d %b %Y')}"


def build_body(blurb: str, articles: list[dict]) -> str:
    """Plain-text email body (no To/Subject headers — those go to the MCP call)."""
    lines = [blurb.strip(), ""]

    if articles:
        count = len(articles)
        noun = "story" if count == 1 else "stories"
        lines.append(f"{count} {noun} from the last 24 h, ranked by relevance.")
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
            "Nothing notable today — all companies searched, "
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
    """
    Sends the email via Gmail SMTP using an App Password.
    Returns True on success, False on failure or missing credentials.
    Falls back to print-only mode if GMAIL_USER / GMAIL_APP_PASSWORD are not set.

    One-time setup:
      myaccount.google.com → Security → App passwords → create "agentic-commerce-agent"
      Store the 16-char password as GMAIL_APP_PASSWORD in .env / GitHub Actions secrets.
    """
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

    # ── Search ──────────────────────────────────────────────────────────────
    all_articles: list[dict] = []
    seen_urls: set[str] = set()

    batches = list(chunk_list(ALL_COMPANIES, BATCH_SIZE))
    total = len(batches)

    for i, batch in enumerate(batches, 1):
        print(f"  [{i:02d}/{total}] {', '.join(batch)}")
        try:
            articles = search_news_for_batch(batch)
            raw = len(articles)
            new = 0
            for art in articles:
                url = art.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_articles.append(art)
                    new += 1
            dupes = raw - new
            print(
                f"           → {raw} returned by search"
                + (f", {new} new ({dupes} duplicate URL{'s' if dupes != 1 else ''})" if dupes else f", {new} new")
            )
        except Exception as exc:
            print(f"           ⚠️  Error: {exc}")

    print()
    print(f"✅  {len(all_articles)} unique articles collected")

    # ── Dedupe against sent history ──────────────────────────────────────────
    fresh_articles = filter_sent_articles(all_articles, sent_urls)
    skipped = len(all_articles) - len(fresh_articles)
    if skipped:
        print(
            f"🔁  {skipped} article{'s' if skipped != 1 else ''} skipped "
            f"(sent within last {DEDUP_DAYS} days)"
        )
    print(f"🆕  {len(fresh_articles)} fresh articles going to ranker")

    # ── Build subject (used for both empty and normal paths) ─────────────────
    subject = build_subject()

    # ── Handle empty pipeline ────────────────────────────────────────────────
    if not fresh_articles:
        body = (
            "Nothing notable in agentic commerce today — "
            "all companies searched, no new relevant articles found."
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

    # ── Debug: print every scored article ───────────────────────────────────
    print()
    print(f"📊  All scored articles ({len(all_scored)} total):")
    for art in all_scored:
        score = art.get("score", "?")
        passed = "✓" if isinstance(score, (int, float)) and score >= 5 else "✗"
        company = art.get("company", "?")
        title = art.get("title", "Untitled")[:80]
        print(f"  {passed} [{score:>2}] {company}: {title}")

    # ── Filter and cap in Python (prompt now returns all articles) ───────────
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
            # Only record URLs as sent after a confirmed send
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
