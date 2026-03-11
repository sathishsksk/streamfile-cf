"""
File-To-Link Bot — Koyeb + Cloudflare Edition
Multi-DC Architecture — Zero FloodWait

WHY FloodWait HAPPENS:
  Telegram has 5 Data Centers (DC1–DC5).
  A single Pyrogram session is on ONE DC.
  When a file lives on a DIFFERENT DC, Pyrogram must call
  auth.ExportAuthorization to borrow auth for that DC.
  Telegram rate-limits this call → FloodWait 2850s.

PROPER SOLUTION — Multi-DC Clients:
  1. At startup: create one Pyrogram client per DC (DC1–DC5)
  2. Each client is pre-authorized for its own DC
  3. When streaming: decode the DC from file_id → use that DC's client
  4. Result: auth.ExportAuthorization is NEVER called during downloads

All previous fixes also included:
  ✅ in_memory=True          no session file
  ✅ enums.ParseMode.HTML    Pyrogram 2.x
  ✅ bot.run(main())         proper event loop
  ✅ chunk_index fix         offset=chunk NUMBER not bytes
  ✅ Group + Channel support
"""

import re, time, asyncio, logging, hashlib, mimetypes
from datetime import datetime

import motor.motor_asyncio
from aiohttp import web
from pyrogram import Client, filters, idle, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FileIdInvalid, FloodWait
from pyrogram.file_id import FileId
from pyrogram.raw import functions as raw_fn
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("FileBot")

mongo = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
db    = mongo["filebot"]

# ── Main bot client (receives messages, handles commands) ─────────────────────
bot = Client(
    "session",
    api_id    = Config.API_ID,
    api_hash  = Config.API_HASH,
    bot_token = Config.BOT_TOKEN,
    in_memory = True,
)

# ── DC client pool — populated at startup, keyed by DC id (1–5) ──────────────
# Each client is pre-authorized for its own DC.
# stream_media uses the matching DC client → no ExportAuthorization per request.
dc_clients: dict[int, Client] = {}

CHUNK_SIZE = 1024 * 1024   # Pyrogram always yields 1 MB chunks

# ══════════════════════════════════════════════════════════════════════════════
# MULTI-DC SETUP
# ══════════════════════════════════════════════════════════════════════════════

def get_file_dc(file_id: str) -> int:
    """Decode DC id from a Pyrogram file_id string. Returns 1–5."""
    try:
        return FileId.decode(file_id).dc_id
    except Exception:
        return 1  # safe fallback


async def create_dc_client(dc_id: int) -> Client:
    """
    Create and return a Pyrogram BOT client authorized for dc_id.

    ROOT CAUSE OF ALL PREVIOUS FAILURES:
    ─────────────────────────────────────────────────────────────────────
    All previous attempts called storage.dc_id(dc_id) AFTER connect().
    By that point Pyrogram had already connected to the DEFAULT DC (DC2).
    The exported auth bytes were for dc_id (e.g. DC1) but the connection
    was on DC2 → AUTH_BYTES_INVALID every time.

    You can see this in the logs:
      "Connected! Production DC2 - IPv4"  ← appears for DC1, DC3, DC4
      That means they all connected to DC2 instead of their target DC.

    CORRECT ORDER (this version):
      1. Export auth bytes FOR target DC from main bot session
      2. Create client
      3. Open storage               ← must happen before setting values
      4. storage.dc_id(dc_id)      ← set BEFORE connect() ← THE FIX
      5. storage.is_bot(True)      ← set BEFORE connect()
      6. client.connect()          ← NOW connects to the correct DC
      7. ImportAuthorization       ← auth bytes match the DC we're on ✅
    """
    # Step 1 — export auth from main bot FOR target DC
    exported = await bot.invoke(
        raw_fn.auth.ExportAuthorization(dc_id=dc_id)
    )

    # Step 2 — create bare client (not started yet)
    client = Client(
        f"dc{dc_id}",
        api_id    = Config.API_ID,
        api_hash  = Config.API_HASH,
        in_memory = True,
    )

    # Steps 3+4+5 — open storage, set DC and bot flag BEFORE connecting
    await client.storage.open()
    await client.storage.dc_id(dc_id)   # ← BEFORE connect() — THE KEY FIX
    await client.storage.is_bot(True)   # ← BEFORE connect()

    # Step 6 — connect NOW — Pyrogram will connect to dc_id not default DC
    await client.connect()

    # Step 7 — import auth bytes — they match dc_id we are actually on ✅
    result = await client.invoke(
        raw_fn.auth.ImportAuthorization(
            id    = exported.id,
            bytes = exported.bytes,
        )
    )

    await client.storage.user_id(result.user.id)
    await client.storage.date(0)

    log.info(f"✅ DC{dc_id} client ready")
    return client


