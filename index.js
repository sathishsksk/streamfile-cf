/**
 * File-To-Link Bot — Cloudflare Workers Edition
 * Rewritten from Python (polling) → JavaScript (webhook)
 * 
 * Architecture:
 *  POST /webhook        ← Telegram pushes all updates here
 *  GET  /dl/{token}     ← User clicks download link → streams file
 *  GET  /setup          ← One-time: registers webhook with Telegram
 *  GET  /               ← Health check / status page
 */

// ─── Telegram API Helper ──────────────────────────────────────────────────────

async function tg(env, method, body = {}) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  return res.json();
}

// Send a message (supports HTML parse mode)
async function sendMessage(env, chat_id, text, extra = {}) {
  return tg(env, "sendMessage", {
    chat_id,
    text,
    parse_mode: "HTML",
    ...extra,
  });
}

// Edit a message (for channel file posts)
async function editMessage(env, chat_id, message_id, text, extra = {}) {
  return tg(env, "editMessageText", {
    chat_id,
    message_id,
    text,
    parse_mode: "HTML",
    ...extra,
  });
}

// Forward message to BIN_CHANNEL and return message_id
async function forwardToBin(env, from_chat_id, message_id) {
  const res = await tg(env, "forwardMessage", {
    chat_id: env.BIN_CHANNEL,
    from_chat_id,
    message_id,
  });
  return res.ok ? res.result.message_id : null;
}

// Copy message to BIN_CHANNEL (no forward header)
async function copyToBin(env, from_chat_id, message_id) {
  const res = await tg(env, "copyMessage", {
    chat_id: env.BIN_CHANNEL,
    from_chat_id,
    message_id,
  });
  return res.ok ? res.result.message_id : null;
}

// Get Telegram file info (path)
async function getFileInfo(env, file_id) {
  const res = await tg(env, "getFile", { file_id });
  return res.ok ? res.result : null;
}

// Get bot info
async function getBotInfo(env) {
  const res = await tg(env, "getMe");
  return res.ok ? res.result : null;
}

// Answer callback query
async function answerCallback(env, callback_query_id, text = "") {
  return tg(env, "answerCallbackQuery", { callback_query_id, text });
}

// ─── KV Helpers ───────────────────────────────────────────────────────────────
// KV stores:  token → { file_id, file_name, file_size, mime_type, msg_id }

