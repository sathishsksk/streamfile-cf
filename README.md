# 📁 File-To-Link Bot — Koyeb + Cloudflare (4 GB Support)

## Architecture

```
User sends file (up to 4 GB)
        │
        ▼
┌─────────────────────┐
│  Python Bot (Koyeb) │  ← Pyrogram MTProto handles all file sizes
│  bot.py             │  ← aiohttp server streams bytes on /stream/{token}
│  port 8080          │  ← MongoDB stores token → file_id mapping
└────────┬────────────┘
         │ file stored in BIN_CHANNEL
         │ token saved to MongoDB
         │
         ▼
  Link sent to user:  https://your-worker.workers.dev/file/{token}
         │
         ▼
┌─────────────────────────┐
│  Cloudflare Worker      │  ← Serves download page
│  /file/{token}          │  ← Fetches metadata from Koyeb /info/{token}
│  /dl/{token}            │  ← Proxies stream from Koyeb /stream/{token}
└─────────────────────────┘
         │
         ▼
    User downloads at full speed via Cloudflare CDN
```

---

## PART 1 — Deploy Python Bot on Koyeb

### Step 1 — Push python-bot folder to GitHub

Create a new GitHub repo and push these files:
- `bot.py`
- `config.py`
- `requirements.txt`
- `Dockerfile`

### Step 2 — Create MongoDB (free)

1. Go to **mongodb.com/atlas** → Free tier → Create cluster
2. Database Access → Add user → copy username/password
3. Network Access → Allow from anywhere (0.0.0.0/0)
4. Connect → Drivers → copy URI like:
   `mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true`

### Step 3 — Deploy on Koyeb

1. Go to **app.koyeb.com** → Create App
2. Source: **GitHub** → select your repo
3. Builder: **Dockerfile** ← important!
4. Port: **8080**
5. Add Environment Variables:

| Variable        | Value                                      |
|-----------------|--------------------------------------------|
| `API_ID`        | From my.telegram.org                       |
| `API_HASH`      | From my.telegram.org                       |
| `BOT_TOKEN`     | From @BotFather                            |
| `BIN_CHANNEL`   | Your channel ID e.g. `-1001234567890`      |
| `OWNER_ID`      | Your Telegram user ID                      |
| `DATABASE_URL`  | MongoDB Atlas URI                          |
| `CF_WORKER_URL` | Your CF Worker URL (fill in after Part 2)  |
| `KOYEB_URL`     | Your Koyeb app URL (auto-assigned)         |
| `MY_PASS`       | Optional password                          |
| `UPDATES_CHANNEL` | Optional channel username               |

6. Deploy → wait for green ✅
7. Copy your Koyeb URL: `https://your-app-yourname.koyeb.app`
8. Test it: open `https://your-app-yourname.koyeb.app/health` → should show `OK`

---

## PART 2 — Deploy Cloudflare Worker

### Step 1 — Install Wrangler
```bash
cd cf-worker
npm install
wrangler login
```

### Step 2 — Set your Koyeb URL secret
```bash
npm run set-koyeb
# Paste: https://your-app-yourname.koyeb.app
```

### Step 3 — Deploy
```bash
npm run deploy
```
You'll get: `https://file-to-link-bot.yourname.workers.dev`

---

## PART 3 — Connect Both Together

### Update Koyeb env vars

Go back to Koyeb → your app → Environment:
- `CF_WORKER_URL` = `https://file-to-link-bot.yourname.workers.dev`

Redeploy the Koyeb app.

---

## PART 4 — Keep Koyeb Alive (No Sleep)

Add this GitHub Actions file to your repo at `.github/workflows/keep_alive.yml`:

```yaml
name: Keep Alive
on:
  schedule:
    - cron: '*/5 * * * *'
  workflow_dispatch:
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Ping Koyeb
        run: curl -s ${{ secrets.KOYEB_URL }}/health
```

Add `KOYEB_URL` as a GitHub secret in your repo settings.

---

## Quick Test

1. Start your Telegram bot → send `/start`
2. Send a file (any size up to 4 GB)
3. Bot replies with download link
4. Click link → opens Cloudflare download page
5. Click Download → file streams via Cloudflare CDN ⚡

---

## File Size Limits

| Method        | Max Size |
|---------------|----------|
| Bot API       | 20 MB ❌  |
| MTProto (this)| **4 GB** ✅ |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot not responding | Check Koyeb logs, verify BOT_TOKEN |
| "File not found" | MongoDB not connected, check DATABASE_URL |
| CF Worker error | Check KOYEB_URL secret is set correctly |
| Can't store file | Bot not admin in BIN_CHANNEL |
| 4 GB file fails | Normal — Telegram itself limits user uploads to 4 GB |
