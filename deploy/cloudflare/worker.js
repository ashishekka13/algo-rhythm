// algo-rhythm.in in front of Cloud Run. Cloud Run has no custom domains in asia-south1, so this Worker forwards each
// request (WebSockets too) to the service's run.app address. It passes on the visitor's address (X-Client-IP) and the
// address they typed (X-Forwarded-Host) with EDGE_SECRET, a Worker secret also set on both services: the app believes
// those headers only with it (algo_rhythm/server.py from_edge). Deployed by deploy/cloudflare.sh.
const ORIGINS = {
  "algo-rhythm.in": "algo-rhythm-web-zakvaz2hfa-el.a.run.app",
  "staging.algo-rhythm.in": "algorythm-web-staging-zakvaz2hfa-el.a.run.app",
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
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
