"""
File-To-Link Bot — Koyeb + Cloudflare Edition
Supports files up to 4 GB via Pyrogram MTProto

✅ STRING_SESSION        permanent FloodWait fix
✅ Client Pool           multiple concurrent downloads (POOL_SIZE clients)
✅ in_memory fallback    if no STRING_SESSION
✅ enums.ParseMode.HTML  Pyrogram 2.x
✅ chunk_index fix       offset=chunk NUMBER not bytes
✅ Group + Channel       works everywhere
✅ FloodWait → 503       graceful error before headers sent
✅ ConnectionReset       client disconnect handled silently
"""

import re, time, asyncio, logging, hashlib, mimetypes
from datetime import datetime
from itertools import cycle

import motor.motor_asyncio
from aiohttp import web
from pyrogram import Client, filters, idle, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FileIdInvalid, FloodWait
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("FileBot")

mongo = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
db    = mongo["filebot"]

# ── How many parallel download streams to support ─────────────────────────────
# Each client handles ONE stream_media at a time.
# POOL_SIZE = 3 means 3 simultaneous downloads without any blocking.
# Increase if you get many concurrent users. Each client uses ~10MB RAM.
POOL_SIZE  = 3
CHUNK_SIZE = 1024 * 1024

# ── Main bot — handles all messages/commands ──────────────────────────────────
bot = Client(
    "bot_main",
    api_id         = Config.API_ID,
    api_hash       = Config.API_HASH,
    bot_token      = Config.BOT_TOKEN,
    session_string = Config.STRING_SESSION or None,
    in_memory      = True,
)

# ── Stream client pool — handles parallel downloads ───────────────────────────
# All use the same bot token — Telegram allows multiple sessions per bot.
# Round-robin distributes downloads across the pool.
stream_pool: list[Client] = []
pool_cycle  = None   # set in main() after clients are started

# ══════════════════════════════════════════════════════════════════════════════
# POOL SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def init_stream_pool():
    """Start POOL_SIZE streaming clients and set up round-robin cycle."""
    global pool_cycle
    for i in range(POOL_SIZE):
        c = Client(
            f"stream_{i}",
            api_id         = Config.API_ID,
            api_hash       = Config.API_HASH,
            bot_token      = Config.BOT_TOKEN,
            session_string = Config.STRING_SESSION or None,
            in_memory      = True,
        )
        await c.start()
        stream_pool.append(c)
        log.info(f"✅ Stream client {i+1}/{POOL_SIZE} ready")
    pool_cycle = cycle(stream_pool)
    log.info(f"✅ Stream pool ready — {POOL_SIZE} concurrent downloads supported")

