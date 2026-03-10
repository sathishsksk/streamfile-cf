# 📁 File-To-Link Bot — Cloudflare Workers Edition

Rewritten from Python (Koyeb/Heroku polling) → JavaScript (Cloudflare Workers webhook).
**Zero sleep. Global CDN. 100% Free.**

---

## ✅ What's New vs the Original Python Bot

| Feature              | Original (Python/Koyeb) | This (Cloudflare Workers) |
|----------------------|------------------------|--------------------------|
| Sleep issue          | ❌ Sleeps when idle     | ✅ Never sleeps           |
| Cold start           | ~30 seconds            | ~0ms (instant)            |
| Mode                 | Polling                | Webhook                   |
| Language             | Python                 | JavaScript                |
| File storage         | Heroku dynos           | Cloudflare KV             |
| Cost                 | Free (limited)         | Free (100k req/day)       |
| Custom domain        | ✅                      | ✅                         |
| Channel auto-links   | ✅                      | ✅                         |
| Password protection  | ✅                      | ✅                         |

---

## 🚀 Deploy in 5 Steps

### Step 1 — Install Wrangler CLI
```bash
npm install -g wrangler
wrangler login
```

### Step 2 — Clone this repo & install deps
```bash
git clone https://github.com/YOUR_USERNAME/file-to-link-cf
cd file-to-link-cf
npm install
```

### Step 3 — Create KV Namespace
```bash
npm run setup-kv
```
This outputs something like:
```
{ binding = "FILES_KV", id = "abc123def456..." }
```
Copy the `id` value and paste it into `wrangler.toml`:
```toml
[[kv_namespaces]]
binding = "FILES_KV"
id = "abc123def456..."   ← paste here
```

### Step 4 — Set Secrets
```bash
# Required
npm run set-token       # Paste your BOT_TOKEN from @BotFather
npm run set-channel     # Paste BIN_CHANNEL id (e.g. -1001234567890)
npm run set-owner       # Your Telegram user ID

# Optional
npm run set-pass        # Password to protect the bot
wrangler secret put UPDATES_CHANNEL   # Public channel username (no @)
```

### Step 5 — Deploy
```bash
npm run deploy
```
You'll get a URL like: `https://file-to-link-bot.YOUR_NAME.workers.dev`

### Step 6 — Register Webhook (ONE TIME ONLY)
Open this in your browser:
```
https://file-to-link-bot.YOUR_NAME.workers.dev/setup
```
You'll see `"webhook_set": { "ok": true }` — done! ✅

---

## 📋 Environment Variables Reference

| Variable          | Required | Description                                      |
|-------------------|----------|--------------------------------------------------|
| `BOT_TOKEN`       | ✅        | From [@BotFather](https://t.me/BotFather)        |
| `BIN_CHANNEL`     | ✅        | Channel ID where files are stored (e.g. -100...) |
| `OWNER_ID`        | ✅        | Your Telegram user ID                            |
| `MY_PASS`         | ❌        | Password to protect the bot (optional)           |
| `UPDATES_CHANNEL` | ❌        | Public channel username shown in /start button   |

---

## 🔗 URL Routes

| Route             | Description                                  |
|-------------------|----------------------------------------------|
| `POST /webhook`   | Telegram sends all bot updates here          |
| `GET  /`          | Status / home page                           |
| `GET  /setup`     | Registers webhook with Telegram (run once)   |
| `GET  /file/{token}` | Beautiful download page (HTML)            |
| `GET  /dl/{token}`   | Direct file download / stream              |

---

## 📱 Bot Commands

| Command | Description         |
|---------|---------------------|
| /start  | Welcome message     |
| /help   | Show help           |
| /ping   | Check bot latency   |

Send any **file, video, audio, photo** → get instant download link!

---

## ⚠️ Important Notes

1. **Add bot to BIN_CHANNEL as Admin** — the bot forwards files there for permanent access
2. **Telegram file size limit** — Bots can only access files up to **20MB** via `getFile` API. Larger files need a User Bot (MTProto) approach.
3. **File links expire** — Telegram's `file_path` URLs are temporary, but we always fetch fresh ones on download, so links stay valid as long as the KV token exists (30 days by default).

---

## 🛠️ Local Development
```bash
npm run dev
# Then use ngrok or cloudflared to expose localhost for webhook testing
```

## 📊 View Logs
```bash
npm run logs
```
