# agentic-commerce-daily-brief

A daily news agent that scans 44 companies across LLMs, payments, retail, and big tech for agentic commerce news, ranks the results, and emails the top stories.

## What it does

Agentic commerce — AI agents that discover, decide, transact, and pay on behalf of users or businesses — is moving fast. Tracking it across LLM labs, card networks, payment infrastructure, commerce platforms, retailers, banks, and big tech means watching a lot of surface area, and most of it is noise.

This agent runs every morning at 9am UK time, searches the web for news from the last 24 hours about a curated watchlist of 44 companies, scores each article against a relevance rubric, and emails the top stories — typically around 10, capped at 15 — to a fixed inbox. Each entry includes a title, link, and a 1–2 line description. The email opens with a 2–3 line blurb summarising the day.

## Architecture

```
[GitHub Actions cron 08:00 UTC] → main.py
   │
   ├─► Load sent_urls.json — drop any URLs sent in the last 7 days
   │
   ├─► For each of 9 batches (5 companies each): Anthropic API + web_search
   │     → returns JSON array of articles per batch
   │
   ├─► Dedupe across batches by URL
   │
   ├─► Filter out anything in sent_urls.json
   │
   ├─► Single Anthropic API call: score all candidates against the relevance
   │     rubric, return top ~10 (max 15) ranked, plus a 2–3 line daily blurb
   │
   ├─► Format email body
   │
   ├─► Send via smtplib + Gmail App Password (To: work, Cc: personal)
   │
   └─► Append today's URLs to sent_urls.json, prune entries older than 7 days,
       commit the updated file back to the repo
```

Three Anthropic API calls per day in the typical case (9 search batches → 1 ranker), all on Claude Sonnet 4.6. SMTP send is plain `smtplib`, no Claude in the loop.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/reshmi-suresh/agentic-commerce-daily-brief.git
cd agentic-commerce-daily-brief
pip install -r requirements.txt
```

### 2. Create a Gmail App Password

The agent sends via SMTP using a Google App Password — this is the headless-friendly way to send from Gmail without an OAuth flow.

1. Go to your Google Account → **Security** → make sure **2-Step Verification** is on.
2. **Security** → **App passwords** → create a new password, name it `agentic-commerce-daily-brief`.
3. Copy the 16-character password. You won't see it again.

### 3. Local environment

Copy `.env.example` to `.env` and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_USER=your.personal@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

### 4. GitHub Actions secrets

For the scheduled run, add the same three values as repo secrets:
**Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

- `ANTHROPIC_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

## Running it

**Locally, print-only (no email sent):**

```bash
python main.py
```

**Locally, send the email:**

```bash
python main.py --send
```

**On GitHub Actions:**

The workflow runs automatically every day at 08:00 UTC (09:00 UK in winter, 09:00 BST is one hour off — acceptable seasonal drift).

To trigger manually for testing: **Actions** tab → **Daily Brief** → **Run workflow**.

## Watchlist

44 companies grouped into seven categories. The list is a net, not a checklist — most days most companies will have nothing, and that's the point. The full list lives in `companies.py` and is easy to edit.

- **LLM labs:** Anthropic, OpenAI, Google Gemini / DeepMind, Meta AI, xAI, Mistral
- **Card networks & payments infra:** Visa, Mastercard, American Express, Discover, Stripe, Adyen, Worldpay, PayPal, Checkout.com, Fiserv, Paze
- **Agentic commerce / fintech:** Perplexity, Ramp, Klarna
- **Commerce platforms:** Shopify, BigCommerce, commercetools, Scayle, Adobe Commerce, Salesforce Commerce Cloud
- **Retailers:** Amazon, Walmart, eBay, Etsy, Instacart, Macy's, Nordstrom, NEXT, M&S, Target
- **Big tech:** Apple, Microsoft, Meta
- **Top US banks:** JPMorgan Chase, Bank of America, Citi, Wells Fargo, Goldman Sachs

## Relevance rubric

Claude scores each article 0–10 against a written rubric in `prompts.py`. Higher scores for: AI agents that browse / shop / book / pay; agent-to-merchant or agent-to-agent payment protocols; new agent-checkout products from networks, banks, or platforms; LLM-provider partnerships with commerce or payments players; merchant tooling for being agent-discoverable; regulatory, fraud, or trust news specific to agent transactions. Lower scores for: generic AI features, generic earnings news, unrelated product launches, executive moves, or general retail / banking news with no agent or AI-commerce angle. Anything scoring below 5 is dropped.

The rubric is the single most tunable part of the system — adjust it in `prompts.py` based on what gets through.

## What I'd build next

- Source-quality boost in the rubric (Reuters, FT, WSJ, Bloomberg, The Information, PYMNTS, Finextra) without hard-restricting to a whitelist.
- Optional weekly digest variant — fewer items, more synthesis, sent Sunday evening.
- Archive past briefs to a Notion database so the history is searchable.

## Stack

Python · Anthropic API (Claude Sonnet 4.6, web_search tool) · GitHub Actions · smtplib