function generateToken() {
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function storeFile(env, fileData) {
  const token = generateToken();
  await env.FILES_KV.put(token, JSON.stringify(fileData), {
    expirationTtl: 60 * 60 * 24 * 30, // 30 days
  });
  return token;
}

async function getFile(env, token) {
  const data = await env.FILES_KV.get(token);
  return data ? JSON.parse(data) : null;
}

// User session: track password-verified users
async function isVerified(env, user_id) {
  if (!env.MY_PASS) return true; // No password set → open access
  const val = await env.FILES_KV.get(`auth_${user_id}`);
  return val === "1";
}

async function setVerified(env, user_id) {
  await env.FILES_KV.put(`auth_${user_id}`, "1", {
    expirationTtl: 60 * 60 * 24 * 7, // 7 days session
  });
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function formatBytes(bytes) {
  if (!bytes) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function extractMedia(message) {
  // Returns { file_id, file_name, file_size, mime_type } or null
  const m = message;

  if (m.document)
    return {
      file_id: m.document.file_id,
      file_name: m.document.file_name || "file",
      file_size: m.document.file_size,
      mime_type: m.document.mime_type || "application/octet-stream",
    };

  if (m.video)
    return {
      file_id: m.video.file_id,
      file_name: m.video.file_name || `video_${Date.now()}.mp4`,
      file_size: m.video.file_size,
      mime_type: m.video.mime_type || "video/mp4",
    };

  if (m.audio)
    return {
      file_id: m.audio.file_id,
      file_name:
        m.audio.title ||
        m.audio.file_name ||
        `audio_${Date.now()}.mp3`,
      file_size: m.audio.file_size,
      mime_type: m.audio.mime_type || "audio/mpeg",
    };

  if (m.photo) {
    const photo = m.photo[m.photo.length - 1]; // Largest size
    return {
      file_id: photo.file_id,
      file_name: `photo_${Date.now()}.jpg`,
      file_size: photo.file_size,
      mime_type: "image/jpeg",
    };
  }

  if (m.voice)
    return {
      file_id: m.voice.file_id,
      file_name: `voice_${Date.now()}.ogg`,
      file_size: m.voice.file_size,
      mime_type: "audio/ogg",
    };

  if (m.video_note)
    return {
      file_id: m.video_note.file_id,
      file_name: `videonote_${Date.now()}.mp4`,
      file_size: m.video_note.file_size,
      mime_type: "video/mp4",
    };

  if (m.sticker)
    return {
      file_id: m.sticker.file_id,
      file_name: `sticker_${Date.now()}.webp`,
      file_size: m.sticker.file_size,
      mime_type: "image/webp",
    };

  if (m.animation)
    return {
      file_id: m.animation.file_id,
      file_name: m.animation.file_name || `gif_${Date.now()}.gif`,
      file_size: m.animation.file_size,
      mime_type: m.animation.mime_type || "video/mp4",
    };

  return null;
}

// ─── HTML Pages ───────────────────────────────────────────────────────────────

function downloadPage(fileData, downloadUrl, workerUrl) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>${fileData.file_name} — File To Link</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f0f;color:#eee;min-height:100vh;display:flex;align-items:center;justify-content:center}
    .card{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:40px;max-width:520px;width:90%;text-align:center;box-shadow:0 8px 40px rgba(0,0,0,.5)}
    .icon{font-size:56px;margin-bottom:16px}
    h1{font-size:1.4rem;margin-bottom:8px;color:#fff;word-break:break-all}
    .meta{color:#888;font-size:0.85rem;margin-bottom:28px}
    .meta span{margin:0 8px}
    a.btn{display:inline-block;background:linear-gradient(135deg,#0088cc,#005f8e);color:#fff;text-decoration:none;padding:14px 36px;border-radius:10px;font-size:1rem;font-weight:600;margin:6px;transition:transform .15s,box-shadow .15s}
    a.btn:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,136,204,.4)}
    a.btn.stream{background:linear-gradient(135deg,#22c55e,#16a34a)}
    a.btn.stream:hover{box-shadow:0 6px 20px rgba(34,197,94,.4)}
    .footer{margin-top:28px;font-size:0.75rem;color:#555}
    .footer a{color:#0088cc;text-decoration:none}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">📁</div>
    <h1>${fileData.file_name}</h1>
    <div class="meta">
      <span>📦 ${formatBytes(fileData.file_size)}</span>
      <span>•</span>
      <span>🏷️ ${fileData.mime_type}</span>
    </div>
    <a class="btn" href="${downloadUrl}" download="${fileData.file_name}">⬇️ Download</a>
    <a class="btn stream" href="${downloadUrl}" target="_blank">▶️ Stream / Open</a>
    <div class="footer">
      Powered by <a href="${workerUrl}">File-To-Link Bot</a> on Cloudflare Workers
    </div>
  </div>
</body>
</html>`;
}

function homePage(botUsername) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>File-To-Link Bot</title>
  <style>
    body{font-family:'Segoe UI',sans-serif;background:#0f0f0f;color:#eee;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .card{text-align:center;padding:40px;max-width:420px}
    h1{font-size:2rem;color:#0088cc;margin-bottom:12px}
    p{color:#888;margin-bottom:24px}
    a{display:inline-block;background:#0088cc;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600}
    .status{color:#22c55e;font-size:0.85rem;margin-top:20px}
  </style>
</head>
<body>
  <div class="card">
    <h1>📁 File To Link</h1>
    <p>Instant Telegram file → direct download link generator</p>
    <a href="https://t.me/${botUsername}">Open Bot on Telegram</a>
    <div class="status">✅ Worker is running on Cloudflare</div>
  </div>
</body>
</html>`;
}

// ─── Message Handlers ─────────────────────────────────────────────────────────

async function handleStart(env, message) {
  const { chat, from } = message;
  const name = from.first_name || "there";

  const text = `👋 <b>Hello, ${name}!</b>

📁 <b>File To Link Bot</b>
Send me any file, video, audio, or photo and I'll give you an instant <b>direct download link</b>.

<b>How to use:</b>
1️⃣ Just send or forward any file to this bot
2️⃣ Get an instant download link
3️⃣ Share it with anyone — no Telegram needed!

⚡ Powered by Cloudflare Workers — zero sleep, global CDN.`;

  await sendMessage(env, chat.id, text, {
    reply_markup: {
      inline_keyboard: [
        [{ text: "📢 Updates Channel", url: `https://t.me/${env.UPDATES_CHANNEL || "telegram"}` }],
      ],
    },
  });
}

async function handleHelp(env, message) {
  const { chat } = message;
  await sendMessage(
    env,
    chat.id,
    `📖 <b>Help</b>

/start — Start the bot
/help  — Show this message
/ping  — Check if bot is alive

Simply send any <b>file, video, audio, photo</b> and get a direct link instantly.`
  );
}

async function handlePing(env, message) {
  const start = Date.now();
  const sent = await sendMessage(env, message.chat.id, "🏓 Pinging...");
  const ms = Date.now() - start;
  await editMessage(
    env,
    message.chat.id,
    sent.result.message_id,
    `🏓 <b>Pong!</b>  <code>${ms}ms</code>\n⚡ Running on Cloudflare Workers`
  );
}

async function handleFile(env, message, workerUrl) {
  const { chat, from, message_id } = message;

  // Password check
  if (env.MY_PASS && !(await isVerified(env, from.id))) {
    await sendMessage(
      env,
      chat.id,
      `🔒 <b>Bot is password protected.</b>\n\nSend the password to continue.`
    );
    await env.FILES_KV.put(`pending_auth_${from.id}`, "1", { expirationTtl: 300 });
    return;
  }

  const media = extractMedia(message);
  if (!media) return; // Not a media message

  // Show "processing" message
  const processing = await sendMessage(env, chat.id, "⏳ <b>Processing your file...</b>");

  // Copy to BIN_CHANNEL so file is always accessible via bot token
  const binMsgId = await copyToBin(env, chat.id, message_id);
  if (!binMsgId) {
    await editMessage(env, chat.id, processing.result.message_id, "❌ Failed to process file. Is the bot added to BIN_CHANNEL as admin?");
    return;
  }

  // Store in KV
  const token = await storeFile(env, {
    ...media,
    bin_msg_id: binMsgId,
    uploaded_by: from.id,
    uploaded_at: Date.now(),
  });

  const downloadUrl = `${workerUrl}/dl/${token}`;
  const pageUrl = `${workerUrl}/file/${token}`;

  const replyText = `✅ <b>Your link is ready!</b>

📄 <b>File:</b> <code>${media.file_name}</code>
📦 <b>Size:</b> ${formatBytes(media.file_size)}
🏷️ <b>Type:</b> <code>${media.mime_type}</code>

🔗 <b>Download Link:</b>
<code>${downloadUrl}</code>

🌐 <b>Web Page:</b>
<code>${pageUrl}</code>`;

  await editMessage(env, chat.id, processing.result.message_id, replyText, {
    reply_markup: {
      inline_keyboard: [
        [
          { text: "⬇️ Download", url: downloadUrl },
          { text: "🌐 Web Page", url: pageUrl },
        ],
      ],
    },
  });
}

async function handlePassword(env, message) {
  const { chat, from, text } = message;

  const pending = await env.FILES_KV.get(`pending_auth_${from.id}`);
  if (!pending) return false; // Not waiting for password

  if (text === env.MY_PASS) {
    await setVerified(env, from.id);
    await env.FILES_KV.delete(`pending_auth_${from.id}`);
    await sendMessage(env, chat.id, "✅ <b>Correct password!</b> You can now send files.");
    return true;
  } else {
    await sendMessage(env, chat.id, "❌ <b>Wrong password.</b> Try again.");
    return true;
  }
}

// Handle files posted in a channel (auto-generate link)
async function handleChannelPost(env, channel_post, workerUrl) {
  const media = extractMedia(channel_post);
  if (!media) return;

  const { chat, message_id } = channel_post;

  // Copy to BIN_CHANNEL
  const binMsgId = await copyToBin(env, chat.id, message_id);
  if (!binMsgId) return;

  const token = await storeFile(env, {
    ...media,
    bin_msg_id: binMsgId,
    uploaded_at: Date.now(),
  });

  const downloadUrl = `${workerUrl}/dl/${token}`;
  const pageUrl = `${workerUrl}/file/${token}`;

  // Edit original channel post to add download button
  await tg(env, "editMessageReplyMarkup", {
    chat_id: chat.id,
    message_id,
    reply_markup: {
      inline_keyboard: [
        [
          { text: "⬇️ Download", url: downloadUrl },
          { text: "🌐 Web Page", url: pageUrl },
        ],
      ],
    },
  });
}

// ─── Route Handlers ───────────────────────────────────────────────────────────

async function handleWebhook(env, request, workerUrl) {
  const update = await request.json();

  // Channel post
  if (update.channel_post) {
    await handleChannelPost(env, update.channel_post, workerUrl);
    return new Response("ok");
  }

  const message = update.message || update.edited_message;
  if (!message) return new Response("ok");

  const text = message.text || "";

  // Commands
  if (text.startsWith("/start")) {
    await handleStart(env, message);
    return new Response("ok");
  }
  if (text.startsWith("/help")) {
    await handleHelp(env, message);
    return new Response("ok");
  }
  if (text.startsWith("/ping")) {
    await handlePing(env, message);
    return new Response("ok");
  }

  // Password input?
  if (env.MY_PASS && text && !text.startsWith("/")) {
    const handled = await handlePassword(env, message);
    if (handled) return new Response("ok");
  }

  // Media file
  const media = extractMedia(message);
  if (media) {
    await handleFile(env, message, workerUrl);
  }

  return new Response("ok");
}

async function handleDownload(env, token) {
  const fileData = await getFile(env, token);
  if (!fileData) {
    return new Response("❌ File not found or link expired.", { status: 404 });
  }

  // Get fresh file path from Telegram (file_path URLs expire)
  const info = await getFileInfo(env, fileData.file_id);
  if (!info || !info.file_path) {
    return new Response("❌ Could not retrieve file from Telegram.", { status: 502 });
  }

  const telegramFileUrl = `https://api.telegram.org/file/bot${env.BOT_TOKEN}/${info.file_path}`;

  // Proxy-stream the file directly from Telegram CDN
  const upstream = await fetch(telegramFileUrl);
  if (!upstream.ok) {
    return new Response("❌ Failed to fetch file from Telegram.", { status: 502 });
  }

  const headers = new Headers();
  headers.set("Content-Type", fileData.mime_type || "application/octet-stream");
  headers.set("Content-Disposition", `attachment; filename="${fileData.file_name}"`);
  if (fileData.file_size) headers.set("Content-Length", String(fileData.file_size));
  headers.set("Cache-Control", "public, max-age=3600");
  headers.set("Access-Control-Allow-Origin", "*");

  return new Response(upstream.body, { headers, status: 200 });
}

async function handleFilePage(env, token, workerUrl) {
  const fileData = await getFile(env, token);
  if (!fileData) {
    return new Response("❌ File not found or link expired.", {
      status: 404,
      headers: { "Content-Type": "text/plain" },
    });
  }

  const downloadUrl = `${workerUrl}/dl/${token}`;
  const html = downloadPage(fileData, downloadUrl, workerUrl);
  return new Response(html, { headers: { "Content-Type": "text/html;charset=utf-8" } });
}

async function handleSetup(env, workerUrl) {
  const webhookUrl = `${workerUrl}/webhook`;
  const result = await tg(env, "setWebhook", {
    url: webhookUrl,
    allowed_updates: ["message", "edited_message", "channel_post", "callback_query"],
    drop_pending_updates: true,
  });

  const botInfo = await getBotInfo(env);

  return new Response(
    JSON.stringify({
      webhook_set: result,
      bot: botInfo,
      worker_url: workerUrl,
    }),
    { headers: { "Content-Type": "application/json" } }
  );
}

// ─── Main Entry Point ─────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const workerUrl = `${url.protocol}//${url.host}`;
    const path = url.pathname;

    // POST /webhook  — Telegram sends updates here
    if (request.method === "POST" && path === "/webhook") {
      return handleWebhook(env, request, workerUrl);
    }

    // GET /dl/{token}  — Direct file download (streams from Telegram)
    if (request.method === "GET" && path.startsWith("/dl/")) {
      const token = path.slice(4);
      return handleDownload(env, token);
    }

    // GET /file/{token}  — Download page (HTML)
    if (request.method === "GET" && path.startsWith("/file/")) {
      const token = path.slice(6);
      return handleFilePage(env, token, workerUrl);
    }

    // GET /setup  — Register webhook (run once after deploy)
    if (request.method === "GET" && path === "/setup") {
      return handleSetup(env, workerUrl);
    }

    // GET /  — Status page
    if (request.method === "GET" && path === "/") {
      const botInfo = await getBotInfo(env);
      const html = homePage(botInfo?.username || "YourBot");
      return new Response(html, { headers: { "Content-Type": "text/html;charset=utf-8" } });
    }

    return new Response("Not Found", { status: 404 });
  },
};
