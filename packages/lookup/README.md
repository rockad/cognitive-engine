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

## Network shares

Declare a share in the manifest and it is indexed with nothing configured on the host:

```toml
[[source]]
name = "archive"
root = "/Volumes/knowledge-archive"   # LOGICAL path: stored and shown, never read from disk
include = ["*.md", "**/*.md"]
extractor = "docs.markdown"
visibility = "gated"

[source.volume]
type = "cifs"                         # or "smb"
device = "//core/knowledge-archive"   # //host/share
subpath = "wiki"                      # optional: index only this directory of the share
port = 445                            # optional
username = "${SHARE_USER}"            # references only: a literal value is a manifest error
password = "${SHARE_PASS}"
```

- **How it reads.** Every source read goes through a `FileSystem` (`vfs.py`); `LocalFS` is the disk and `SmbFS` (`smb.py`, over `smbprotocol`) is the share. A directory listing already carries size, modification time and the reparse flag, so a walk is one round trip per directory and an unchanged file is never opened: the second pass over an unchanged share performs zero reads and zero extra network calls (tested).
- **Logical paths.** Results show `root/...`, and hits from a share are marked `REMOTE` (use `get_document`, do not open the path). Existing manifests and an index migrated from the Docker version keep their paths.
- **Credentials.** `${VAR}` references only. A literal value, or an unset variable, fails at manifest load and names the variable. The password is held in memory and never appears in an error, a log line or a `repr`. Authentication is **NTLM only**: Kerberos needs system krb5 libraries, which is the host setup this removes. The old `options = "ro,vers=3.0"` is accepted and ignored.
- **Safety.** A logical path is mapped onto the share only after every segment is checked: `..`, a backslash (a separator on the wire), `:` (an NTFS alternate data stream) and NUL are refused. Reparse points (symlinks, junctions, mount points) are **never followed**: they are skipped, counted in the ingest result as `skipped_links` and named in its problems.
- **Failure.** An unreachable share keeps its rows, is named in the refresh report with the reason, and has its file ledger dropped so the next pass re-verifies every file. A listing that fails part-way raises `SourceUnreachable` and deletes nothing, so a dropped connection cannot read as "the other files were deleted"; the other sources still ingest.
- **Known library behaviour.** `smbprotocol` caches one session per host, port and user for the life of the process, so changing a password needs a restart to be re-validated. Different users on the same host are isolated.

## Differences from the Docker-era server

- **Network shares are read in userspace, not mounted.** The container used to mount SMB shares itself (`shares.py`, `lookup mounts`, `CAP_SYS_ADMIN`). Native Lookup reads a declared share over SMB directly, with no host mount, root or FUSE. See [Network shares](#network-shares).
- **Python 3.12.** The old package required 3.14, mostly for `PurePath.full_match` (3.13+). `lookup.manifest.full_match` replaces it and is tested against the stdlib reference.
- **FastMCP** replaces the hand-written JSON-RPC and HTTP server. `GET /healthz` went with it.

## Tests

```bash
uv run pytest packages/lookup
```

The ported suite is `unittest`-style and unchanged where the behaviour is unchanged; `test_mcp_server.py` is new and drives the four tools through FastMCP with all four visibility values, plus a real HTTP process for the loopback and `$HOME` guarantees.
