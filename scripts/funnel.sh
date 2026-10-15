#!/usr/bin/env bash
# Funnel demo switch for the SpoiledDuckie shop. Demo only: no real money moves
# here by construction (demo mode + fake rail), and Funnel exposes the web edge
# (Caddy :80) plus nothing else — never Postgres, never raw service ports.
#
#   scripts/funnel.sh up      # daemon, overrides, funnel on, verify
#   scripts/funnel.sh down    # funnel off, stock compose restored
#   scripts/funnel.sh status  # funnel + containers at a glance
#
# Artifacts live in /tmp (generated Caddyfile, compose overrides). The repo
# Caddyfile and docker-compose.yml are never touched.
set -euo pipefail

TS_BIN="$HOME/.local/bin/tailscale"
TS_SOCK=/tmp/ts-state/tailscaled.sock
DAEMON=/home/apollo/.local/bin/tailscaled
STATE_DIR=/tmp/ts-state
COMPOSE=(docker compose -f docker-compose.yml)
FUNNEL_CADDY=/tmp/opencode-funnel-Caddyfile
FUNNEL_YML=/tmp/opencode-funnel.yml
ORIGIN_YML=/tmp/opencode-origin.yml

ts() { "$TS_BIN" --socket="$TS_SOCK" "$@"; }

need_login() {
	ts status >/dev/null 2>&1 || {
		echo "Tailscale is logged out. Run:"
		echo "  $TS_BIN --socket=$TS_SOCK up"
		echo "then re-run this script."
		exit 1
	}
}

up() {
	# 1. Daemon (rootless, cert storage explicit — without --statedir the node
	#    cannot terminate funnel TLS: "no TailscaleVarRoot").
	if ! ts status >/dev/null 2>&1; then
		if ! pgrep -f "tailscaled --state=$STATE_DIR" >/dev/null; then
			mkdir -p "$STATE_DIR"
			nohup "$DAEMON" --state="$STATE_DIR/tailscaled.state" --socket="$TS_SOCK" \
				--statedir="$STATE_DIR" --tun=userspace-networking >>"$STATE_DIR/tailscaled.log" 2>&1 &
			sleep 6
		fi
	fi
	need_login
	HOST="$(ts status --json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["Self"]["HostName"] + "." + d["MagicDNSSuffix'])')"
	echo "Funnel host: https://$HOST"

	# 2. Caddy block for the funnel host (repo Caddyfile untouched).
	sed "s/__FUNNEL_HOST__/$HOST/" /tmp/opencode-funnel-Caddyfile.template >"$FUNNEL_CADDY"
	{
		sed -n '1,28p' Caddyfile
		echo
		cat "$FUNNEL_CADDY"
	} >"$FUNNEL_CADDY.full" && mv "$FUNNEL_CADDY.full" "$FUNNEL_CADDY"
	cat >"$FUNNEL_YML" <<YML
services:
  caddy:
    volumes:
      - $FUNNEL_CADDY:/etc/caddy/Caddyfile:ro
YML

	# 3. Sidecar advertises the funnel origin (cards, OAuth, approve URLs).
	if [ ! -f "$ORIGIN_YML" ]; then
		cat >"$ORIGIN_YML" <<YML
services:
  sidecar:
    ports:
      - "127.0.0.1:18080:8000"
    environment:
      OPENSTORE_PUBLIC_ORIGIN: https://$HOST
YML
	else
		sed -i "s|OPENSTORE_PUBLIC_ORIGIN: .*|OPENSTORE_PUBLIC_ORIGIN: https://$HOST|" "$ORIGIN_YML"
	fi

	# 4. Recreate edge + sidecar, funnel on, verify.
	"${COMPOSE[@]}" -f "$FUNNEL_YML" -f "$ORIGIN_YML" up -d caddy sidecar
	sleep 8
	ts funnel --bg "http://127.0.0.1:80" 2>&1 | tail -3
	sleep 10
	curl -s --max-time 20 "https://$HOST/.well-known/agent-commerce.json" |
		python3 -c "import sys,json; d=json.load(sys.stdin); print('LIVE:', d['endpoints']['mcp'])"
}

down() {
	ts funnel off 2>/dev/null || true
	# Stock compose back (repo files were never modified, so this is just a
	# recreate without the /tmp overrides). Data volumes are untouched.
	"${COMPOSE[@]}" up -d caddy sidecar
	echo "Funnel off. Local demo still runs on *.localhost."
	echo "To also log out: $TS_BIN --socket=$TS_SOCK logout"
}

status() {
	ts funnel status 2>&1 | head -5
	"${COMPOSE[@]}" ps caddy sidecar 2>&1 | tail -3
}

case "${1:-status}" in
up | down | status) "$1" ;;
*) echo "usage: $0 [up|down|status]" >&2; exit 1 ;;
esac
