import asyncio
import os
import math
import logging
import secrets
from datetime import datetime

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, FileIdInvalid, FileReferenceExpired
from pyrogram.types import Message
from motor.motor_asyncio import AsyncIOMotorClient

from config import (
    API_ID, API_HASH, BOT_TOKEN, BIN_CHANNEL,
    OWNER_ID, DATABASE_URL, CF_WORKER_URL,
    KOYEB_URL, MY_PASS, UPDATES_CHANNEL
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── MongoDB ─────────────────────────────────────────────────────────────────
mongo = AsyncIOMotorClient(DATABASE_URL)
db = mongo["filestorebot"]
col = db["files"]

# ─── Pyrogram Client ─────────────────────────────────────────────────────────
app = Client(
    "filestore",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ─── DC Auth Cache  (fixes ExportAuthorization FloodWait) ────────────────────
# Reuse exported auth per DC instead of re-exporting on every /stream request
_dc_auth_cache: dict[int, object] = {}


async def get_exported_auth(dc_id: int):
    """Cache ExportAuthorization per DC to avoid repeated flood waits."""
    if dc_id in _dc_auth_cache:
        return _dc_auth_cache[dc_id]
    while True:
        try:
            from pyrogram.raw.functions.auth import ExportAuthorization
            exported = await app.invoke(ExportAuthorization(dc_id=dc_id))
            _dc_auth_cache[dc_id] = exported
            log.info(f"Cached auth for DC {dc_id}")
            return exported
        except FloodWait as e:
            log.warning(f"FloodWait {e.value}s on ExportAuthorization DC {dc_id} — sleeping…")
            await asyncio.sleep(e.value + 2)


async def stream_media_safe(message: Message, offset: int = 0, limit: int | None = None):
    """
    stream_media wrapper with:
      - FloodWait retry (sleeps exact wait + 2s buffer)
      - FileReferenceExpired refresh
      - Exponential back-off for unexpected errors
    """
    retries = 0
    max_retries = 5

    while retries < max_retries:
        try:
            async for chunk in app.stream_media(message, offset=offset, limit=limit):
                yield chunk
            return
        except FloodWait as e:
            log.warning(f"FloodWait {e.value}s during stream — sleeping…")
            # Invalidate DC cache so auth is re-fetched after wait
            _dc_auth_cache.clear()
            await asyncio.sleep(e.value + 2)
            retries += 1
        except FileReferenceExpired:
            log.warning("FileReferenceExpired — refreshing message…")
            try:
                message = await app.get_messages(BIN_CHANNEL, message.id)
            except Exception as ex:
                log.error(f"Could not refresh message: {ex}")
                return
            retries += 1
        except FileIdInvalid:
            log.error("FileIdInvalid — cannot stream this file.")
            return
        except Exception as e:
            wait = 2 ** retries
            log.error(f"Stream error (retry {retries}/{max_retries}): {e} — waiting {wait}s")
            await asyncio.sleep(wait)
            retries += 1

    log.error("Max retries reached for stream_media_safe — giving up.")


# ─── Helpers ──────────────────────────────────────────────────────────────────
def human_size(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


async def save_file(token: str, file_id: str, file_ref: str,
                    file_name: str, file_size: int, mime: str):
    await col.update_one(
        {"token": token},
        {"$set": {
            "token": token,
            "file_id": file_id,
            "file_ref": file_ref,
            "file_name": file_name,
            "file_size": file_size,
            "mime": mime,
            "created": datetime.utcnow(),
        }},
        upsert=True
    )


async def get_file_doc(token: str):
    return await col.find_one({"token": token})


# ─── Bot Handlers ─────────────────────────────────────────────────────────────
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message: Message):
    await message.reply_text(
        "👋 **Welcome to File-To-Link Bot!**\n\n"
        "📤 Send me any file (up to 4 GB) and I'll give you a download link.\n\n"
        f"Powered by MTProto + Cloudflare ⚡"
    )


@app.on_message(
    filters.private
    & (filters.document | filters.video | filters.audio | filters.photo)
)
async def handle_file(client, message: Message):
    media = message.document or message.video or message.audio or message.photo

    if not media:
        return

    file_id = media.file_id
    file_ref = media.file_unique_id
    file_name = getattr(media, "file_name", None) or f"file_{message.id}"
    file_size = getattr(media, "file_size", 0) or 0
    mime = getattr(media, "mime_type", "application/octet-stream") or "application/octet-stream"

    # Forward to BIN_CHANNEL for persistent storage
    try:
        fwd = await message.forward(BIN_CHANNEL)
        # Use forwarded message's file_id for stable reference
        fwd_media = fwd.document or fwd.video or fwd.audio or fwd.photo
        if fwd_media:
            file_id = fwd_media.file_id
            file_ref = fwd_media.file_unique_id
    except FloodWait as e:
        log.warning(f"FloodWait {e.value}s while forwarding — sleeping…")
        await asyncio.sleep(e.value + 2)
        try:
            fwd = await message.forward(BIN_CHANNEL)
            fwd_media = fwd.document or fwd.video or fwd.audio or fwd.photo
            if fwd_media:
                file_id = fwd_media.file_id
                file_ref = fwd_media.file_unique_id
        except Exception as ex:
            log.error(f"Forward retry failed: {ex}")
    except Exception as ex:
        log.error(f"Forward failed: {ex}")

    token = secrets.token_urlsafe(16)
    await save_file(token, file_id, file_ref, file_name, file_size, mime)

    link = f"{CF_WORKER_URL}/file/{token}"
    await message.reply_text(
        f"✅ **File stored!**\n\n"
        f"📄 **Name:** `{file_name}`\n"
        f"📦 **Size:** {human_size(file_size)}\n\n"
        f"🔗 **Link:**\n{link}"
    )


# ─── aiohttp HTTP Server ───────────────────────────────────────────────────────
CHUNK_SIZE = 1024 * 1024  # 1 MB


async def health(request):
    return web.Response(text="OK")


async def info_handler(request):
    token = request.match_info["token"]
    doc = await get_file_doc(token)
    if not doc:
        raise web.HTTPNotFound(text="File not found")
    return web.json_response({
        "file_name": doc["file_name"],
        "file_size": doc["file_size"],
        "mime": doc["mime"],
    })


async def stream_handler(request):
    token = request.match_info["token"]
    doc = await get_file_doc(token)
    if not doc:
        raise web.HTTPNotFound(text="File not found")

    file_id = doc["file_id"]
    file_size = doc["file_size"]
    mime = doc.get("mime", "application/octet-stream")
    file_name = doc.get("file_name", "file")

    # Parse Range header
    range_header = request.headers.get("Range", None)
    offset = 0
    end = file_size - 1
    status = 200

    if range_header:
        try:
            ranges = range_header.strip().replace("bytes=", "").split("-")
            offset = int(ranges[0]) if ranges[0] else 0
            end = int(ranges[1]) if ranges[1] else file_size - 1
            status = 206
        except Exception:
            pass

    # Align offset to nearest 1MB boundary (Pyrogram requirement)
    first_part = math.floor(offset / CHUNK_SIZE)
    last_part = math.ceil((end + 1) / CHUNK_SIZE)

    offset_bytes = first_part * CHUNK_SIZE
    limit = last_part - first_part
    cut_at = offset - offset_bytes

    headers = {
        "Content-Type": mime,
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - offset + 1),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {offset}-{end}/{file_size}"

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    # Fetch message from BIN_CHANNEL to get a fresh message object
    try:
        # We need to find the message — store msg_id in DB for best results.
        # Fallback: reconstruct using file_id via get_messages workaround.
        msg_id = doc.get("msg_id")
        if msg_id:
            msg = await app.get_messages(BIN_CHANNEL, msg_id)
        else:
            # If no msg_id stored, stream directly via file_id
            # Build a fake message wrapper using copy_message approach
            msg = None
    except FloodWait as e:
        log.warning(f"FloodWait {e.value}s fetching message — sleeping…")
        await asyncio.sleep(e.value + 2)
        msg = None
    except Exception as e:
        log.error(f"get_messages failed: {e}")
        msg = None

    written = 0
    target = end - offset + 1

    try:
        if msg:
            async for chunk in stream_media_safe(msg, offset=first_part, limit=limit):
                if cut_at:
                    chunk = chunk[cut_at:]
                    cut_at = 0
                if written + len(chunk) > target:
                    chunk = chunk[:target - written]
                await response.write(chunk)
                written += len(chunk)
                if written >= target:
                    break
        else:
            # Direct file_id streaming fallback
            async for chunk in app.stream_media(file_id, offset=first_part, limit=limit):
                if cut_at:
                    chunk = chunk[cut_at:]
                    cut_at = 0
                if written + len(chunk) > target:
                    chunk = chunk[:target - written]
                await response.write(chunk)
                written += len(chunk)
                if written >= target:
                    break
    except (ConnectionResetError, BrokenPipeError):
        pass  # Client disconnected — normal
    except FloodWait as e:
        log.warning(f"FloodWait {e.value}s mid-stream — client will retry.")
        _dc_auth_cache.clear()
    except Exception as e:
        log.error(f"Streaming error: {e}")

    await response.write_eof()
    return response


# ─── App Startup / Shutdown ───────────────────────────────────────────────────
web_app = web.Application()
web_app.router.add_get("/health", health)
web_app.router.add_get("/info/{token}", info_handler)
web_app.router.add_get("/stream/{token}", stream_handler)


async def start_services():
    await app.start()
    log.info("Pyrogram client started ✅")

    if UPDATES_CHANNEL:
        try:
            await app.send_message(UPDATES_CHANNEL, "🟢 Bot started!")
        except Exception:
            pass

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("aiohttp server listening on :8080 ✅")

    # Keep alive forever
    await asyncio.Event().wait()


async def stop_services():
    await app.stop()
    log.info("Pyrogram client stopped.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        loop.run_until_complete(stop_services())