async def init_dc_clients():
    """
    Pre-authorize ALL Telegram DCs at startup.
    Called once from main() before idle().
    After this, stream_media on any file never needs ExportAuthorization.
    """
    main_dc = await bot.storage.dc_id()
    log.info(f"🌐 Bot is on DC{main_dc} — initialising other DCs…")

    # Main DC — use the bot client directly (already authorized)
    dc_clients[main_dc] = bot

    for dc_id in [1, 2, 3, 4, 5]:
        if dc_id == main_dc:
            continue
        try:
            client = await create_dc_client(dc_id)
            dc_clients[dc_id] = client
        except FloodWait as e:
            # If Telegram rate-limits during startup, wait and retry once
            log.warning(f"⏳ DC{dc_id} FloodWait {e.value}s during init — waiting…")
            await asyncio.sleep(e.value + 3)
            try:
                client = await create_dc_client(dc_id)
                dc_clients[dc_id] = client
            except Exception as err:
                log.warning(f"⚠️ DC{dc_id} init failed after retry: {err} — fallback to main")
                dc_clients[dc_id] = bot
        except Exception as e:
            log.warning(f"⚠️ DC{dc_id} init failed: {e} — fallback to main bot")
            dc_clients[dc_id] = bot  # fallback: main bot handles it

    ready = [f"DC{k}" for k in sorted(dc_clients)]
    log.info(f"✅ DC pool ready: {', '.join(ready)}")


def get_dc_client(file_id: str) -> Client:
    """Return the correct pre-authorized client for this file's DC."""
    dc_id  = get_file_dc(file_id)
    client = dc_clients.get(dc_id) or bot   # always falls back to bot
    return client

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_size(b):
    if not b: return "Unknown"
    for u in ["B","KB","MB","GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.2f} TB"

def get_media_info(msg: Message):
    media = (msg.document or msg.video or msg.audio or msg.photo
             or msg.voice or msg.video_note or msg.sticker or msg.animation)
    if not media: return None
    name = getattr(media, "file_name", None)
    if not name:
        types = {"video":"mp4","audio":"mp3","voice":"ogg","photo":"jpg",
                 "sticker":"webp","animation":"gif","video_note":"mp4"}
        for t, ext in types.items():
            if getattr(msg, t, None):
                name = f"{t}_{msg.id}.{ext}"; break
        name = name or f"file_{msg.id}"
    return {
        "file_id"  : media.file_id,
        "file_name": name,
        "file_size": getattr(media, "file_size", 0) or 0,
        "mime_type": getattr(media, "mime_type", None)
                     or mimetypes.guess_type(name)[0]
                     or "application/octet-stream",
    }

def make_token(fid): return hashlib.sha256(fid.encode()).hexdigest()[:32]

def build_links(token):
    b = Config.CF_WORKER_URL
    return f"{b}/dl/{token}", f"{b}/file/{token}"

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

async def save_file(info, bin_msg_id):
    token = make_token(info["file_id"])
    await db["files"].update_one({"token": token},
        {"$set": {**info, "token": token, "bin_msg_id": bin_msg_id,
                  "updated_at": datetime.utcnow()}},
        upsert=True)
    return token

async def get_file(token):
    return await db["files"].find_one({"token": token}, {"_id": 0})

async def save_user(uid, name):
    await db["users"].update_one({"uid": uid},
        {"$set": {"name": name, "last": datetime.utcnow()},
         "$setOnInsert": {"joined": datetime.utcnow()}},
        upsert=True)

async def is_verified(uid):
    if not Config.MY_PASS: return True
    return bool(await db["auth"].find_one({"uid": uid}))

async def is_pending(uid):
    return bool(await db["pending"].find_one({"uid": uid}))

# ══════════════════════════════════════════════════════════════════════════════
# SHARED FILE PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

async def process_and_reply(client, msg: Message):
    info = get_media_info(msg)
    if not info: return

    proc = await msg.reply_text(
        "⏳ <b>Processing your file…</b>",
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        fwd = await client.copy_message(Config.BIN_CHANNEL, msg.chat.id, msg.id)
    except Exception as e:
        log.error(f"copy_message failed: {e}")
        await proc.edit_text(
            "❌ <b>Failed to store file.</b>\n\n"
            "Make sure the bot is <b>Admin in BIN_CHANNEL</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    bin_info = get_media_info(fwd)
    if bin_info: info["file_id"] = bin_info["file_id"]

    token = await save_file(info, fwd.id)
    dl, page = build_links(token)

    # Show which DC the file is on (useful for debugging)
    dc_id = get_file_dc(info["file_id"])

    await proc.edit_text(
        f"✅ <b>Link Ready!</b>\n\n"
        f"📄 <b>File:</b> <code>{info['file_name']}</code>\n"
        f"📦 <b>Size:</b> {fmt_size(info['file_size'])}\n"
        f"🏷️ <b>Type:</b> <code>{info['mime_type']}</code>\n"
        f"🌐 <b>Telegram DC:</b> DC{dc_id}\n\n"
        f"🔗 <b>Download Link:</b>\n<code>{dl}</code>\n\n"
        f"🌐 <b>Web Page:</b>\n<code>{page}</code>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬇️ Download", url=dl),
            InlineKeyboardButton("🌐 Web Page", url=page),
        ]]),
    )

