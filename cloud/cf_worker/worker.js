// 神州顺风车监控 · 邮箱订阅 Worker（Cloudflare Workers + KV）
//
// 让没有 GitHub 账号的人也能订阅：看板 POST 邮箱 + 条件 → 存 KV（待确认）→ 发确认信 →
// 点确认才生效；每封提醒都带退订链接。监控（GitHub Actions）每轮用 ADMIN_TOKEN 拉取已确认订阅。
//
//   POST /subscribe            看板提交 {email, profile}
//   GET|POST /confirm?id=&t=   GET 只出页面，点按钮 POST 才生效（防邮件扫描器预取误触）
//   GET|POST /unsubscribe?id=&t=
//   GET /subscriptions         Authorization: Bearer ADMIN_TOKEN → 已确认订阅列表
//
// 绑定：KV SUBS；vars MAIL_FROM / PUBLIC_URL / ALLOWED_ORIGINS；secrets RESEND_API_KEY / ADMIN_TOKEN

// 与 szhitch/subscriptions.py 的 ALLOWED_KEYS 保持一致（email 单独处理）
const ALLOWED_KEYS = [
  "name", "pickup_cities", "return_cities", "any_cities", "routes",
  "brands", "exclude_brands", "models", "exclude_models",
  "max_price", "min_price", "max_price_per_day",
  "only_special", "special_price", "min_days", "max_days",
  "date_from", "date_to",
];
// 至少要有一个筛选条件，同 subscriptions.clean_profile
const FILTER_KEYS = ["pickup_cities", "return_cities", "any_cities", "routes",
  "brands", "models", "max_price", "max_price_per_day"];

const PENDING_TTL = 86400;   // 未确认的订阅 24 小时后自动作废
const RESEND_GAP = 600;      // 同一邮箱 10 分钟内只发一封确认信
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = corsHeaders(request, env);
    try {
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
      if (url.pathname === "/subscribe" && request.method === "POST") return await subscribe(request, env, cors);
      if (url.pathname === "/confirm") return await confirm(request, env, url);
      if (url.pathname === "/unsubscribe") return await unsubscribe(request, env, url);
      if (url.pathname === "/subscriptions" && request.method === "GET") return await list(request, env);
      return json({ error: "not found" }, 404, cors);
    } catch (err) {
      return json({ error: "server error" }, 500, cors);
    }
  },
};

// ---------------------------------------------------------------- 订阅

async function subscribe(request, env, cors) {
  if (!cors["Access-Control-Allow-Origin"]) return json({ error: "origin not allowed" }, 403, cors);
  const raw = await request.text();
  if (raw.length > 4096) return json({ error: "请求过大" }, 413, cors);
  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: "JSON 格式不对" }, 400, cors); }

  const email = String(body.email || "").trim().toLowerCase();
  if (!EMAIL_RE.test(email) || email.length > 254) return json({ error: "邮箱格式不对" }, 400, cors);
  const profile = cleanProfile(body.profile);
  if (!profile) return json({ error: "请至少选一个筛选条件" }, 400, cors);

  const rlKey = `rl:${email}`;
  if (await env.SUBS.get(rlKey)) return json({ error: "确认邮件刚发过，请 10 分钟后再试" }, 429, cors);

  const rec = {
    id: crypto.randomUUID(),
    token: randomHex(24),
    email,
    profile,
    status: "pending",
    created: new Date().toISOString(),
  };
  await env.SUBS.put(`sub:${rec.id}`, JSON.stringify(rec), { expirationTtl: PENDING_TTL });
  await env.SUBS.put(rlKey, "1", { expirationTtl: RESEND_GAP });

  const link = `${env.PUBLIC_URL}/confirm?id=${rec.id}&t=${rec.token}`;
  const ok = await sendMail(env, email, "确认订阅 · 神州顺风车监控",
    `<p>你在神州顺风车监控订阅了：</p><p><b>${esc(describe(profile))}</b></p>` +
    `<p><a href="${link}">点这里确认订阅</a>（24 小时内有效）。</p>` +
    `<p style="color:#888">不是你本人操作的话，忽略这封邮件即可，不会生效。</p>`);
  if (!ok) return json({ error: "确认邮件发送失败，请稍后再试" }, 502, cors);
  return json({ ok: true }, 200, cors);
}

