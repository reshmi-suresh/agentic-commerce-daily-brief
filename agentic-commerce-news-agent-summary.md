# Agentic Commerce News Agent — Project Summary

## Goal

A scheduled agent that runs daily at 9am, scans the internet for news published in the last 24h about a watchlist of 44 companies, ranks the results by relevance to agentic commerce, and emails the top ~10 (up to 15) to Reshmi's Worldpay inbox with a CC to her personal Gmail.

## What it does

1. Wakes up at 09:00 UK time, every day.
2. For each company on the watchlist, searches for news articles published in the last 24 hours.
3. Asks Claude to score each article on agentic commerce relevance using a provided rubric.
4. Picks the most relevant articles (target ~10, hard cap 15), ordered most → least relevant. Goes above 10 only if multiple major stories clear the bar.
5. Sends an email: subject `Agentic Commerce Daily — {date}`, body opens with a 2–3 line blurb summarising the day, then a numbered list of articles with title, link, and a 1–2 line description.
6. If nothing relevant came up, send a short "nothing notable today" email rather than skipping.

## Watchlist (44 companies)

**LLM labs (5):** Anthropic, OpenAI, Google Gemini / DeepMind, Meta AI, xAI, Mistral

**Card networks & payments infra (11):** Visa, Mastercard, American Express, Discover, Stripe, Adyen, Worldpay, PayPal, Checkout.com, Fiserv, Paze

**Agentic commerce / fintech (3):** Perplexity, Ramp, Klarna

**Commerce platforms (6):** Shopify, BigCommerce, commercetools, Scayle, Adobe Commerce, Salesforce Commerce Cloud

**Retailers (10):** Amazon, Walmart, eBay, Etsy, Instacart, Macy's, Nordstrom, NEXT, M&S, Target

**Big tech (3):** Apple, Microsoft, Meta

**Top US banks (5):** JPMorgan Chase, Bank of America, Citi, Wells Fargo, Goldman Sachs

## Relevance rubric (Claude judges)

A description Claude uses to score each article 0–10. Reshmi to refine, but a starting cut:

> Score how relevant this article is to "agentic commerce" — the use of AI agents to discover, decide, transact, and pay on behalf of users or businesses. Higher scores for: AI agents that browse / shop / book / pay; agent-to-merchant or agent-to-agent payment protocols; new agent-checkout products from networks, banks, or platforms; partnerships between LLM providers and commerce / payments players; merchant tooling for being "agent-discoverable"; regulatory or fraud / trust news specific to agent transactions. Lower scores for: generic AI features, generic earnings news, unrelated product launches, executive moves, or general retail / banking news with no agent or AI-commerce angle. Score 0 if the article has nothing to do with AI or commerce.

Only articles scoring ≥ 5 make the email. Cap at 15.

## Stack

- **Runtime:** GitHub Actions, cron `0 9 * * *` (configured for UK time — note BST/GMT switching; use a runner action that handles this, or run at 08:00 UTC + accept the 1h drift in winter).
- **Language:** Python, single `main.py`.
- **News search:** Anthropic API with the `web_search` tool. One call per company, or batched groups of ~5 companies per call to reduce latency and cost.
- **Ranking & summarising:** Single Claude call passed all candidate articles, returns the top 15 with descriptions in JSON.
- **Email:** Gmail MCP server (`https://gmail.mcp.claude.com/mcp`), called from a final Claude API request that uses the MCP server to send the email.
- **Secrets:** `ANTHROPIC_API_KEY` and a Claude.ai MCP auth token (same one used by the briefing agent) stored as GitHub Actions secrets.

## Email format

```
Subject: Agentic Commerce Daily — Tue 26 May 2026

[2–3 line blurb — Claude's read on the day. What's the big story, what theme
is emerging, or "quiet day, mostly incremental updates". Factual, no hype.]

N stories from the last 24h, ranked by relevance.

1. [Title]
   https://link
   1–2 line description.

2. [Title]
   https://link
   1–2 line description.

...
```

To: `Reshmi.suresh@worldpay.com`
Cc: `reshmis93@gmail.com`
From: `reshmis93@gmail.com` (via Gmail MCP)

**Article count:** target ~10, hard cap at 15. Go above 10 only if there are genuinely major stories that all clear the relevance bar — don't pad to hit a number.

**Coverage:** the 44-company watchlist is a net, not a checklist. Most days most companies will have nothing. The point is to not miss anything — not to surface one item per company.

## Pipeline shape

```
[cron 09:00] → main.py
   │
   ├─► For each company group: Claude API + web_search → list of articles (title, url, snippet, date)
   │
   ├─► Dedupe by URL across groups
   │
   ├─► Dedupe against last 7 days of sent URLs (stored in sent_urls.json, committed to repo)
   │
   ├─► Single Claude API call: score all articles against the rubric, return top ~10 (up to 15) as JSON, plus a 2–3 line blurb summarising the day
   │
   ├─► Format email body (plain text or simple HTML)
   │
   └─► Claude API call with Gmail MCP: send email to Worldpay, CC personal
```

## Open decisions for later

- Exact time of day for cron (09:00 BST/GMT — pick one and live with the seasonal drift, or use a timezone-aware action).
- Whether to keep a running archive of past digests anywhere (Notion page? GitHub repo? Skip entirely?).
- Whether to include a "what's missing" line — e.g., companies the search returned zero results for, so you know coverage was attempted.

## Phasing

- **Phase 1:** Local script. Build the search + rank + format flow. Print the email to terminal. No cron, no sending. ~1 hour.
- **Phase 2:** Add dedupe (sent_urls.json) and Gmail MCP sending. Trigger manually. ~30 min.
- **Phase 3:** Move to GitHub Actions with cron + secrets, commit sent_urls.json back to repo from the action. ~30 min.

## Building this in Claude Code

This will live in its own GitHub repo (portfolio piece alongside the briefing agent and Sole & Thread).

**Repo name suggestion:** `agentic-commerce-news-agent`

**Suggested structure:**
```
agentic-commerce-news-agent/
├── main.py              # the whole pipeline
├── companies.py         # the 44-company watchlist as Python lists
├── prompts.py           # system prompts (search, ranking, blurb)
├── sent_urls.json       # rolling 7-day dedupe store (committed by Action)
├── requirements.txt     # anthropic
├── .github/
│   └── workflows/
│       └── daily.yml    # cron + run + commit sent_urls.json back
├── .env.example         # ANTHROPIC_API_KEY, GMAIL_MCP_TOKEN
├── .gitignore           # .env
└── README.md            # what it does, how to run, screenshot of an email
```

**Handoff prompt for Claude Code** (paste this after `cd`-ing into a new empty folder):

> Read `agentic-commerce-news-agent-summary.md` in this folder. Build Phase 1 only: a local Python script that takes the 44-company watchlist, uses the Anthropic API with the web_search tool to find news from the last 24h on each (batch ~5 companies per call), then a second Claude API call to score against the relevance rubric and return the top ~10 (max 15) as JSON plus a 2–3 line daily blurb. Format and print the email body to terminal. Do not send anything yet. Single `main.py`, watchlist in `companies.py`, prompts in `prompts.py`. Walk me through each function as you build it.

Drop the summary file into the repo, run that prompt, and Claude Code will scaffold Phase 1. Then a follow-up prompt for Phase 2 (Gmail MCP), then Phase 3 (GitHub Action).