# ══════════════════════════════════════════════════════════════════════════════
# BOT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@bot.on_message(filters.command("start") & (filters.private | filters.group))
async def cmd_start(_, msg: Message):
    await save_user(msg.from_user.id, msg.from_user.first_name)
    btns = []
    if Config.UPDATES_CHANNEL:
        btns.append([InlineKeyboardButton(
            "📢 Updates Channel",
            url=f"https://t.me/{Config.UPDATES_CHANNEL}"
        )])
    await msg.reply_text(
        f"👋 <b>Hello {msg.from_user.first_name}!</b>\n\n"
        "📁 <b>File To Link Bot</b>\n"
        "Send any file up to <b>4 GB</b> — get an instant direct download link!\n\n"
        "⚡ Powered by Pyrogram MTProto + Cloudflare CDN\n\n"
        "<b>Works in:</b> Private chats, Groups, Channels",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(btns) if btns else None,
    )

@bot.on_message(filters.command("help") & (filters.private | filters.group))
async def cmd_help(_, msg: Message):
    await msg.reply_text(
        "📖 <b>Help</b>\n\n"
        "/start — Welcome message\n"
        "/help  — This message\n"
        "/ping  — Check bot speed\n\n"
        "Send any <b>file, video, audio, photo</b> to get a download link!\n\n"
        "<b>Supported:</b> Private chats ✅ Groups ✅ Channels ✅",
        parse_mode=enums.ParseMode.HTML,
    )

@bot.on_message(filters.command("ping") & (filters.private | filters.group))
async def cmd_ping(_, msg: Message):
    t = time.time()
    m = await msg.reply_text("🏓 Pinging…")
    ms = round((time.time()-t)*1000)
    dcs = ", ".join(f"DC{k}" for k in sorted(dc_clients))
    await m.edit_text(
        f"🏓 <b>Pong!</b>  <code>{ms}ms</code>\n"
        f"🐍 Pyrogram on Koyeb + ⚡ Cloudflare\n"
        f"🌐 Active DC pool: <code>{dcs}</code>",
        parse_mode=enums.ParseMode.HTML,
    )

