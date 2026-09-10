# Facebook Content Health Monitor — Telegram Bot

Monitors Facebook objects **you have Graph API access to** (your own Pages,
posts, videos, photos) and alerts you the instant one becomes inaccessible.
Uses only the official `https://graph.facebook.com` REST API — no scraping,
no headless browser, no bypassing of Facebook's login/anti-bot systems.

## Important scope limitation

The Graph API only returns data for objects your access token is authorized
to read (things you own/manage, or fully public Pages, depending on the
token's permissions). It **cannot** be used to monitor arbitrary strangers'
posts or profiles — that's by design, and it's what makes this approach
compliant with Facebook's Platform Terms.

## 1. Get a long-lived access token

1. Go to the [Graph API Explorer](https://developers.facebook.com/tools/explorer/).
2. Select your app, generate a User or Page access token with the
   permissions you need (e.g. `pages_read_engagement` for a Page you manage).
3. Exchange the short-lived token for a long-lived one (60 days) using the
   [access token debugger](https://developers.facebook.com/tools/debug/accesstoken/)
   or the `oauth/access_token` endpoint with `grant_type=fb_exchange_token`.
4. For Pages, consider generating a Page access token, which doesn't expire
   as long as the associated user token stays valid.
5. You are responsible for refreshing this token before it expires — the
   bot will report `is_auth_error` alerts (not false DEAD alerts) if it
   stops working.

## 2. Configure environment variables

Copy `.env.example` to `.env` and fill in:

```
BOT_TOKEN=...          # from @BotFather
ADMIN_ID=...            # your numeric Telegram user id
FB_ACCESS_TOKEN=...     # from step 1
GRAPH_API_VERSION=v19.0
DB_PATH=bot.db
CHECK_INTERVAL_SECONDS=60
DEFAULT_TIMEZONE=Asia/Dhaka
```

## 3. Run locally

```bash
pip install -r requirements.txt
python bot.py
```

## 4. Deploy on Railway

1. Push this repo to GitHub and create a new Railway project from it.
2. In Railway → Variables, add the same variables as `.env` above.
3. Railway's Nixpacks builder auto-detects `requirements.txt` and runs
   `python bot.py` — no extra browser system packages are needed since this
   version doesn't use Playwright/Chromium at all.

## How object IDs are resolved

Send the bot any of:
- A bare numeric ID: `100012345678901`
- A classic post ID: `100012345678901_998877665544`
- A page username: `mypageusername`
- A URL such as `facebook.com/mypage/posts/123456789`, `facebook.com/mypage`,
  `facebook.com/profile.php?id=...`, `facebook.com/permalink.php?story_fbid=...&id=...`

The bot extracts the best-guess Graph object ID and immediately queries the
API to confirm it. If extraction is ambiguous, it's usually more reliable to
just paste the raw numeric object ID directly.

## Files

| File | Purpose |
|---|---|
| `config.py` | Environment variable loading & constants |
| `database.py` | aiosqlite schema + CRUD for users/objects |
| `graph_checker.py` | Graph API calls, error classification, ID extraction |
| `bot.py` | Telegram handlers, UI cards, admin approval, background worker |

## Status semantics

- **ACTIVE** — last check succeeded, or the last check was a transient error
  (network hiccup, rate limit) — anti-glitch protection means transient
  errors never flip a link to DEAD.
- **DEAD** — Graph API returned an error code that specifically means the
  object doesn't exist / was deleted (e.g. code 100, code 21).
- **Auth errors** (invalid/expired token, code 190) never mark anything DEAD
  — they're logged and should prompt you to refresh `FB_ACCESS_TOKEN`.
- **STOPPED** — user manually stopped monitoring via the 🔴 Stop button.
