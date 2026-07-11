"""
File-To-Link Bot — No-DB streaming version
Files stream directly using file_id encoded in the URL hash.
MongoDB only used for user tracking (non-critical, fails silently).
"""

import re, time, asyncio, logging, hashlib, mimetypes, base64, json
from itertools import cycle
from datetime import datetime
from urllib.parse import quote

import motor.motor_asyncio
from aiohttp import web
from pyrogram import Client, filters, idle, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FileIdInvalid, FloodWait
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("FileBot")

# MongoDB — optional, fails silently if unavailable
try:
    mongo = motor.motor_asyncio.AsyncIOMotorClient(
        Config.DATABASE_URL,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=5000,
    )
    db = mongo["filebot"]
    MONGO_OK = True
except Exception as e:
    log.warning(f"MongoDB init failed (non-critical): {e}")
    db = None
    MONGO_OK = False

CHUNK_SIZE = 1024 * 1024
POOL_SIZE  = 8

bot = Client(
    "session",
    api_id         = Config.API_ID,
    api_hash       = Config.API_HASH,
    bot_token      = Config.BOT_TOKEN,
    session_string = Config.STRING_SESSION or None,
    in_memory      = True,
)

stream_pool: list[Client] = []
_pool_cycle = None

async def init_stream_pool():
    global _pool_cycle
    if not Config.STRING_SESSION:
        log.warning("⚠️  STRING_SESSION not set — using single client")
        stream_pool.append(bot)
        _pool_cycle = cycle(stream_pool)
        return
    for i in range(POOL_SIZE):
        c = Client(
            f"stream_{i}",
            api_id    = Config.API_ID,
            api_hash  = Config.API_HASH,
            bot_token = Config.BOT_TOKEN,
            in_memory = True,
        )
        await c.start()
        stream_pool.append(c)
    _pool_cycle = cycle(stream_pool)
    log.info(f"✅ Stream pool ready — {POOL_SIZE} concurrent downloads")

def next_client() -> Client:
    return next(_pool_cycle) if _pool_cycle else bot

# ── Helpers ───────────────────────────────────────────────────────────────────

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
        "file_id"        : media.file_id,
        "file_unique_id" : media.file_unique_id,
        "file_name"      : name,
        "file_size"      : getattr(media, "file_size", 0) or 0,
        "mime_type"      : getattr(media, "mime_type", None)
                           or mimetypes.guess_type(name)[0]
                           or "application/octet-stream",
    }

# ── NO-DB approach: encode file_id + mime in the hash param ──────────────────
# hash = base64url( json({fid, mime, size}) )

def encode_hash(file_id: str, mime_type: str, file_size: int) -> str:
    payload = json.dumps({"f": file_id, "m": mime_type, "s": file_size})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

def decode_hash(h: str) -> dict | None:
    try:
        padding = 4 - len(h) % 4
        if padding != 4: h += "=" * padding
        return json.loads(base64.urlsafe_b64decode(h).decode())
    except Exception:
        return None

def build_links(bin_msg_id: int, file_name: str, file_id: str,
                mime_type: str, file_size: int):
    base     = Config.CF_WORKER_URL
    encoded  = quote(file_name, safe="")
    h        = encode_hash(file_id, mime_type, file_size)
    dl   = f"{base}/dl/{bin_msg_id}/{encoded}?hash={h}"
    page = f"{base}/file/{bin_msg_id}/{encoded}?hash={h}"
    return dl, page

# ── MongoDB helpers (fail silently) ──────────────────────────────────────────

async def save_user(uid, name):
    if not db: return
    try:
        await db["users"].update_one(
            {"uid": uid},
            {"$set": {"name": name, "last": datetime.utcnow()},
             "$setOnInsert": {"joined": datetime.utcnow()}},
            upsert=True
        )
    except Exception as e:
        log.warning(f"save_user failed (non-critical): {e}")

async def is_verified(uid):
    if not Config.MY_PASS: return True
    if not db: return True
    try:
        return bool(await db["auth"].find_one({"uid": uid}))
    except Exception:
        return True

