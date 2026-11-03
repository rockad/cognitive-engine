# cognitive-lookup

Search over a declared set of user corpora: a SQLite FTS5 index, the extractors that feed it, and `docs.visibility` enforcement, served over MCP. Ported from the Docker-era Lookup in `agentic-hub` and made native: Python 3.12, `uv`, no container.

The engine (`db`, `manifest`, `pipeline`, `extract/`) is stdlib only. FastMCP is the transport and nothing else imports it.

## Run

```bash
uv run lookup mcp --http          # http://127.0.0.1:8100/mcp, stateless, re-ingests every 300 s
uv run lookup mcp                 # stdio
uv run lookup sources             # what the manifest declares
uv run lookup ingest --all
```

The HTTP bind is `127.0.0.1` and there is no `--host` flag: the tools return the text of documents classified `gated`, and nothing authenticates the caller. A forged `Host` header is refused (421) and so is a foreign `Origin` (403), so a browser tab cannot reach the daemon by DNS rebinding; tests pin both. A misspelled argument such as `visiblity` is a tool error, never silently ignored.

The MCP layer is FastMCP rather than the official SDK's `MCPServer`; both were compared behind the same handlers (RFC 003 §4, item 11). The engine does not import either, so the choice is confined to `mcp.py`.

Tools: `search_archive`, `get_document`, `refresh_index`, `archive_stats`.

## Paths

| variable | default |
| --- | --- |
| `LOOKUP_DB` | `~/.local/share/cognitive-engine/lookup/index.db` |
| `LOOKUP_MANIFEST` | `~/.config/cognitive-engine/lookup/sources.toml`, else the packaged `sources.example.toml` |

Copy `sources.example.toml` to the manifest path and edit it. **The manifest is the only declaration of what may be indexed**: there is no "everything except" mode, and the pipeline re-checks every document's resolved path against its declared root, refusing anything that escapes it, symlinks included.

## `docs.visibility`

A source declares `visibility = "open" | "gated" | "mixed"`; omitting it means `unstated`, which is handled as gated. A document may carry its own `visibility:` in YAML front matter, and:

- It may **narrow** its source's value and may never **widen** it. A widening document is refused at ingest and counted in `refused`.
- Every `search_archive` hit and every `get_document` reply carries the effective label. `mixed` describes a corpus, so it never labels a hit: a silent document in a `mixed` source is `unstated`.
- `search_archive(visibility=...)` filters in SQL, before `LIMIT`, using the same resolution as the labels.
- An unrecognised `visibility` value is a **tool error**. The legacy server dropped it and answered unfiltered; for the filter that keeps gated material out, silence was the wrong failure.
- Nothing is banned. Provenance decides the ceremony (verify, get sign-off) and not whether the material may be used.

## Differences from the Docker-era server

- **No network-share mounting.** The container used to mount SMB shares itself (`shares.py`, `lookup mounts`, `CAP_SYS_ADMIN`). A native process has no container to mount into, so a `[source.volume]` table is now metadata only: its `root` must already resolve on this machine. A declared `volume` whose root is absent is reported by name, as `UNMOUNTED`, in the startup log and in `refresh_index`, because an unmounted share is otherwise indistinguishable from an empty one. Declaring shares in the manifest and indexing them with no host setup is a proposed follow-up; see the Track 2 report in `docs/handoffs/antigravity-claude-bridge.md`.
- **Python 3.12.** The old package required 3.14, mostly for `PurePath.full_match` (3.13+). `lookup.manifest.full_match` replaces it and is tested against the stdlib reference.
- **FastMCP** replaces the hand-written JSON-RPC and HTTP server. `GET /healthz` went with it.

## Tests

```bash
uv run pytest packages/lookup
```

The ported suite is `unittest`-style and unchanged where the behaviour is unchanged; `test_mcp_server.py` is new and drives the four tools through FastMCP with all four visibility values, plus a real HTTP process for the loopback and `$HOME` guarantees.
