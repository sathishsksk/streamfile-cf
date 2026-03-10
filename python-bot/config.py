import os

class Config:
    # ── Telegram ──────────────────────────────────────────
    API_ID        = int(os.environ.get("API_ID", 0))
    API_HASH      = os.environ.get("API_HASH", "")
    BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
    BIN_CHANNEL   = int(os.environ.get("BIN_CHANNEL", 0))   # e.g. -1001234567890
    OWNER_ID      = int(os.environ.get("OWNER_ID", 0))

    # ── Cloudflare Worker URL ─────────────────────────────
    # Your deployed CF Worker URL e.g. https://file-bot.yourname.workers.dev
    CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").rstrip("/")

    # ── MongoDB ───────────────────────────────────────────
    DATABASE_URL  = os.environ.get("DATABASE_URL", "")

    # ── Optional ──────────────────────────────────────────
    MY_PASS           = os.environ.get("MY_PASS", "")          # Password protection
    UPDATES_CHANNEL   = os.environ.get("UPDATES_CHANNEL", "")  # Public channel username
    PORT              = int(os.environ.get("PORT", 8080))

    # ── Stream server (this Koyeb app's public URL) ───────
    # Koyeb gives you: https://your-app-yourname.koyeb.app
    KOYEB_URL = os.environ.get("KOYEB_URL", "").rstrip("/")