async function confirm(request, env, url) {
  const rec = await loadRec(env, url);
  if (!rec) return page("链接无效或已过期", "请回到看板重新订阅。");
  if (request.method !== "POST") {
    return page("确认订阅", `条件：${esc(describe(rec.profile))}`, "确认订阅", url);
  }
  if (rec.status !== "active") {
    rec.status = "active";
    rec.confirmed = new Date().toISOString();
    await env.SUBS.put(`sub:${rec.id}`, JSON.stringify(rec));  // 去掉 TTL，长期有效
  }
  return page("订阅成功 ✅", "有符合条件的车时会发邮件给你；每封邮件底部都有退订链接。");
}

async function unsubscribe(request, env, url) {
  const rec = await loadRec(env, url);
  if (!rec) return page("已退订", "这条订阅已不存在。");
  if (request.method !== "POST") {
    return page("退订", `条件：${esc(describe(rec.profile))}`, "确认退订", url);
  }
  await env.SUBS.delete(`sub:${rec.id}`);
  return page("已退订", "以后不会再收到这条订阅的提醒。");
}

async function list(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!env.ADMIN_TOKEN || auth !== `Bearer ${env.ADMIN_TOKEN}`) return json({ error: "unauthorized" }, 401);
  const out = [];
  let cursor;
  do {
    const res = await env.SUBS.list({ prefix: "sub:", cursor });
    for (const k of res.keys) {
      const rec = await env.SUBS.get(k.name, "json");
      if (rec && rec.status === "active") {
        out.push({ id: rec.id, token: rec.token, email: rec.email, profile: rec.profile });
      }
    }
    cursor = res.list_complete ? undefined : res.cursor;
  } while (cursor);
  return json({ subscriptions: out });
}

// ---------------------------------------------------------------- 工具

function cleanProfile(p) {
  if (!p || typeof p !== "object" || Array.isArray(p)) return null;
  const out = {};
  for (const k of ALLOWED_KEYS) {
    const v = p[k];
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) continue;
    if (Array.isArray(v)) out[k] = v.slice(0, 50).map(x => String(x).slice(0, 40));
    else if (typeof v === "number" || typeof v === "boolean") out[k] = v;
    else if (typeof v === "string") out[k] = v.slice(0, 40);
  }
  return FILTER_KEYS.some(k => out[k] !== undefined) ? out : null;
}

function describe(p) {
  const bits = [];
  if (p.pickup_cities) bits.push(`取车 ${p.pickup_cities.join("/")}`);
  if (p.return_cities) bits.push(`还车 ${p.return_cities.join("/")}`);
  if (p.any_cities) bits.push(`途经 ${p.any_cities.join("/")}`);
  if (p.routes) bits.push(`路线 ${p.routes.join("、")}`);
  if (p.brands) bits.push(`品牌 ${p.brands.join("/")}`);
  if (p.models) bits.push(`车型 ${p.models.join("/")}`);
  if (p.max_price != null) bits.push(`≤${p.max_price} 元`);
  return bits.join(" · ") || String(p.name || "我的订阅");
}

async function loadRec(env, url) {
  const id = url.searchParams.get("id") || "";
  const t = url.searchParams.get("t") || "";
  if (!/^[0-9a-f-]{36}$/.test(id) || !t) return null;
  const rec = await env.SUBS.get(`sub:${id}`, "json");
  return rec && rec.token === t ? rec : null;
}

async function sendMail(env, to, subject, html) {
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: `神州顺风车监控 <${env.MAIL_FROM}>`, to: [to], subject, html }),
  });
  return r.ok;
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin") || "";
  const allowed = String(env.ALLOWED_ORIGINS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!allowed.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
  });
}

function page(title, text, button, url) {
  const form = button
    ? `<form method="post" action="${esc(url.pathname + url.search)}"><button>${esc(button)}</button></form>`
    : "";
  return new Response(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title>
<style>body{font-family:-apple-system,"Segoe UI",sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;color:#222}
button{font-size:16px;padding:10px 22px;border:0;border-radius:6px;background:#e0a100;color:#1a1400;cursor:pointer}</style>
</head><body><h2>${esc(title)}</h2><p>${text}</p>${form}</body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function randomHex(bytes) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return [...a].map(b => b.toString(16).padStart(2, "0")).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
