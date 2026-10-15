// algo-rhythm.in in front of Cloud Run. Cloud Run has no custom domains in asia-south1, so this Worker forwards each
// request (WebSockets too) to the service's run.app address. It passes on the visitor's address (X-Client-IP) and the
// address they typed (X-Forwarded-Host) with EDGE_SECRET, a Worker secret also set on both services: the app believes
// those headers only with it (algo_rhythm/server.py from_edge). Deployed by deploy/cloudflare.sh.
//
// POST /_edge/mail (with EDGE_SECRET) emails support: the services send {subject, text, html} (algo_rhythm/notify.py)
// and Cloudflare Email Routing delivers it from no-reply@ to MAIL_TO, support's verified inbox (Email Routing can
// send only to verified destination addresses; support@ forwards there too). MAIL_CC: more verified addresses, each
// sent the same message (shown as Cc).
//
// Mail to support@ (Email Routing rule "Send to a Worker": algo-rhythm-edge) is forwarded to MAIL_TO and every
// MAIL_CC address: Email Routing alone forwards an address to one destination.
const ORIGINS = {
  "algo-rhythm.in": "algo-rhythm-web-zakvaz2hfa-el.a.run.app",
  "staging.algo-rhythm.in": "algorythm-web-staging-zakvaz2hfa-el.a.run.app",
};

const FROM = "no-reply@algo-rhythm.in";

function sameSecret(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function base64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

// a multipart/alternative message (plain text and HTML), every part base64 so any character survives
export function mime({ subject, text, html }, to, cc = [], id = crypto.randomUUID(), date = new Date()) {
  const boundary = "b" + id.replace(/-/g, "");
  const part = (type, body) => [`--${boundary}`, `Content-Type: ${type}; charset=UTF-8`,
    "Content-Transfer-Encoding: base64", "", base64(body).replace(/.{1,76}/g, "$&\r\n").trimEnd()];
  return [`From: algo-rhythm <${FROM}>`, `To: ${to}`, ...(cc.length ? [`Cc: ${cc.join(", ")}`] : []), `Subject: =?UTF-8?B?${base64(subject)}?=`,
    `Message-ID: <${id}@algo-rhythm.in>`, `Date: ${date.toUTCString()}`, "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`, "",
    ...part("text/plain", text), ...part("text/html", html), `--${boundary}--`, ""].join("\r\n");
}

// the Cc addresses: MAIL_CC, comma-separated, without support's own inbox
export function ccList(env) {
  const all = (env.MAIL_CC || "").split(/[\s,;]+/).filter(Boolean).map((a) => a.toLowerCase());
  return [...new Set(all)].filter((a) => a !== String(env.MAIL_TO || "").toLowerCase());
}

async function mail(request, env) {
  if (request.method !== "POST" || !sameSecret(request.headers.get("x-edge-secret"), env.EDGE_SECRET)) {
    return new Response("forbidden", { status: 403 });
  }
  const size = Number(request.headers.get("content-length") || 0);
  if (!size || size > 200000) return new Response("bad size", { status: 400 });
  const m = await request.json();
  if (![m.subject, m.text, m.html].every((v) => typeof v === "string" && v)) {
    return new Response("need subject, text and html", { status: 400 });
  }
  const { EmailMessage } = await import("cloudflare:email");
  // one message, delivered to support's inbox and to each Cc (each must be a verified Email Routing address)
  const cc = ccList(env);
  const raw = mime(m, "support@algo-rhythm.in", cc);
  try {
    await env.SUPPORT_MAIL.send(new EmailMessage(FROM, env.MAIL_TO, raw));
  } catch (e) {
    return new Response(`not sent: ${e.message}`, { status: 502 });
  }
  const failed = [];
  for (const to of cc) {
    try {
      await env.SUPPORT_MAIL.send(new EmailMessage(FROM, to, raw));
    } catch (e) {
      failed.push(`${to}: ${e.message}`);
    }
  }
  return Response.json({ ok: true, cc: cc.length - failed.length, failed });
}

export default {
  // mail arriving for support@: one copy to each of support's inbox and the Cc list (one bad address skips only itself)
  async email(message, env) {
    for (const to of [env.MAIL_TO, ...ccList(env)]) {
      try {
        await message.forward(to);
      } catch (e) {
        console.log(`not forwarded to ${to}: ${e.message}`);
      }
    }
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/_edge/mail") return mail(request, env);
    if (url.hostname === "www.algo-rhythm.in") {
      url.hostname = "algo-rhythm.in";
      return Response.redirect(url.toString(), 301);
    }
    const origin = ORIGINS[url.hostname];
    if (!origin) return new Response("not found", { status: 404 });
    const headers = new Headers(request.headers);
    headers.set("x-forwarded-host", url.hostname);
    headers.set("x-client-ip", request.headers.get("cf-connecting-ip") || "");
    headers.set("x-edge-secret", env.EDGE_SECRET);
    url.hostname = origin;
    url.protocol = "https:";
    url.port = "";
    return fetch(new Request(url, { method: request.method, headers, body: request.body, redirect: "manual" }));
  },
};
