/**
 * File-To-Link  ─  Cloudflare Worker (Frontend)
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * Routes:
 *   GET /           → status page
 *   GET /file/{token} → beautiful download page
 *   GET /dl/{token}   → proxies stream from Koyeb Python server
 *
 * env secrets needed:
 *   KOYEB_URL  = https://your-app.koyeb.app   (no trailing slash)
 */

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtSize(b) {
  if (!b) return "Unknown";
  const u = ["B","KB","MB","GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}`;
}

// ── Pages ─────────────────────────────────────────────────────────────────────

function downloadPage(info, token, workerUrl) {
  const dlUrl   = `${workerUrl}/dl/${token}`;
  const { file_name, file_size, mime_type } = info;

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>${file_name}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0a0a0f;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: linear-gradient(145deg, #141420, #1a1a2e);
      border: 1px solid #2a2a45;
      border-radius: 20px;
      padding: 44px 36px;
      max-width: 540px;
      width: 100%;
      text-align: center;
      box-shadow: 0 24px 80px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.04);
    }
    .icon { font-size: 64px; margin-bottom: 20px; display: block; }
    .name {
      font-size: 1.25rem;
      font-weight: 700;
      color: #f1f5f9;
      word-break: break-all;
      margin-bottom: 10px;
      line-height: 1.4;
    }
    .meta {
      display: flex;
      gap: 12px;
      justify-content: center;
      flex-wrap: wrap;
      margin-bottom: 32px;
    }
    .badge {
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 999px;
      padding: 4px 14px;
      font-size: 0.78rem;
      color: #94a3b8;
    }
    .actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 14px 28px;
      border-radius: 12px;
      font-size: 0.95rem;
      font-weight: 600;
      text-decoration: none;
      transition: transform .15s, box-shadow .15s;
      cursor: pointer;
    }
    .btn:hover { transform: translateY(-2px); }
    .btn-dl {
      background: linear-gradient(135deg, #0ea5e9, #0284c7);
      color: #fff;
      box-shadow: 0 4px 20px rgba(14,165,233,.35);
    }
    .btn-dl:hover { box-shadow: 0 8px 30px rgba(14,165,233,.5); }
    .btn-stream {
      background: linear-gradient(135deg, #22c55e, #16a34a);
      color: #fff;
      box-shadow: 0 4px 20px rgba(34,197,94,.3);
    }
    .btn-stream:hover { box-shadow: 0 8px 30px rgba(34,197,94,.5); }
    .footer {
      margin-top: 28px;
      font-size: 0.72rem;
      color: #475569;
    }
    .footer span { color: #0ea5e9; }
  </style>
</head>
<body>
<div class="card">
  <span class="icon">📁</span>
  <div class="name">${file_name}</div>
  <div class="meta">
    <span class="badge">📦 ${fmtSize(file_size)}</span>
    <span class="badge">🏷️ ${mime_type || "unknown"}</span>
  </div>
  <div class="actions">
    <a class="btn btn-dl"     href="${dlUrl}" download="${file_name}">⬇️ Download</a>
    <a class="btn btn-stream" href="${dlUrl}" target="_blank">▶️ Stream</a>
  </div>
  <div class="footer">
    Powered by <span>Cloudflare Workers</span> + <span>Pyrogram MTProto</span>
    · Up to 4 GB supported
  </div>
</div>
</body>
</html>`;
}

function homePage() {
  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>File Bot</title>
<style>body{background:#0a0a0f;color:#e2e8f0;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:20px}
.b{text-align:center;max-width:400px}
h1{color:#0ea5e9;font-size:2rem;margin-bottom:12px}
p{color:#64748b;margin-bottom:8px}
.status{color:#22c55e;font-size:.85rem;margin-top:16px}
</style></head>
<body><div class="b">
<h1>📁 File To Link</h1>
<p>Telegram file → direct download link</p>
<p style="font-size:.8rem;color:#475569">Supports files up to 4 GB · Pyrogram MTProto</p>
<div class="status">✅ Cloudflare Worker is running</div>
</div></body></html>`;
}

function errorPage(msg, detail = "") {
  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Error</title>
<style>body{background:#0a0a0f;color:#e2e8f0;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:20px}
.b{text-align:center;max-width:460px;background:#141420;border:1px solid #2a2a45;
border-radius:16px;padding:36px}
h2{color:#f87171;margin-bottom:12px}p{color:#94a3b8;line-height:1.6}
small{color:#475569;font-size:.75rem}</style></head>
<body><div class="b">
<h2>❌ ${msg}</h2>
${detail ? `<p>${detail}</p>` : ""}
<small>If you think this is an error, try again later.</small>
</div></body></html>`;
}

// ── Route: /dl/{token} — proxies stream from Koyeb ───────────────────────────

async function handleDownload(env, token, request) {
  const koyebStream = `${env.KOYEB_URL}/stream/${token}`;

  // Forward Range header (for video seeking / resumable download)
  const rangeHeader = request.headers.get("Range");
  const reqHeaders  = { "User-Agent": "CloudflareWorker/1.0" };
  if (rangeHeader) reqHeaders["Range"] = rangeHeader;

  let upstream;
  try {
    upstream = await fetch(koyebStream, {
      method: "GET",
      headers: reqHeaders,
      // CF will stream — don't buffer entire response
    });
  } catch (err) {
    return new Response(
      errorPage("Server Unreachable", "The Koyeb Python server could not be reached. It may be starting up — try again in 10 seconds."),
      { status: 502, headers: { "Content-Type": "text/html;charset=utf-8" } }
    );
  }

  if (upstream.status === 404) {
    return new Response(
      errorPage("File Not Found", "This link may have expired or the file was removed."),
      { status: 404, headers: { "Content-Type": "text/html;charset=utf-8" } }
    );
  }

  if (!upstream.ok && upstream.status !== 206) {
    return new Response(
      errorPage("Download Failed", `Server returned status ${upstream.status}.`),
      { status: upstream.status, headers: { "Content-Type": "text/html;charset=utf-8" } }
    );
  }

  // Pass through headers from Koyeb (Content-Type, Content-Disposition, etc.)
  const headers = new Headers(upstream.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "public, max-age=3600");

  return new Response(upstream.body, {
    status : upstream.status,
    headers,
  });
}

// ── Route: /file/{token} — HTML download page ─────────────────────────────────

async function handleFilePage(env, token, workerUrl) {
  // Fetch file metadata from Koyeb /info/{token}
  let info;
  try {
    const res = await fetch(`${env.KOYEB_URL}/info/${token}`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    info = await res.json();
  } catch (e) {
    return new Response(
      errorPage("File Not Found", "This link may have expired or the file was removed."),
      { status: 404, headers: { "Content-Type": "text/html;charset=utf-8" } }
    );
  }

  return new Response(downloadPage(info, token, workerUrl), {
    headers: { "Content-Type": "text/html;charset=utf-8" },
  });
}

// ── Main Entry ────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url       = new URL(request.url);
    const path      = url.pathname;
    const workerUrl = `${url.protocol}//${url.host}`;

    // GET /dl/{token}
    if (request.method === "GET" && path.startsWith("/dl/")) {
      return handleDownload(env, path.slice(4), request);
    }

    // GET /file/{token}
    if (request.method === "GET" && path.startsWith("/file/")) {
      return handleFilePage(env, path.slice(6), workerUrl);
    }

    // GET /
    if (request.method === "GET" && (path === "/" || path === "")) {
      return new Response(homePage(), { headers: { "Content-Type": "text/html;charset=utf-8" } });
    }

    return new Response("Not Found", { status: 404 });
  },
};
