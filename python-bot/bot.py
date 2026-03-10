"""
File-To-Link Bot  ─  Koyeb + Cloudflare Edition
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pyrogram (MTProto) → supports files up to 4 GB
aiohttp web server → /stream/{token} streams bytes to CF Worker
"""

import re, time, asyncio, logging, hashlib, mimetypes
from datetime import datetime

import motor.motor_asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FileIdInvalid
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("FileBot")

# ── MongoDB ───────────────────────────────────────────────────────────────────
mongo = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
db    = mongo["filebot"]

# ── Pyrogram Bot ──────────────────────────────────────────────────────────────
bot = Client("session", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

# ══════════════════ HELPERS ═══════════════════════════════════════════════════

def fmt_size(b):
    if not b: return "Unknown"
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.2f} TB"

def get_media_info(msg: Message):
    media = (msg.document or msg.video or msg.audio or msg.photo
             or msg.voice or msg.video_note or msg.sticker or msg.animation)
    if not media:
        return None
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

def make_token(file_id: str) -> str:
    return hashlib.sha256(file_id.encode()).hexdigest()[:32]

def build_links(token: str):
    base = Config.CF_WORKER_URL
    return f"{base}/dl/{token}", f"{base}/file/{token}"

# ══════════════════ DATABASE ══════════════════════════════════════════════════

async def save_file(info: dict, bin_msg_id: int) -> str:
    token = make_token(info["file_id"])
    await db["files"].update_one(
        {"token": token},
        {"$set": {**info, "token": token, "bin_msg_id": bin_msg_id, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return token

async def get_file(token: str):
    return await db["files"].find_one({"token": token}, {"_id": 0})

async def save_user(uid: int, name: str):
    await db["users"].update_one(
        {"uid": uid},
        {"$set": {"name": name, "last": datetime.utcnow()}, "$setOnInsert": {"joined": datetime.utcnow()}},
        upsert=True,
    )

async def is_verified(uid: int) -> bool:
    if not Config.MY_PASS: return True
    return bool(await db["auth"].find_one({"uid": uid}))

async def is_pending(uid: int) -> bool:
    return bool(await db["pending"].find_one({"uid": uid}))

# ══════════════════ BOT HANDLERS ══════════════════════════════════════════════

@bot.on_message(filters.command("start") & filters.private)
async def cmd_start(_, msg: Message):
    await save_user(msg.from_user.id, msg.from_user.first_name)
    btns = []
    if Config.UPDATES_CHANNEL:
        btns.append([InlineKeyboardButton("📢 Updates Channel", url=f"https://t.me/{Config.UPDATES_CHANNEL}")])
    await msg.reply_text(
        f"👋 <b>Hello {msg.from_user.first_name}!</b>\n\n"
        "📁 <b>File To Link Bot</b>\n"
        "Send any file up to <b>4 GB</b> and get an instant download link!\n\n"
        "⚡ MTProto + Cloudflare CDN",
        parse_mode="html",
        reply_markup=InlineKeyboardMarkup(btns) if btns else None,
    )

@bot.on_message(filters.command("ping") & filters.private)
async def cmd_ping(_, msg: Message):
    t = time.time()
    m = await msg.reply_text("🏓 Pinging…")
    ms = round((time.time() - t) * 1000)
    await m.edit_text(f"🏓 <b>Pong!</b>  <code>{ms}ms</code>\n🐍 Pyrogram on Koyeb + ⚡ Cloudflare", parse_mode="html")

@bot.on_message(filters.command("help") & filters.private)
async def cmd_help(_, msg: Message):
    await msg.reply_text(
        "📖 <b>Commands</b>\n\n"
        "/start — Welcome\n/help — This message\n/ping — Speed check\n\n"
        "Send any file to get a download link.",
        parse_mode="html",
    )

# ── File handler ──────────────────────────────────────────────────────────────
@bot.on_message(
    filters.private &
    (filters.document | filters.video | filters.audio | filters.photo |
     filters.voice | filters.video_note | filters.animation | filters.sticker)
)
async def handle_file(client, msg: Message):
    await save_user(msg.from_user.id, msg.from_user.first_name)

    if not await is_verified(msg.from_user.id):
        await db["pending"].update_one({"uid": msg.from_user.id}, {"$set": {"uid": msg.from_user.id}}, upsert=True)
        await msg.reply_text("🔒 <b>Bot is password protected.</b>\nSend the password to continue.", parse_mode="html")
        return

    info = get_media_info(msg)
    if not info: return

    proc = await msg.reply_text("⏳ <b>Processing…</b>", parse_mode="html")

    try:
        fwd = await client.copy_message(Config.BIN_CHANNEL, msg.chat.id, msg.id)
    except Exception as e:
        log.error(f"copy_message failed: {e}")
        await proc.edit_text("❌ <b>Failed.</b> Is bot Admin in BIN_CHANNEL?", parse_mode="html")
        return

    # Always use fresh file_id from BIN_CHANNEL
    bin_info = get_media_info(fwd)
    if bin_info: info["file_id"] = bin_info["file_id"]

    token = await save_file(info, fwd.id)
    dl, page = build_links(token)

    await proc.edit_text(
        f"✅ <b>Link Ready!</b>\n\n"
        f"📄 <b>File:</b> <code>{info['file_name']}</code>\n"
        f"📦 <b>Size:</b> {fmt_size(info['file_size'])}\n"
        f"🏷️ <b>Type:</b> <code>{info['mime_type']}</code>\n\n"
        f"🔗 <b>Download:</b>\n<code>{dl}</code>\n\n"
        f"🌐 <b>Web Page:</b>\n<code>{page}</code>",
        parse_mode="html",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬇️ Download", url=dl),
            InlineKeyboardButton("🌐 Web Page", url=page),
        ]]),
    )