# ── Private file ──────────────────────────────────────────────────────────────
@bot.on_message(
    filters.private &
    (filters.document | filters.video | filters.audio | filters.photo |
     filters.voice | filters.video_note | filters.animation | filters.sticker)
)
async def handle_private_file(client, msg: Message):
    await save_user(msg.from_user.id, msg.from_user.first_name)
    if not await is_verified(msg.from_user.id):
        await db["pending"].update_one(
            {"uid": msg.from_user.id}, {"$set": {"uid": msg.from_user.id}}, upsert=True)
        await msg.reply_text(
            "🔒 <b>Bot is password protected.</b>\n\nSend the password to continue.",
            parse_mode=enums.ParseMode.HTML)
        return
    await process_and_reply(client, msg)

# ── Group file ────────────────────────────────────────────────────────────────
@bot.on_message(
    filters.group &
    (filters.document | filters.video | filters.audio | filters.photo |
     filters.voice | filters.video_note | filters.animation | filters.sticker)
)
async def handle_group_file(client, msg: Message):
    await process_and_reply(client, msg)

# ── Password input ────────────────────────────────────────────────────────────
@bot.on_message(
    filters.private & filters.text &
    ~filters.command(["start","help","ping"])
)
async def handle_text(_, msg: Message):
    if not Config.MY_PASS: return
    if not await is_pending(msg.from_user.id): return
    if msg.text == Config.MY_PASS:
        await db["auth"].update_one(
            {"uid": msg.from_user.id}, {"$set": {"uid": msg.from_user.id}}, upsert=True)
        await db["pending"].delete_one({"uid": msg.from_user.id})
        await msg.reply_text(
            "✅ <b>Password correct! Now send your file.</b>",
            parse_mode=enums.ParseMode.HTML)
    else:
        await msg.reply_text(
            "❌ <b>Wrong password.</b> Try again.",
            parse_mode=enums.ParseMode.HTML)

