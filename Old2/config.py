import os

class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    API_ID      = int(os.environ.get("API_ID", 0))
    API_HASH    = os.environ.get("API_HASH", "")
    BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
    BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL", 0))
    OWNER_ID    = int(os.environ.get("OWNER_ID", 0))

    # ── Cloudflare Worker URL ─────────────────────────────────────────────────
    # e.g. https://file-to-link-bot.yourname.workers.dev
    CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").rstrip("/")

    # ── Koyeb App URL ─────────────────────────────────────────────────────────
    # e.g. https://your-app-yourname.koyeb.app
    KOYEB_URL = os.environ.get("KOYEB_URL", "").rstrip("/")

    # ── MongoDB ───────────────────────────────────────────────────────────────
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # ── Optional ──────────────────────────────────────────────────────────────
    MY_PASS         = os.environ.get("MY_PASS", "")
    UPDATES_CHANNEL = os.environ.get("UPDATES_CHANNEL", "")
    PORT            = int(os.environ.get("PORT", 8080))