# ── Password input ────────────────────────────────────────────────────────────
@bot.on_message(filters.private & filters.text & ~filters.command(["start","help","ping"]))
async def handle_text(_, msg: Message):
    if not Config.MY_PASS: return
    if not await is_pending(msg.from_user.id): return
    if msg.text == Config.MY_PASS:
        await db["auth"].update_one({"uid": msg.from_user.id}, {"$set": {"uid": msg.from_user.id}}, upsert=True)
        await db["pending"].delete_one({"uid": msg.from_user.id})
        await msg.reply_text("✅ <b>Correct! Now send your file.</b>", parse_mode="html")
    else:
        await msg.reply_text("❌ <b>Wrong password.</b> Try again.", parse_mode="html")

# ── Channel auto-link ─────────────────────────────────────────────────────────
@bot.on_message(
    filters.channel &
    (filters.document | filters.video | filters.audio | filters.photo | filters.animation)
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
        log.error(f"Channel handler error: {e}")

# ══════════════════ WEB SERVER (streams to CF Worker) ════════════════════════

async def stream_handler(request: web.Request):
    """
    GET /stream/{token}
    Streams file bytes from Telegram via Pyrogram.
    Called by the Cloudflare Worker. Supports Range (video seek).
    """
    token = request.match_info["token"]
    info  = await get_file(token)
    if not info:
        return web.Response(status=404, text="File not found")

    file_id   = info["file_id"]
    file_size = info.get("file_size", 0)
    mime      = info.get("mime_type", "application/octet-stream")
    file_name = info.get("file_name", "file")

    # Parse Range header for video seeking
    range_hdr = request.headers.get("Range", "")
    start, end = 0, file_size - 1 if file_size else 0

    if range_hdr and file_size:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if m:
            start = int(m.group(1))
            end   = int(m.group(2)) if m.group(2) else file_size - 1

    headers = {
        "Content-Type"               : mime,
        "Content-Disposition"        : f'attachment; filename="{file_name}"',
        "Accept-Ranges"              : "bytes",
        "Access-Control-Allow-Origin": "*",
    }
    if file_size:
        headers["Content-Length"] = str(end - start + 1)
        if range_hdr:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    status   = 206 if (range_hdr and file_size) else 200
    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    remaining = (end - start + 1) if file_size else None
    try:
        async for chunk in bot.stream_media(file_id, offset=start):
            if remaining is not None:
                if remaining <= 0: break
                chunk = chunk[:remaining]
                remaining -= len(chunk)
            await response.write(chunk)
    except FileIdInvalid:
        log.error(f"FileIdInvalid: {token}")
    except Exception as e:
        log.error(f"Stream error: {e}")

    await response.write_eof()
    return response


async def info_handler(request: web.Request):
    """GET /info/{token} → JSON file metadata for CF Worker."""
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
    return web.Response(content_type="text/html", text="""<!DOCTYPE html>
<html><head><title>File Bot</title>
<style>body{background:#0f0f0f;color:#eee;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.b{text-align:center}h1{color:#0088cc}p{color:#888}</style></head>
<body><div class="b"><h1>📁 File To Link</h1>
<p>Pyrogram MTProto · 4 GB Support</p>
<p style="color:#22c55e">✅ Running on Koyeb</p></div></body></html>""")


def build_web_app():
    a = web.Application()
    a.router.add_get("/",               home_handler)
    a.router.add_get("/health",         health_handler)
    a.router.add_get("/stream/{token}", stream_handler)
    a.router.add_get("/info/{token}",   info_handler)
    return a


# ══════════════════ MAIN ══════════════════════════════════════════════════════

async def main():
    log.info("🚀 Starting File-To-Link Bot (4 GB support)")
    await bot.start()
    me = await bot.get_me()
    log.info(f"✅ Bot: @{me.username}")

    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", Config.PORT).start()
    log.info(f"✅ Web server on port {Config.PORT}")

    await asyncio.Event().wait()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