def get_stream_client() -> Client:
    """Return next available client from pool (round-robin)."""
    if pool_cycle:
        return next(pool_cycle)
    return bot   # fallback to main bot if pool not ready

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

    await proc.edit_text(
        f"✅ <b>Link Ready!</b>\n\n"
        f"📄 <b>File:</b> <code>{info['file_name']}</code>\n"
        f"📦 <b>Size:</b> {fmt_size(info['file_size'])}\n"
        f"🏷️ <b>Type:</b> <code>{info['mime_type']}</code>\n\n"
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
    await m.edit_text(
        f"🏓 <b>Pong!</b>  <code>{ms}ms</code>\n"
        f"🐍 Pyrogram · ⚡ Cloudflare · 🔄 {POOL_SIZE} stream clients",
        parse_mode=enums.ParseMode.HTML,
    )

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

@bot.on_message(
    filters.group &
    (filters.document | filters.video | filters.audio | filters.photo |
     filters.voice | filters.video_note | filters.animation | filters.sticker)
)
async def handle_group_file(client, msg: Message):
    await process_and_reply(client, msg)

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
# WEB SERVER
# ══════════════════════════════════════════════════════════════════════════════

async def stream_handler(request: web.Request):
    """
    GET /stream/{token}

    Uses round-robin client pool so multiple users can download simultaneously.
    Each stream_media call goes to a different Pyrogram client → no blocking.

    Fixes:
      1. Client pool     — concurrent downloads
      2. chunk_index     — offset = chunk NUMBER not bytes
      3. Prefetch        — first chunk fetched before prepare()
      4. FloodWait       — 503 returned cleanly
      5. ConnectionReset — client disconnect handled silently
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

    # Fix: chunk index not bytes
    chunk_index = byte_start // CHUNK_SIZE
    skip_bytes  = byte_start % CHUNK_SIZE

    # Pick next client from pool — round-robin for concurrency
    client = get_stream_client()

    # Prefetch first chunk before prepare() so FloodWait → clean 503
    media_iter  = client.stream_media(file_id, offset=chunk_index).__aiter__()
    first_chunk = None
    try:
        raw         = await media_iter.__anext__()
        first_chunk = raw[skip_bytes:]
    except FloodWait as e:
        log.warning(f"[FloodWait] {token} — {e.value}s")
        return web.Response(
            status  = 503,
            text    = f"Telegram rate limit. Retry in {e.value} seconds.",
            headers = {"Retry-After": str(e.value), "Access-Control-Allow-Origin": "*"},
        )
    except FileIdInvalid:
        return web.Response(status=410, text="File ID no longer valid.")
    except StopAsyncIteration:
        first_chunk = b""
    except Exception as e:
        log.error(f"Prefetch error: {e}")
        return web.Response(status=502, text="Failed to fetch from Telegram.")

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

    try:
        await response.prepare(request)
    except (ConnectionResetError, ConnectionAbortedError):
        return response

    # Write first chunk
    remaining = (byte_end - byte_start + 1) if file_size else None
    if first_chunk:
        chunk = first_chunk
        if remaining is not None:
            if len(chunk) > remaining: chunk = chunk[:remaining]
            remaining -= len(chunk)
        if chunk:
            try:
                await response.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError):
                return response

    # Stream remaining chunks
    try:
        async for chunk in media_iter:
            if remaining is not None:
                if remaining <= 0: break
                if len(chunk) > remaining: chunk = chunk[:remaining]
                remaining -= len(chunk)
            if chunk:
                await response.write(chunk)
            if remaining is not None and remaining <= 0: break
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    except FloodWait as e:
        log.warning(f"[FloodWait mid-stream] {token} — {e.value}s")
    except FileIdInvalid:
        log.error(f"FileIdInvalid mid-stream: {token}")
    except Exception as e:
        log.error(f"Stream error: {e}")

    try:
        await response.write_eof()
    except (ConnectionResetError, ConnectionAbortedError):
        pass
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
    return web.Response(content_type="text/html", text=f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>File To Link Bot</title>
<style>body{{background:#0f0f0f;color:#eee;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.b{{text-align:center}}h1{{color:#0088cc;font-size:2rem;margin-bottom:12px}}
p{{color:#888;margin-bottom:8px}}.ok{{color:#22c55e;margin-top:16px}}
.badges{{display:flex;gap:10px;justify-content:center;margin-top:12px;flex-wrap:wrap}}
.badge{{background:#1a1a2e;border:1px solid #333;border-radius:999px;
padding:4px 14px;font-size:.75rem;color:#94a3b8}}</style></head>
<body><div class="b"><h1>📁 File To Link Bot</h1>
<p>Pyrogram MTProto · 4 GB Support</p>
<div class="badges">
<span class="badge">✅ Private</span>
<span class="badge">✅ Groups</span>
<span class="badge">✅ Channels</span>
<span class="badge">🔄 {POOL_SIZE} Concurrent Streams</span>
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
    mode = "✅ String Session" if Config.STRING_SESSION else "⚠️  in_memory (set STRING_SESSION!)"
    log.info(f"🚀 Starting File-To-Link Bot [{mode}] — {POOL_SIZE} stream clients")

    # Start main bot first
    await bot.start()

    # Start stream client pool
    await init_stream_pool()

    asyncio.get_event_loop().create_task(start_web_server())

    me = await bot.get_me()
    log.info(f"✅ Bot: @{me.username} — ready ({POOL_SIZE} concurrent downloads)")

    await idle()

    # Clean shutdown
    for c in stream_pool:
        await c.stop()
    await bot.stop()

if __name__ == "__main__":
    bot.run(main())