async def is_pending(uid):
    if not db: return False
    try:
        return bool(await db["pending"].find_one({"uid": uid}))
    except Exception:
        return False

# ── File processor ────────────────────────────────────────────────────────────

async def process_and_reply(client, msg: Message):
    info = get_media_info(msg)
    if not info: return

    from_user = msg.from_user
    user_name = " ".join(filter(None, [
        getattr(from_user, "first_name", ""),
        getattr(from_user, "last_name", ""),
    ])) or getattr(from_user, "username", "") or str(getattr(from_user, "id", "Unknown"))
    user_id = getattr(from_user, "id", "Unknown")

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

    # Use bin channel file_id for streaming (most stable)
    bin_info = get_media_info(fwd)
    if bin_info:
        info["file_id"]  = bin_info["file_id"]
        info["mime_type"] = bin_info.get("mime_type") or info["mime_type"]

    # Build links with encoded file_id — no DB needed for streaming
    dl, page = build_links(
        fwd.id,
        info["file_name"],
        info["file_id"],
        info["mime_type"],
        info["file_size"],
    )

    # Bin channel notification
    try:
        await client.send_message(
            Config.BIN_CHANNEL,
            f"🗂 <b>New File Stored</b>\n\n"
            f"📁 <b>File:</b> <code>{info['file_name']}</code>\n"
            f"💾 <b>Size:</b> {fmt_size(info['file_size'])}\n"
            f"🔢 <b>Msg ID:</b> <code>{fwd.id}</code>\n\n"
            f"👤 <b>ʀᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :</b> {user_name}\n"
            f"🆔 <b>ᴜꜱᴇʀ ɪᴅ :</b> <code>{user_id}</code>\n\n"
            f"▶️ <b>ꜱᴛʀᴇᴀᴍ ʟɪɴᴋ :</b>\n<code>{dl}</code>\n\n"
            f"🌐 <b>ᴡᴇʙ ᴘᴀɢᴇ :</b>\n<code>{page}</code>",
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=fwd.id,
        )
    except Exception as e:
        log.warning(f"Bin notify failed: {e}")

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

# ── Bot handlers ──────────────────────────────────────────────────────────────

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
        "⚡ Powered by Pyrogram MTProto + Cloudflare CDN",
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
        "Send any <b>file, video, audio, photo</b> to get a download link!",
        parse_mode=enums.ParseMode.HTML,
    )

@bot.on_message(filters.command("ping") & (filters.private | filters.group))
async def cmd_ping(_, msg: Message):
    t = time.time()
    m = await msg.reply_text("🏓 Pinging…")
    ms = round((time.time()-t)*1000)
    await m.edit_text(
        f"🏓 <b>Pong!</b>  <code>{ms}ms</code>",
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
            {"uid": msg.from_user.id},
            {"$set": {"uid": msg.from_user.id}}, upsert=True)
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
        try:
            await db["auth"].update_one(
                {"uid": msg.from_user.id},
                {"$set": {"uid": msg.from_user.id}}, upsert=True)
            await db["pending"].delete_one({"uid": msg.from_user.id})
        except Exception: pass
        await msg.reply_text("✅ <b>Password correct! Now send your file.</b>",
                             parse_mode=enums.ParseMode.HTML)
    else:
        await msg.reply_text("❌ <b>Wrong password.</b> Try again.",
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
        if bin_info:
            info["file_id"]   = bin_info["file_id"]
            info["mime_type"] = bin_info.get("mime_type") or info["mime_type"]
        dl, page = build_links(
            fwd.id, info["file_name"],
            info["file_id"], info["mime_type"], info["file_size"]
        )
        await client.edit_message_reply_markup(
            msg.chat.id, msg.id,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬇️ Download", url=dl),
                InlineKeyboardButton("🌐 Web Page", url=page),
            ]]),
        )
    except Exception as e:
        log.error(f"Channel error: {e}")

# ── Web server ────────────────────────────────────────────────────────────────

