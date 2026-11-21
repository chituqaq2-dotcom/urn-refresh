# urn-refresh (private)

Keeps 5 clean US home-broadband URnetwork SOCKS exits alive and publishes the subscription
into Cloudflare Workers KV (`urn-sub-kv`); Worker `urn-sub` (urn.08010613.xyz/<key>) serves it.

- Schedule: every 6 h (`.github/workflows/refresh.yml`), plus manual "Run workflow".
- Secrets: `URNETWORK_API_KEY` (ur.io API key), `CF_API_TOKEN` (CF token, Workers KV Storage: Edit).
- No tokens in git: node state (client ids / auth tokens) lives only in KV key `state`.
- KV lock key prevents overlapping runs (Hark job / Action).
- `worker_kv.js.txt`: reference copy of the Worker (subscription key redacted).
