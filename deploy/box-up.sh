#!/usr/bin/env bash
# Bring FitCheck up on the Grok Bot box: dockerd (no iptables) -> compose (host-network override)
# -> Cloudflare quick tunnel -> point AUTH_URL/PUBLIC_URL at the tunnel URL and restart web+api.
# Box-specific. On a real host just use `docker compose up -d` (see docs/DEPLOY.md).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/workspace/dockerbin/docker:/workspace/dockerbin:$PATH
RUN=/workspace/fitcheck-run; mkdir -p "$RUN"
DC="sudo env PATH=$PATH docker compose -f docker-compose.yml -f docker-compose.box.yml"

if ! sudo env PATH=$PATH docker info >/dev/null 2>&1; then
  echo "starting dockerd"
  sudo setsid env PATH=$PATH dockerd --iptables=false --ip6tables=false --data-root /var/lib/docker-fc \
    >/workspace/dockerbin/dockerd.log 2>&1 < /dev/null &
  for _ in $(seq 30); do sudo env PATH=$PATH docker info >/dev/null 2>&1 && break; sleep 1; done
fi

# Tunnel: the box's DNS returns a proxy IP for argotunnel.com, so dial Cloudflare edge IPs directly over http2.
if [ ! -s "$RUN/tunnel.pid" ] || ! kill -0 "$(cat "$RUN/tunnel.pid")" 2>/dev/null; then
  setsid nohup /workspace/bin/cloudflared tunnel --no-autoupdate --protocol http2 \
    --edge 198.41.192.7:7844 --edge 198.41.200.13:7844 --edge 198.41.192.27:7844 --edge 198.41.200.33:7844 \
    --url http://127.0.0.1:3000 > "$RUN/tunnel.log" 2>&1 < /dev/null &
  echo $! > "$RUN/tunnel.pid"
  for _ in $(seq 30); do grep -q "Registered tunnel connection" "$RUN/tunnel.log" && break; sleep 1; done
fi
URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$RUN/tunnel.log" | tail -1)
[ -n "$URL" ] || { echo "no tunnel URL; see $RUN/tunnel.log"; exit 1; }

python3 deploy/init_env.py --public-url "$URL" >/dev/null   # fills missing secrets, never prints them
$DC up -d
$DC up -d --force-recreate --no-deps backend frontend >/dev/null   # pick up the (possibly new) URL
echo "FitCheck: $URL"
