> **Moved from `rockad/agentic-hub` on 2026-11-10**, scrubbed of machine-specific details. Current state: [`plans/2026-11-10-delivery-plan.md`](plans/2026-11-10-delivery-plan.md). Links to `plugins/...` refer to the `agentic-hub` repository.

# Port map

One table for every TCP port used by the agentic-hub, cognitive-engine and recall projects on a workstation or on the storage host. Decided 2026-11-09 (owner: Aleksandr).

## Rules

- **Production ports are authoritative.** When a production port and an eval port collide, the production port keeps the number and the eval stack moves.
- **Eval-stack ports are overridable at run time.** They are defaults in `recall/eval/` compose files, chosen not to collide with each other; nothing may depend on them.
- **Coordination is tailnet-only on 8120.** The remote-peer listener binds `127.0.0.1:8121` from stage s2 (task S2-LISTEN). The Cloudflare Tunnel route is the exposure gate: no route exists before the S3 gate and the owner's explicit go.
- **Native lookup stages on 8101 and rebinds to 8100 at Track 4 cutover**, after the legacy Docker lookup is stopped and share-source parity is verified (cognitive-engine RFC 003, section 4 item 5).
- **Everything binds loopback by default.** The only non-loopback exposure is coordination on the tailnet (8120 on the storage host).
- **cognitive-engine model sessions (sbx) must never reach 8120 or 8121**, at any address.

## Table

| Port | Service | Host | Owner or repo | Status | Notes |
|---|---|---|---|---|---|
| 8100 | lookup (legacy Docker, `lookup-lookup-1`) | workstation, `127.0.0.1` | `agentic-hub` `plugins/memory` (compose service `lookup`, `LOOKUP_PORT`) | production (legacy) | Becomes native lookup (`cognitive-engine` `packages/lookup`) at Track 4 cutover. The `ghcr.io/rockad/lookup` image is an INTERIM artifact removed with `plugins/memory` in RFC 005 Phase C. |
| 8101 | lookup (native, staging) | workstation, `127.0.0.1` | `cognitive-engine` `packages/lookup` (RFC 003, #8) | staging | Run as `lookup mcp --http --port 8101` while the legacy container holds 8100. Rebinds to 8100 at cutover; the code default is already 8100. |
| 8101 | Graphiti MCP (`/mcp`) | workstation, `localhost` | `recall` `eval/graphiti` | eval-only | Same number as native lookup staging; the two must not run together, and the eval port is overridable. |
| 8105 | memory (native Basic Memory, `/mcp`) | workstation, `127.0.0.1` | `cognitive-engine` `packages/memory` (RFC 003) | production (native) | Collides with nothing. |
| 8110 | recall (legacy Docker, Basic Memory 0.24.1, `/mcp`) | workstation, `127.0.0.1` | `agentic-hub` `plugins/memory` (compose service `recall`, `RECALL_PORT`) | production (legacy) | Retired with `plugins/memory` in RFC 005 Phase C. |
| 8110 | Basic Memory (eval, `/sse`) | workstation, `localhost` | `recall` `eval/basic-memory` | eval-only | Overridable; do not run alongside the legacy recall container. |
| 8115 | jev (Streamable HTTP, `/mcp`) | workstation, `127.0.0.1` | `cognitive-engine` `packages/jev` | production (optional) | Stdio is the default transport; HTTP on 8115 only when supervised by `engine up` (RFC 001 decisions 15 and 24). As of 2026-11-12 the live Jev line is the agentic-hub plugin (2.3.x); `packages/jev` 2.4.0 is the frozen reference (delivery plan, decisions 14, 15, 19). |
| 8120 | coordination server (`a2a-coord`) | the storage host in production (tailnet-only); the workstation for S1 dev (`127.0.0.1` only) | `agentic-hub` `plugins/coordination` | production (planned; S1 not yet shipped) | Serves `/mcp`, `/a2a/v1`, `/wake/listen`, `/sessions`, `/status` and the Agent Card. Tailnet only, never public. |
| 8120 | mcp-memory-service (dashboard and MCP) | workstation, `127.0.0.1` | `recall` `eval/mcp-memory-service` | eval-only | Collides with coordination; the eval port is overridable and must be moved when coordination runs on the same host. |
| 8121 | coordination remote-peer listener | the storage host, `127.0.0.1` | `agentic-hub` `plugins/coordination` | staging (planned from stage s2) | Binds loopback from s2 (task S2-LISTEN, R8 approved 2026-11-09). Serves `remote-peer` keys only. The tunnel target `localhost:8121` is routed at S3 only; no route exists now. |
| 8125 | Harness2 (previously defaulted to 8120) | workstation | `cognitive-engine` (#5, change pending on restack) | production (planned) | Decided 2026-11-08 in `docs/pr-plan-2026-11-08.md`. UNVERIFIED in `cognitive-engine` code: the 8125 default is not yet in the tree and lands when #5 is restacked. |
| 3002 | Graphiti FalkorDB browser | workstation, `localhost` | `recall` `eval/graphiti` | eval-only | Overridable. |
| none | coordination Postgres | the storage host or the workstation, compose network only | `agentic-hub` `plugins/coordination` | production (planned from S1) | Publishes no port (decision 37). Reachable only from the server and the `pg_dump` sidecar inside the compose project. |

## How to add a port

1. Pick a number not in the table and not already listening on the target host (`ss -tlnp`).
2. Bind `127.0.0.1` unless the service is meant to be reached over the tailnet; state that exposure in the notes.
3. Add one row here in the same commit that introduces the port, with owner, status and any collision.
4. Use eval or staging status for anything not yet in daily use; use production only for what clients are registered against.
5. If the new port collides with an eval port, the eval port moves, not the production port.

## Sources

- `plugins/memory/docker-compose.yml` for 8100 and 8110 defaults.
- `recall/eval/README.md` (endpoint table) for 8101, 8110, 8120 and 3002 eval defaults.
- `plugins/coordination/AGENTS.md` and `README.md` for 8120, 8121 and the Postgres rule (decision 37 in `plugins/coordination/docs/plans/consolidated-bridge-v2-plan.md`).
- `cognitive-engine` RFC 003 on `origin/main` and `origin/feat/track2-memory-lookup` for 8105 and the 8101 staging resolution; RFC 001 decisions 15 and 24 for 8115.
