#!/usr/bin/env bash
# Email to users (algo_rhythm.notify send_user) through Resend, from no-reply@algo-rhythm.in. Each step is safe to run
# again. Cloudflare Email Routing still receives support@; Resend's records live on send. and resend._domainkey, so they
# do not touch the root's MX and SPF.
#
#   deploy/resend.sh domain    register the domain with Resend, add its DNS records in Cloudflare, ask Resend to verify
#   deploy/resend.sh check     the domain's status and each record's (read-only)
#   deploy/resend.sh key       a sending-only key for this domain, into Secret Manager as resend-api-key (never printed);
#                              then deploy the services (deploy/gcp.sh server), which pass it on as RESEND_API_KEY
#
# Keys: ~/.config/resend/key (a full-access Resend key, for these steps only) and ~/.config/cloudflare/token.
set -euo pipefail

DOMAIN=${DOMAIN:-algo-rhythm.in}
PROJECT=${PROJECT:-song-mixer-511123}
REGION=${RESEND_REGION:-ap-northeast-1}          # Tokyo: the closest Resend region to India
SA=${SA:-algo-rhythm-run@$PROJECT.iam.gserviceaccount.com}
RKEY=$(cat ~/.config/resend/key 2>/dev/null || true)
CKEY=$(cat ~/.config/cloudflare/token 2>/dev/null || true)
[ -n "$RKEY" ] || { echo "no Resend key in ~/.config/resend/key" >&2; exit 1; }

resend() {  # resend METHOD PATH [JSON] -> response JSON; exits on an HTTP error
  local out code
  out=$(curl -sS -w '\n%{http_code}' -X "$1" "https://api.resend.com$2" -H "Authorization: Bearer $RKEY" \
    -H "Content-Type: application/json" ${3:+--data "$3"})
  code=${out##*$'\n'}; out=${out%$'\n'*}
  [[ $code == 2* ]] || { echo "Resend refused $1 $2: HTTP $code $out" >&2; exit 1; }
  printf '%s' "$out"
}
cf() {  # cf METHOD PATH [JSON] -> Cloudflare's "result"
  curl -sS -X "$1" "https://api.cloudflare.com/client/v4$2" -H "Authorization: Bearer $CKEY" \
    -H "Content-Type: application/json" ${3:+--data "$3"} | python3 -c 'import json, sys
d = json.load(sys.stdin)
if not d.get("success"):
    sys.exit("Cloudflare refused: " + "; ".join("%s %s" % (e.get("code"), e.get("message")) for e in d.get("errors", [])))
print(json.dumps(d["result"]))'
}
py() { python3 -c "$1" "${@:2}"; }

domain_id() {
  resend GET /domains | py 'import json, sys
print(next((d["id"] for d in json.load(sys.stdin)["data"] if d["name"] == sys.argv[1]), ""))' "$DOMAIN"
}

case "${1:-}" in
domain)
  [ -n "$CKEY" ] || { echo "no Cloudflare token in ~/.config/cloudflare/token" >&2; exit 1; }
  ID=$(domain_id)
  [ -n "$ID" ] || ID=$(resend POST /domains "{\"name\": \"$DOMAIN\", \"region\": \"$REGION\"}" | py 'import json, sys; print(json.load(sys.stdin)["id"])')
  ZONE=$(cf GET "/zones?name=$DOMAIN" | py 'import json, sys; print(json.load(sys.stdin)[0]["id"])')
  # each record Resend asks for, added to Cloudflare unless one with that name, type and content is there already
  resend GET "/domains/$ID" | py 'import json, sys
for r in json.load(sys.stdin)["records"]:
    name = r["name"] if r["name"].endswith(sys.argv[1]) else r["name"] + "." + sys.argv[1]
    print(json.dumps({"type": r["type"], "name": name, "content": r["value"], "ttl": 1, "proxied": False,
                      "comment": "Resend", **({"priority": int(r["priority"])} if r.get("priority") is not None else {})}))' "$DOMAIN" |
  while read -r REC; do
    NAME=$(py 'import json, sys; print(json.loads(sys.argv[1])["name"])' "$REC")
    TYPE=$(py 'import json, sys; print(json.loads(sys.argv[1])["type"])' "$REC")
    HAVE=$(cf GET "/zones/$ZONE/dns_records?name=$NAME&type=$TYPE" | py 'import json, sys
want = json.loads(sys.argv[1])["content"].strip("\"")
print("yes" if any(x["content"].strip("\"") == want for x in json.load(sys.stdin)) else "")' "$REC")
    if [ -n "$HAVE" ]; then echo "dns $TYPE $NAME already there"; continue; fi
    cf POST "/zones/$ZONE/dns_records" "$REC" >/dev/null
    echo "dns $TYPE $NAME added"
  done
  resend POST "/domains/$ID/verify" >/dev/null
  echo "asked Resend to verify $DOMAIN (deploy/resend.sh check shows when it is verified; usually minutes)"
  ;;
check)
  ID=$(domain_id); [ -n "$ID" ] || { echo "$DOMAIN is not registered with Resend: run deploy/resend.sh domain"; exit 1; }
  resend GET "/domains/$ID" | py 'import json, sys
d = json.load(sys.stdin)
print("%s: %s (region %s)" % (d["name"], d["status"], d.get("region")))
for r in d["records"]:
    print("  %-5s %-28s %s" % (r["type"], r["name"], r.get("status")))'
  ;;
key)
  ID=$(domain_id); [ -n "$ID" ] || { echo "run deploy/resend.sh domain first" >&2; exit 1; }
  G="gcloud --project=$PROJECT"
  $G secrets describe resend-api-key >/dev/null 2>&1 || \
    $G secrets create resend-api-key --replication-policy=automatic >/dev/null
  resend POST /api-keys "{\"name\": \"algo-rhythm server\", \"permission\": \"sending_access\", \"domain_id\": \"$ID\"}" |
    py 'import json, sys; print(json.load(sys.stdin)["token"], end="")' |
    $G secrets versions add resend-api-key --data-file=- >/dev/null
  $G secrets add-iam-policy-binding resend-api-key --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  echo "resend-api-key: a new sending-only key for $DOMAIN (deploy the services to use it)"
  ;;
*)
  sed -n '2,13p' "$0"; exit 1
  ;;
esac
