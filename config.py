import os

class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    API_ID        = int(os.environ.get("API_ID", 0))
    API_HASH      = os.environ.get("API_HASH", "")
    BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
    OWNER_ID      = int(os.environ.get("OWNER_ID", 0))
    BIN_CHANNEL   = int(os.environ.get("BIN_CHANNEL", 0))

    # ── String Session (REQUIRED to fix FloodWait permanently) ───────────────
    # Generate once using gen_session.py then paste as Koyeb env var
    STRING_SESSION = os.environ.get("STRING_SESSION", "")

    # ── Cloudflare ────────────────────────────────────────────────────────────
    CF_WORKER_URL  = os.environ.get("CF_WORKER_URL", "").rstrip("/")
    KOYEB_URL      = os.environ.get("KOYEB_URL", "").rstrip("/")

    # ── Optional ──────────────────────────────────────────────────────────────
    MY_PASS        = os.environ.get("MY_PASS", "")
    UPDATES_CHANNEL= os.environ.get("UPDATES_CHANNEL", "")
    DATABASE_URL   = os.environ.get("DATABASE_URL", "")
    PORT           = int(os.environ.get("PORT", 8080))
