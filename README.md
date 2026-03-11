# File-To-Link Bot — Solution 1 (Hybrid)
## Koyeb + Pyrogram MTProto + Cloudflare R2 + D1

---

## Architecture

```
User sends file (up to 4 GB)
        ↓
Telegram Bot (Koyeb — Pyrogram MTProto)
        ↓ streams via MTProto
Cloudflare R2 (file storage — 10GB free)
        ↓
Cloudflare D1 (metadata database — free)
        ↓
Cloudflare Worker (serves download page + streams file)
        ↓
User gets CDN download link ⚡
```

---

## Method 3 — Manual Delete Commands (Owner Only)

| Command | Action |
|---|---|
| `/storage` | Show R2 usage stats |
| `/delete {token}` | Delete specific file from R2 + D1 |
| `/clear` | Ask confirmation to delete all files |
| `/confirmclear` | Actually delete ALL files from R2 + D1 |

---

## Part 1 — Cloudflare Setup

### Step 1 — Create R2 Bucket

1. Go to https://dash.cloudflare.com
2. Click **R2** in left sidebar
3. Click **Create bucket**
4. Name: `filebot`
5. Click **Create bucket** ✅

### Step 2 — Get R2 API Keys

1. On R2 page → click **Manage R2 API Tokens**
2. Click **Create API Token**
3. Permissions: **Object Read & Write**
4. Click **Create API Token**
5. Save these — you will need them:
   - **Access Key ID**   → `R2_ACCESS_KEY`
   - **Secret Access Key** → `R2_SECRET_KEY`
   - **Account ID** (top right of R2 page) → `R2_ACCOUNT_ID`

### Step 3 — Create D1 Database

```bash
# Install wrangler
npm install -g wrangler

# Login
wrangler login

# Create D1 database
wrangler d1 create filebot-db
```
Copy the `database_id` from output → paste into `cf-worker/wrangler.toml`

```toml
# wrangler.toml — replace this line:
database_id = "REPLACE_WITH_YOUR_D1_DATABASE_ID"
# with your actual database_id
```

### Step 4 — Init D1 Schema

```bash
cd cf-worker
wrangler d1 execute filebot-db --file=schema.sql
```

### Step 5 — Deploy CF Worker

```bash
cd cf-worker
npm install
wrangler deploy
```

Note the Worker URL: `https://file-to-link-bot.yourname.workers.dev`

### Step 6 — Set API Secret

```bash
wrangler secret put API_SECRET
# Type a strong random password when prompted
# Example: mySecretKey123!@#
# Save this — you need it as API_SECRET in Koyeb
```

---

## Part 2 — Koyeb Setup

### Step 1 — Push code to GitHub

Push these files to your GitHub repo:
```
bot.py
config.py
requirements.txt
Dockerfile
```

### Step 2 — Deploy on Koyeb

1. Go to https://app.koyeb.com
2. Click **Create App** → **GitHub**
3. Select your repo
4. Builder: **Dockerfile**
5. Port: **8080**
6. Health check path: `/health`

### Step 3 — Set Environment Variables

| Variable | Where to get |
|---|---|
| `API_ID` | https://my.telegram.org |
| `API_HASH` | https://my.telegram.org |
| `BOT_TOKEN` | @BotFather on Telegram |
| `OWNER_ID` | Message @userinfobot on Telegram |
| `CF_WORKER_URL` | Your Worker URL from Step 5 above |
| `API_SECRET` | Same password you set in Step 6 above |
| `R2_ACCOUNT_ID` | Cloudflare dashboard (top right of R2 page) |
| `R2_ACCESS_KEY` | From R2 API token (Step 2) |
| `R2_SECRET_KEY` | From R2 API token (Step 2) |
| `R2_BUCKET_NAME` | `filebot` |
| `PORT` | `8080` |
| `MY_PASS` | Optional — password protect the bot |
| `UPDATES_CHANNEL` | Optional — your channel username |

### Step 4 — Deploy

Click **Deploy** → wait ~2 minutes → test:
```
https://your-app.koyeb.app/health
```
Should return: `OK`

---

## Part 3 — Keep Alive (Prevent Koyeb sleep)

Create `.github/workflows/keep_alive.yml` in your repo:

```yaml
name: Keep Alive
on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Ping
        run: curl -s ${{ secrets.KOYEB_URL }}/health
```

Add `KOYEB_URL` as a GitHub repository secret.

Also set up https://uptimerobot.com — free monitor every 5 min.

---

## Test Your Bot

1. Open Telegram → find your bot
2. Send `/start`
3. Send any file
4. Bot replies with ✅ Link Ready + token
5. Click Download → file streams from Cloudflare R2 ⚡

## Test Delete Commands

```
/storage                              → see R2 usage
/delete abc123def456abc123def456abc   → delete specific file
/clear                                → asks confirmation
/confirmclear                         → deletes everything
```

---

## Free Tier Limits Summary

| Service | Limit |
|---|---|
| CF Worker requests | 100,000/day |
| R2 storage | 10 GB total |
| R2 reads | 1,000,000/month |
| R2 writes | 1,000,000/month |
| R2 **bandwidth** | ♾️ **Unlimited — $0 forever** |
| D1 reads | 5,000,000/day |
| D1 writes | 100,000/day |

---

## File Structure

```
solution1/
├── koyeb-bot/
│   ├── bot.py           ← Python bot (Pyrogram + R2 upload)
│   ├── config.py        ← All env vars
│   ├── requirements.txt
│   └── Dockerfile
└── cf-worker/
    ├── src/
    │   └── index.js     ← CF Worker (downloads + API)
    ├── schema.sql        ← D1 database schema
    ├── wrangler.toml     ← CF config
    └── package.json
```