# ── Channel auto-link ─────────────────────────────────────────────────────────
@bot.on_message(
    filters.channel &
    (filters.document | filters.video | filters.audio |
     filters.photo | filters.animation)
)
async def handle_channel(client, msg: Message):
    info = get_media_info(msg)
    if not info: return
    try:
        fwd = await client.copy_message(Config.BIN_CHANNEL, msg.chat.id, msg.id)
        bin_info = get_media_info(fwd)
        if bin_info: info["file_id"] = bin_info["file_id"]
        token = await save_file(info, fwd.id)
        dl, page = build_links(token)
        await client.edit_message_reply_markup(
            msg.chat.id, msg.id,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬇️ Download", url=dl),
                InlineKeyboardButton("🌐 Web Page", url=page),
            ]]),
        )
    except Exception as e:
        log.error(f"Channel error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# WEB SERVER — streams file bytes to Cloudflare Worker
# ══════════════════════════════════════════════════════════════════════════════

async def stream_handler(request: web.Request):
    """
    GET /stream/{token}

    Uses Multi-DC client pool — NO auth.ExportAuthorization during streaming.

    1. Decode file's DC from file_id
    2. Look up pre-authorized client for that DC
    3. stream_media on that client — direct DC connection, zero auth overhead

    Also applies chunk_index fix:
      offset = chunk INDEX (0,1,2...) not raw bytes
    """
    token = request.match_info["token"]
    info  = await get_file(token)
    if not info:
        return web.Response(status=404, text="File not found")

    file_id   = info["file_id"]
    file_size = info.get("file_size", 0)
    mime      = info.get("mime_type", "application/octet-stream")
    file_name = info.get("file_name", "file")

    # Parse Range header
    range_hdr  = request.headers.get("Range", "")
    byte_start = 0
    byte_end   = max(file_size - 1, 0)
    if range_hdr and file_size:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if m:
            byte_start = int(m.group(1))
            byte_end   = int(m.group(2)) if m.group(2) else file_size - 1

    # chunk_index fix — offset = chunk NUMBER not bytes
    chunk_index = byte_start // CHUNK_SIZE
    skip_bytes  = byte_start % CHUNK_SIZE

    # ── KEY: pick the right DC client — no ExportAuthorization needed ─────────
    dc_client = get_dc_client(file_id)
    dc_id     = get_file_dc(file_id)
    log.info(f"Streaming {token} from DC{dc_id} (chunk={chunk_index})")
    # ─────────────────────────────────────────────────────────────────────────

    headers = {
        "Content-Type"               : mime,
        "Content-Disposition"        : f'attachment; filename="{file_name}"',
        "Accept-Ranges"              : "bytes",
        "Access-Control-Allow-Origin": "*",
    }
    if file_size:
        headers["Content-Length"] = str(byte_end - byte_start + 1)
        if range_hdr:
            headers["Content-Range"] = f"bytes {byte_start}-{byte_end}/{file_size}"

    status   = 206 if (range_hdr and file_size) else 200
    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    remaining   = (byte_end - byte_start + 1) if file_size else None
    first_chunk = True

    try:
        async for chunk in dc_client.stream_media(file_id, offset=chunk_index):
            if first_chunk:
                chunk       = chunk[skip_bytes:]
                first_chunk = False
            if remaining is not None:
                if remaining <= 0: break
                if len(chunk) > remaining: chunk = chunk[:remaining]
                remaining -= len(chunk)
            if chunk:
                await response.write(chunk)
            if remaining is not None and remaining <= 0: break

    except FileIdInvalid:
        log.error(f"FileIdInvalid: {token}")
    except Exception as e:
        log.error(f"Stream error DC{dc_id}: {e}")

    await response.write_eof()
    return response


async def info_handler(request: web.Request):
    info = await get_file(request.match_info["token"])
    if not info:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({
        "file_name": info.get("file_name"),
        "file_size": info.get("file_size"),
        "mime_type": info.get("mime_type"),
    })

async def health_handler(_):
    return web.Response(text="OK")

async def home_handler(_):
    dcs = ", ".join(f"DC{k}" for k in sorted(dc_clients)) if dc_clients else "initialising…"
    return web.Response(content_type="text/html", text=f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>File To Link Bot</title>
<style>
  body{{background:#0f0f0f;color:#eee;font-family:sans-serif;
  display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
  .b{{text-align:center}}
  h1{{color:#0088cc;font-size:2rem;margin-bottom:12px}}
  p{{color:#888;margin-bottom:8px}}
  .ok{{color:#22c55e;margin-top:16px;font-size:.9rem}}
  .badges{{display:flex;gap:10px;justify-content:center;margin-top:12px;flex-wrap:wrap}}
  .badge{{background:#1a1a2e;border:1px solid #333;border-radius:999px;
  padding:4px 14px;font-size:.75rem;color:#94a3b8}}
</style></head>
<body><div class="b">
  <h1>📁 File To Link Bot</h1>
  <p>Pyrogram MTProto · Multi-DC · 4 GB Support</p>
  <div class="badges">
    <span class="badge">✅ Private</span>
    <span class="badge">✅ Groups</span>
    <span class="badge">✅ Channels</span>
    <span class="badge">🌐 {dcs}</span>
  </div>
  <div class="ok">🟢 Running on Koyeb + Cloudflare</div>
</div></body></html>""")

def build_web_app():
    a = web.Application()
    a.router.add_get("/",               home_handler)
    a.router.add_get("/health",         health_handler)
    a.router.add_get("/stream/{token}", stream_handler)
    a.router.add_get("/info/{token}",   info_handler)
    return a

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def start_web_server():
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", Config.PORT).start()
    log.info(f"✅ Web server on port {Config.PORT}")

async def main():
    log.info("🚀 Starting File-To-Link Bot (Multi-DC Edition)")

    await bot.start()

    # ── Pre-authorize all DCs — must happen BEFORE serving any requests ───────
    await init_dc_clients()

    # Start web server after DC pool is ready
    asyncio.get_event_loop().create_task(start_web_server())

    me = await bot.get_me()
    log.info(f"✅ Bot: @{me.username} — ready on all DCs")

    await idle()

    # Clean shutdown — disconnect all DC clients
    for dc_id, client in dc_clients.items():
        if client is not bot:
            try:
                await client.disconnect()
            except Exception:
                pass

    await bot.stop()

if __name__ == "__main__":
    bot.run(main())
