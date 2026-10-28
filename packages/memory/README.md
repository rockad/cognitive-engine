# cognitive-memory

Upstream [`basic-memory`](https://github.com/basicmachines-co/basic-memory) run natively under `uv`, with the reranker actually switched on and tuned, state kept out of the git tree, and nothing bound beyond loopback. No Docker.

This package is a launcher, not a fork: it computes a validated environment and `exec`s the upstream `basic-memory mcp` command. It imports nothing from upstream, so the AGPL-3.0 licence of `basic-memory` stays on the far side of a process boundary (RFC 001 Decision 23). The launcher itself is AGPL-3.0 as well, to match what it ships with.

## Run

```bash
uv run cognitive-memory serve            # http://127.0.0.1:8105/mcp, stateless streamable HTTP
uv run cognitive-memory serve --stdio    # for a harness that spawns its server
uv run cognitive-memory env              # print exactly what serve would hand to upstream
```

The first search downloads the `bge-small-en-v1.5` embedding model and the `jina-reranker-v1-tiny-en` cross-encoder (fastembed) into `<root>/config/fastembed_cache`, so the first run needs network once.

## Configuration

| variable | default | accepted | note |
| --- | --- | --- | --- |
| `COGNITIVE_MEMORY_ROOT` | `~/.local/share/cognitive-engine/memory` | absolute path outside the git checkout | refused otherwise |
| `COGNITIVE_MEMORY_PORT` | `8105` | 1024–65535 | the host is not configurable |
| `BASIC_MEMORY_RERANKER_CANDIDATES` | `10` | 5–10 | out of range is an error, not a clamp |
| `BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS` | `1000` | 500–1000 | out of range is an error, not a clamp |

State layout under the root:

```
config/   config.json, memory.db, fastembed_cache/   (BASIC_MEMORY_CONFIG_DIR)
notes/    the default project's markdown               (BASIC_MEMORY_HOME)
cache/    onnxruntime and Hugging Face scratch         (XDG_CACHE_HOME)
data/     library bookkeeping                          (XDG_DATA_HOME)
```

`BASIC_MEMORY_PROJECT_ROOT` is set to the root, so no project the daemon creates can live outside it.

## Why the launcher pins what it pins

Upstream 0.24.1 defaults differ from what RFC 003 assumes, and each difference is silent:

| setting | upstream default | here |
| --- | --- | --- |
| `reranker_enabled` | **off** | on |
| `reranker_candidates` | 20 | 10 |
| `reranker_max_document_chars` | 2000 | 1000 |
| `auto_update` | on | off (the version is pinned) |

Without this launcher a "tuned" daemon would run plain hybrid retrieval and still answer every call. Upstream also writes `~/.basic-memory` and creates `~/basic-memory` on any invocation, even `--help`; `BASIC_MEMORY_CONFIG_DIR` and `BASIC_MEMORY_HOME` stop that. Third-party libraries wrote into `$HOME` as well (onnxruntime's device-id database, Hugging Face logs, fastmcp's version-check cache), so the XDG dirs are redirected and fastmcp's update check is off.

## Tools

The upstream tool set is served unchanged, including `read_note`, `write_note`, `search_notes`, `delete_note`, `build_context`, `edit_note`, `move_note`, `recent_activity`, and the project tools.

**There is no `list_notes`.** RFC 003 names one; the upstream equivalent is `list_directory` (with `ls` as a shell-style alias). Nothing here renames it.

**`ping` is not answered.** The server returns `Method not found` for the MCP `ping` request, and the official SDK says why: ping was removed from the protocol as of 2026-07-28. Liveness is `tools/list`, or a TCP connect to the port.

**Forged `Host` and foreign `Origin` headers are refused** (421 and 403), which is what stops a browser tab reaching a loopback daemon that can write and delete notes. Upstream enables this; `test_server.py` pins it.

## Upgrading upstream

`basic-memory` is pinned exactly in `pyproject.toml`. The retrieval quality (the 9/10 past-context recall RFC 003 relies on) and every `BASIC_MEMORY_*` name above are properties of a release, so an upgrade is a deliberate change that re-runs the recall check and `uv run pytest packages/memory`, never a resolver decision. Note that 0.23.2, the version the legacy container ran, pins a prerelease `fastmcp` (`4.0.0b1`); 0.24.1 is the first stable-compatible pin.

## Tests

```bash
uv run pytest packages/memory
```

`test_settings.py` covers the bounds, the out-of-tree rule and the loopback-only argv. `test_server.py` starts the real daemon on a free port with a fake `$HOME`, drives it over MCP, and asserts the listening socket, the note lifecycle, that `$HOME` stays empty, and that resident memory stays under 3 GB after sustained searches (about 730 MiB measured).
