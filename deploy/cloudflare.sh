#!/usr/bin/env bash
# algo-rhythm.in on Cloudflare: DNS and the Worker that forwards to Cloud Run (deploy/cloudflare/worker.js).
# The registrar (GoDaddy) only names Cloudflare's nameservers; everything else is here. Each step is safe to run again.
#
#   deploy/cloudflare.sh check     the zone, its DNS records and Worker routes (read-only)
#   deploy/cloudflare.sh worker    upload the Worker, with EDGE_SECRET from Secret Manager (deploy/gcp.sh edge-secret)
#                                  and the email binding (support's inbox must be a verified Email Routing address)
#   deploy/cloudflare.sh routes    send the domain, www and staging through the Worker
#   deploy/cloudflare.sh dns       a proxied record for each (the Worker answers them; no origin of their own)
#
# Token: env CLOUDFLARE_API_TOKEN, else ~/.config/cloudflare/token. It needs Zone DNS Edit, Zone Workers Routes Edit
# and Account Workers Scripts Edit, for this zone only. Workers' free plan: 100,000 requests a day.
set -euo pipefail

DOMAIN=${DOMAIN:-algo-rhythm.in}
PROJECT=${PROJECT:-song-mixer-511123}
SCRIPT=${SCRIPT:-algo-rhythm-edge}
MAIL_TO=${MAIL_TO:-support.algo.rhythm@gmail.com}  # support@ forwards here (Email Routing); the Worker emails it
# Cc'd on the signup email, and sent a copy of all mail to support@ (comma-separated). Each must be a verified destination address in
# Cloudflare Email Routing (dashboard: Email > Email Routing > Destination addresses), or Cloudflare refuses it.
MAIL_CC=${MAIL_CC-ashishekka.work@gmail.com,smv2816@gmail.com}
HOSTS=("$DOMAIN" "www.$DOMAIN" "staging.$DOMAIN")
HERE=$(cd "$(dirname "$0")" && pwd)
TOKEN=${CLOUDFLARE_API_TOKEN:-$(cat ~/.config/cloudflare/token 2>/dev/null || true)}
[ -n "$TOKEN" ] || { echo "no token: set CLOUDFLARE_API_TOKEN or write it to ~/.config/cloudflare/token" >&2; exit 1; }

api() {  # api METHOD PATH [curl args...] -> the JSON "result"; exits with Cloudflare's errors when it refuses
  local method=$1 path=$2; shift 2
  curl -sS -X "$method" "https://api.cloudflare.com/client/v4$path" -H "Authorization: Bearer $TOKEN" "$@" |
    python3 -c 'import json, sys
d = json.load(sys.stdin)
if not d.get("success"):
    sys.exit("Cloudflare refused: " + "; ".join("%s %s" % (e.get("code"), e.get("message")) for e in d.get("errors", [])))
print(json.dumps(d["result"]))'
}
field() { python3 -c 'import json, sys; v = json.load(sys.stdin); print(eval(sys.argv[1], {}, {"r": v}))' "$1"; }

ZONE_JSON=$(api GET "/zones?name=$DOMAIN")
ZONE=$(field 'r[0]["id"] if r else ""' <<<"$ZONE_JSON")
[ -n "$ZONE" ] || { echo "$DOMAIN is not a zone this token can see" >&2; exit 1; }
ACCOUNT=$(field 'r[0]["account"]["id"]' <<<"$ZONE_JSON")

case "${1:-}" in
check)
  echo "zone $DOMAIN: $(field 'r[0]["status"]' <<<"$ZONE_JSON"), nameservers $(field '" ".join(r[0]["name_servers"])' <<<"$ZONE_JSON")"
  echo "-- DNS"
  api GET "/zones/$ZONE/dns_records?per_page=100" |
    field '"\n".join("%-6s %-32s %-40s proxied=%s" % (x["type"], x["name"], x["content"], x["proxied"]) for x in r) or "(none)"'
  echo "-- Worker routes"
  api GET "/zones/$ZONE/workers/routes" | field '"\n".join("%s -> %s" % (x["pattern"], x.get("script")) for x in r) or "(none)"'
  ;;
worker)
  META=$(mktemp); chmod 600 "$META"; trap 'rm -f "$META"' EXIT
  gcloud --project="$PROJECT" secrets versions access latest --secret=edge-secret 2>/dev/null |
    python3 -c 'import json, sys
print(json.dumps({"main_module": "worker.js", "compatibility_date": "2026-10-01",
                  "bindings": [{"type": "secret_text", "name": "EDGE_SECRET", "text": sys.stdin.read()},
                               {"type": "send_email", "name": "SUPPORT_MAIL"},
                               {"type": "plain_text", "name": "MAIL_TO", "text": sys.argv[1]},
                               {"type": "plain_text", "name": "MAIL_CC", "text": sys.argv[2]}]}))' "$MAIL_TO" "$MAIL_CC" > "$META"
  python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1]))["bindings"][0]["text"] else "edge-secret is empty: run deploy/gcp.sh edge-secret")' "$META"
  api PUT "/accounts/$ACCOUNT/workers/scripts/$SCRIPT" \
    -F "metadata=@$META;type=application/json" \
    -F "worker.js=@$HERE/cloudflare/worker.js;type=application/javascript+module" >/dev/null
  echo "Worker $SCRIPT uploaded"
  ;;
routes)
  HAVE=$(api GET "/zones/$ZONE/workers/routes" | field '" ".join(x["pattern"] for x in r)')
  for h in "${HOSTS[@]}"; do
    case " $HAVE " in *" $h/* "*) echo "route $h/* already there"; continue;; esac
    api POST "/zones/$ZONE/workers/routes" -H "Content-Type: application/json" \
      --data "{\"pattern\": \"$h/*\", \"script\": \"$SCRIPT\"}" >/dev/null
    echo "route $h/* -> $SCRIPT"
  done
  ;;
dns)
  # 100:: is the documented placeholder for a name only a Worker answers. A record already there for a name (e.g. the
  # registrar's parking page, copied when the zone was added) is left alone: remove it in the dashboard first.
  for h in "${HOSTS[@]}"; do
    OLD=$(api GET "/zones/$ZONE/dns_records?name=$h" | field '" ".join("%s:%s" % (x["type"], x["content"]) for x in r)')
    if [ "$OLD" = "AAAA:100::" ]; then echo "dns $h already there"; continue; fi
    if [ -n "$OLD" ]; then echo "dns $h has other records ($OLD): remove them first" >&2; exit 1; fi
    api POST "/zones/$ZONE/dns_records" -H "Content-Type: application/json" \
      --data "{\"type\": \"AAAA\", \"name\": \"$h\", \"content\": \"100::\", \"proxied\": true, \"comment\": \"Worker $SCRIPT\"}" >/dev/null
    echo "dns $h -> Worker (proxied)"
  done
  ;;
*)
  sed -n '2,11p' "$0"; exit 1
  ;;
esac
