/**
 * File-To-Link — Cloudflare Worker
 * Routes:
 *   GET /              → status page
 *   GET /file/{id}/{name}?hash=  → download page HTML
 *   GET /dl/{id}/{name}?hash=    → proxies to Koyeb /stream/{id}?hash=
 *
 * env: KOYEB_URL = https://your-app.koyeb.app  (no trailing slash)
 */

function fmtSize(b) {
  if (!b) return "Unknown";
  const u = ["B","KB","MB","GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}`;
}

// ── Download page ─────────────────────────────────────────────────────────────
function downloadPage(info, binMsgId, filename, hash, workerUrl) {
  const dlUrl = `${workerUrl}/dl/${binMsgId}/${encodeURIComponent(filename)}?hash=${hash}`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>${info.file_name || filename}</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0a0f;
    color:#e2e8f0;min-height:100vh;display:flex;align-items:center;
    justify-content:center;padding:20px}
    .card{background:linear-gradient(145deg,#141420,#1a1a2e);
    border:1px solid #2a2a45;border-radius:20px;padding:44px 36px;
    max-width:540px;width:100%;text-align:center;
    box-shadow:0 24px 80px rgba(0,0,0,.6)}
    .icon{font-size:64px;margin-bottom:20px;display:block}
    .name{font-size:1.2rem;font-weight:700;color:#f1f5f9;
    word-break:break-all;margin-bottom:10px;line-height:1.4}
    .meta{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:32px}
    .badge{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
    border-radius:999px;padding:4px 14px;font-size:.78rem;color:#94a3b8}
    .actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
    .btn{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;
    border-radius:12px;font-size:.95rem;font-weight:600;text-decoration:none;
    transition:transform .15s,box-shadow .15s}
    .btn:hover{transform:translateY(-2px)}
    .btn-dl{background:linear-gradient(135deg,#0ea5e9,#0284c7);color:#fff;
    box-shadow:0 4px 20px rgba(14,165,233,.35)}
    .btn-dl:hover{box-shadow:0 8px 30px rgba(14,165,233,.5)}
    .btn-stream{background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;
    box-shadow:0 4px 20px rgba(34,197,94,.3)}
    .btn-stream:hover{box-shadow:0 8px 30px rgba(34,197,94,.5)}
    .footer{margin-top:28px;font-size:.72rem;color:#475569}
    .footer span{color:#0ea5e9}
  </style>
</head>
<body>
<div class="card">
  <span class="icon">📁</span>
  <div class="name">${info.file_name || filename}</div>
  <div class="meta">
    <span class="badge">📦 ${fmtSize(info.file_size)}</span>
    <span class="badge">🏷️ ${info.mime_type || "file"}</span>
  </div>
  <div class="actions">
    <a class="btn btn-dl"     href="${dlUrl}" download="${info.file_name || filename}">⬇️ Download</a>
    <a class="btn btn-stream" href="${dlUrl}" target="_blank">▶️ Stream</a>
  </div>
  <div class="footer">
    Powered by <span>Cloudflare Workers</span> + <span>Pyrogram MTProto</span>
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
<div class="status">✅ Cloudflare Worker is running</div>
</div></body></html>`;
}

// ── Route: GET /dl/{binMsgId}/{filename}?hash= ────────────────────────────────
// Proxy to Koyeb /stream/{binMsgId}?hash=  (hash has encoded file_id inside)
async function handleDownload(env, binMsgId, filename, hash, request) {
  const koyebUrl = `${env.KOYEB_URL}/stream/${binMsgId}?hash=${encodeURIComponent(hash)}`;

  const rangeHeader = request.headers.get("Range");
  const reqHeaders  = { "User-Agent": "CloudflareWorker/1.0" };
  if (rangeHeader) reqHeaders["Range"] = rangeHeader;

  let upstream;
  try {
    upstream = await fetch(koyebUrl, { method: "GET", headers: reqHeaders });
  } catch (err) {
    return new Response("Koyeb server unreachable. Try again in a moment.", {
      status: 502,
      headers: { "Content-Type": "text/plain", "Access-Control-Allow-Origin": "*" },
    });
  }

  if (!upstream.ok && upstream.status !== 206) {
    const body = await upstream.text().catch(() => "");
    return new Response(`Download failed (${upstream.status}): ${body}`, {
      status: upstream.status,
      headers: { "Content-Type": "text/plain", "Access-Control-Allow-Origin": "*" },
    });
  }

  const headers = new Headers(upstream.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "public, max-age=3600");
  // Always override Content-Disposition with the actual filename from URL
  headers.set("Content-Disposition", `attachment; filename="${decodeURIComponent(filename)}"`);
  headers.set("X-File-Name", decodeURIComponent(filename));

  return new Response(upstream.body, { status: upstream.status, headers });
}

// ── Route: GET /file/{binMsgId}/{filename}?hash= ──────────────────────────────
// Show HTML download page — fetch metadata from Koyeb /info/{binMsgId}?hash=
async function handleFilePage(env, binMsgId, filename, hash, workerUrl) {
  let info = { file_name: filename, file_size: 0, mime_type: "file" };
  try {
    const res = await fetch(
      `${env.KOYEB_URL}/info/${binMsgId}?hash=${encodeURIComponent(hash)}&name=${encodeURIComponent(filename)}`,
      { headers: { "User-Agent": "CloudflareWorker/1.0" } }
    );
    if (res.ok) {
      const data = await res.json();
      // Always use filename from URL — it's the most accurate
      info = { ...data, file_name: filename };
    }
  } catch (e) {
    // Use defaults — page still shows with correct filename
  }

  return new Response(downloadPage(info, binMsgId, filename, hash, workerUrl), {
    headers: { "Content-Type": "text/html;charset=utf-8" },
  });
}

// ── Main entry ────────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url       = new URL(request.url);
    const path      = url.pathname;
    const workerUrl = `${url.protocol}//${url.host}`;
    const hash      = url.searchParams.get("hash") || "";

    // GET /dl/{binMsgId}/{filename}?hash=
    if (request.method === "GET" && path.startsWith("/dl/")) {
      const parts    = path.slice(4).split("/");
      const binMsgId = parts[0];
      const filename = decodeURIComponent(parts.slice(1).join("/") || "file");
      return handleDownload(env, binMsgId, filename, hash, request);
    }

    // GET /file/{binMsgId}/{filename}?hash=
    if (request.method === "GET" && path.startsWith("/file/")) {
      const parts    = path.slice(6).split("/");
      const binMsgId = parts[0];
      const filename = decodeURIComponent(parts.slice(1).join("/") || "file");
      return handleFilePage(env, binMsgId, filename, hash, workerUrl);
    }

    // GET /
    if (request.method === "GET" && (path === "/" || path === "")) {
      return new Response(homePage(), { headers: { "Content-Type": "text/html;charset=utf-8" } });
    }

    return new Response("Not Found", { status: 404 });
  },
};