async def stream_handler(request: web.Request):
    """
    GET /stream/{binMsgId}?hash=<encoded>
    Decodes file_id directly from hash — NO MongoDB lookup needed.
    """
    h = request.rel_url.query.get("hash", "")
    payload = decode_hash(h) if h else None

    if not payload or not payload.get("f"):
        return web.Response(status=404, text="File not found — invalid or missing hash")

    file_id   = payload["f"]
    file_size = payload.get("s", 0)
    mime      = payload.get("m", "application/octet-stream")
    file_name = request.match_info.get("file_name", "file")

    range_hdr  = request.headers.get("Range", "")
    byte_start = 0
    byte_end   = max(file_size - 1, 0)
    if range_hdr and file_size:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if m:
            byte_start = int(m.group(1))
            byte_end   = int(m.group(2)) if m.group(2) else file_size - 1

    chunk_index = byte_start // CHUNK_SIZE
    skip_bytes  = byte_start % CHUNK_SIZE

    client      = next_client()
    media_iter  = client.stream_media(file_id, offset=chunk_index).__aiter__()
    first_chunk = b""

    try:
        raw         = await media_iter.__anext__()
        first_chunk = raw[skip_bytes:]
    except FloodWait as e:
        return web.Response(
            status=503, text=f"Rate limited. Retry in {e.value}s",
            headers={"Retry-After": str(e.value), "Access-Control-Allow-Origin": "*"},
        )
    except FileIdInvalid:
        return web.Response(status=410, text="File ID expired.")
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

    remaining = (byte_end - byte_start + 1) if file_size else None

    async def write_chunk(chunk):
        nonlocal remaining
        if remaining is not None:
            if remaining <= 0: return False
            if len(chunk) > remaining: chunk = chunk[:remaining]
            remaining -= len(chunk)
        if chunk:
            try:
                await response.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError):
                return False
        return True

    if not await write_chunk(first_chunk):
        return response

    try:
        async for chunk in media_iter:
            if not await write_chunk(chunk): break
            if remaining is not None and remaining <= 0: break
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    except Exception as e:
        log.error(f"Stream error: {e}")

    try:
        await response.write_eof()
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    return response

async def info_handler(request: web.Request):
    """GET /info/{binMsgId}?hash=<encoded> — returns file metadata from hash"""
    h = request.rel_url.query.get("hash", "")
    payload = decode_hash(h) if h else None
    if not payload:
        return web.json_response({"error": "not found"}, status=404)
    file_name = request.match_info.get("file_name", "file")
    return web.json_response({
        "file_name" : file_name,
        "file_size" : payload.get("s", 0),
        "mime_type" : payload.get("m", "application/octet-stream"),
    })

async def health_handler(_):
    return web.Response(text="OK")

async def home_handler(_):
    return web.Response(content_type="text/html", text="""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>File To Link Bot</title>
<style>body{background:#0f0f0f;color:#eee;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.b{text-align:center}h1{color:#0088cc;font-size:2rem;margin-bottom:12px}
p{color:#888}.ok{color:#22c55e;margin-top:16px}</style></head>
<body><div class="b"><h1>📁 File To Link Bot</h1>
<p>Pyrogram MTProto · 4 GB Support</p>
<div class="ok">🟢 Running</div>
</div></body></html>""")

def build_web_app():
    a = web.Application()
    a.router.add_get("/",                              home_handler)
    a.router.add_get("/health",                        health_handler)
    a.router.add_get("/stream/{bin_msg_id}",           stream_handler)
    a.router.add_get("/info/{bin_msg_id}",             info_handler)
    return a

async def start_web_server():
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", Config.PORT).start()
    log.info(f"✅ Web server on port {Config.PORT}")

async def main():
    await bot.start()
    await init_stream_pool()
    asyncio.get_event_loop().create_task(start_web_server())
    me = await bot.get_me()
    log.info(f"✅ Bot: @{me.username} ready")
    await idle()
    for c in stream_pool:
        if c is not bot: await c.stop()
    await bot.stop()

if __name__ == "__main__":
    bot.run(main())
