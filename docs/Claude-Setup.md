# Connect a shop to Claude (custom connector, paste URL)

Tested against Claude custom connectors using remote MCP (Pro/Max _Customize > Connectors > + > Add custom connector_). The shop speaks MCP JSON-RPC 2.0 (`2025-06-18`) at `/agent/mcp` with OAuth 2.1 + Dynamic Client Registration — no config file, no pasted token.

## Setup (no code, no token paste)

1. In Claude: _Customize > Connectors > + > Add custom connector_.
2. URL: `https://<shop-domain>/agent/mcp`.
3. Choose **Sign in now** (or connect). Claude discovers `/.well-known/oauth-authorization-server`, registers itself, and opens this shop's consent page.
4. Click **Allow** ("search, build baskets, start checkouts — never spend").
5. Done. Ask _"Search this shop for tote"_. Browsing works instantly; baskets work on the same login — tokens refresh silently, nothing to re-paste. The shop offers 15 tools; `set-details` carries the shop's own extras.

No OAuth Client ID to add manually, no Request headers. If Claude shows _"Couldn't register with sign-in service"_, the shop is unreachable from Claude's cloud (tunnel down?) or an old quick-tunnel URL is cached — remove and re-add the connector.

## What pasting the URL gives you (no login at all)

- `initialize`, `tools/list`, `search`, `read-item` — public, IP-rate-limited, buckets not counts. Same bytes as `GET /agent/tools` and `/agent/feed.json`.

## Buying (same login, one human tap)

Claude builds the basket and calls `place-order`, which returns an `approve_url` on the shop domain — open it, approve the exact total. No payment credential ever lives in Claude. OAuth tokens carry the same four scopes at self-registered tier; the Gate still refuses spend without fresh human Authority (`upi-pin`/`passkey`/`confirmed-intent`).

## Manual token path (testing only, 15-min TTL)

If OAuth is unreachable, mint a Bearer and put it in Request headers (`Authorization: Bearer <token>`) — web app only; Desktop/Code use config/`--header` instead:
`POST https://<shop>/agent/token {"client_id":"demo-agent","client_secret":"<secret>"}` or self-register via `POST /agent/register` (public profile, no merchant action).

## Limits (shared with all agents)

- IP: 120/min. Per-agent: 30/min self-registered, 300/min allowlisted. Registration: 5/hour/IP.
- Search pages: 50 default, 200 max. Refusals are named codes (`price-changed`, `rate-limited`, etc.), never silent trims.

## Why Claude can't spend

`place-order` returns a link, never an order. The Gate refuses spend without fresh human Authority on the merchant domain. Reputation buys throughput only.
