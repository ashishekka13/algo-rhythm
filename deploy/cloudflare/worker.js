// algo-rhythm.in in front of Cloud Run. Cloud Run has no custom domains in asia-south1, so this Worker forwards each
// request (WebSockets too) to the service's run.app address. It passes on the visitor's address (X-Client-IP) and the
// address they typed (X-Forwarded-Host) with EDGE_SECRET, a Worker secret also set on both services: the app believes
// those headers only with it (algo_rhythm/server.py from_edge). Deployed by deploy/cloudflare.sh.
//
// POST /_edge/mail (with EDGE_SECRET) emails support: the services send {subject, text, html} (algo_rhythm/notify.py)
// and Cloudflare Email Routing delivers it from no-reply@ to MAIL_TO, support's verified inbox (Email Routing can
// send only to verified destination addresses; support@ forwards there too).
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
export function mime({ subject, text, html }, to, id = crypto.randomUUID(), date = new Date()) {
  const boundary = "b" + id.replace(/-/g, "");
  const part = (type, body) => [`--${boundary}`, `Content-Type: ${type}; charset=UTF-8`,
    "Content-Transfer-Encoding: base64", "", base64(body).replace(/.{1,76}/g, "$&\r\n").trimEnd()];
  return [`From: algo-rhythm <${FROM}>`, `To: ${to}`, `Subject: =?UTF-8?B?${base64(subject)}?=`,
    `Message-ID: <${id}@algo-rhythm.in>`, `Date: ${date.toUTCString()}`, "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`, "",
    ...part("text/plain", text), ...part("text/html", html), `--${boundary}--`, ""].join("\r\n");
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
  try {
    await env.SUPPORT_MAIL.send(new EmailMessage(FROM, env.MAIL_TO, mime(m, "support@algo-rhythm.in")));
  } catch (e) {
    return new Response(`not sent: ${e.message}`, { status: 502 });
  }
  return Response.json({ ok: true });
}

export default {
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
