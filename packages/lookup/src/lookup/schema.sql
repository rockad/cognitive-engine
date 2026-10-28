-- Personal archive search index.
--
-- Two tables on purpose: `docs` holds metadata and the canonical body, `fts`
-- is an external-content FTS5 index over it. External content means the text
-- is stored once, not twice -- on a corpus this size that halves the on-disk
-- footprint and keeps BM25 ranking and snippet() available.

CREATE TABLE IF NOT EXISTS docs (
  id       INTEGER PRIMARY KEY,
  doc_key  TEXT NOT NULL UNIQUE,   -- stable hash: re-ingest updates, never duplicates
  source   TEXT NOT NULL,          -- gemini | mail | vault | activity | notebooklm | ...
  path     TEXT,                   -- where it came from, for provenance
  title    TEXT,
  date     TEXT,                   -- ISO-8601 where known, else NULL
  body     TEXT NOT NULL,
  meta     TEXT,                   -- JSON blob, source-specific extras
  bytes    INTEGER,                -- length of `body`, NOT the source file's size
  ingested TEXT NOT NULL,
  src_sha256 TEXT,                 -- sha256 of the SOURCE FILE at `path`; see below
  visibility TEXT                  -- this document's OWN value; NULL = inherit
);

-- `visibility` on a document, added 2026-09-09. NULL is the normal case and
-- means "inherit the source's value" -- not "unstated", which is a value a
-- document can state. It exists because visibility is not always a property of
-- a corpus: a Slack corpus holds company-wide channels and DMs together, and
-- one word for the whole source mislabels most of it either way.
--
-- A document's value comes from its own YAML front matter and nowhere else.
-- Deliberately not from a path rule (`channels/private/**` -> gated): a path
-- rule is a second inclusion list maintained beside the first, and those drift
-- -- the failure this project already had with the compose mounts.
--
-- ⚠️ A document may NARROW its source's value and may never WIDEN it. The check
-- is at ingest (`manifest.widens`), and a widening document is refused and
-- reported rather than written. Widening cannot be taken back, so it must not
-- be reachable by editing a file's front matter.

-- `src_sha256` exists so an external ledger can prove "nothing lost" by content
-- rather than by filename. It is sha256 of the bytes of the file named in
-- `path`, not of `body`: several documents extracted from one file (5,660
-- vcards from one .vcf) all carry that file's digest, so it identifies the
-- SOURCE, never the document. Document identity stays `doc_key` — see
-- db.doc_key for why hashing the body was tried and rejected.
--
-- NULL means "not hashed", which is not the same as "no hash exists": rows
-- written before this column, and rows whose source file has since moved or
-- gone, stay NULL. Any consumer must treat NULL as unknown and report its
-- coverage, never count it as a mismatch. `backfill-hashes` fills what it can.

CREATE INDEX IF NOT EXISTS docs_source ON docs(source);
CREATE INDEX IF NOT EXISTS docs_date   ON docs(date);
CREATE INDEX IF NOT EXISTS docs_sha    ON docs(src_sha256);

-- A per-source-FILE ledger, so a re-ingest does not have to READ a file to
-- discover it is unchanged.
--
-- ⚠️ This exists because the archive is a network share and a local copy is
-- not acceptable. `upsert_ex` already skips a byte-identical document, but it
-- learns that by reading the file and hashing it: ingesting the archive's 530
-- markdown files over SMB cost 99 s in a container and 44 s on the host,
-- against ~1 s from a local disk, and at a 300-second refresh that is a third
-- of the duty cycle spent re-reading a corpus that barely changes.
--
-- (source, path) -> mtime, size is a stat rather than a read. A file whose
-- mtime and size both match is skipped whole, extractor and all, and the
-- doc_keys it produced last time are read back from `keys` so pruning still
-- knows they were seen.
--
-- mtime+size, not sha256, precisely because the digest requires the read this
-- table exists to avoid. It is a cheaper signal and it can be fooled by a
-- write that preserves both; `ingest --rehash` forces the full read when that
-- matters, and `src_sha256` on `docs` is still the ledger's content join.
CREATE TABLE IF NOT EXISTS src_files (
  source TEXT NOT NULL,
  path   TEXT NOT NULL,
  mtime  REAL NOT NULL,
  size   INTEGER NOT NULL,
  keys   TEXT NOT NULL,            -- JSON array of the doc_keys this file produced
  seen   TEXT NOT NULL,            -- when this row was last confirmed
  PRIMARY KEY (source, path)
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  title,
  body,
  content='docs',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

-- unicode61 with diacritic folding matters here: the corpus is a mix of
-- English, Russian and Finnish, and Cyrillic must tokenise properly.

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO fts(fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
END;

-- UPDATE OF title, body -- not a bare UPDATE. `fts` indexes those two columns
-- and nothing else, so an update to bytes, ingested, path or src_sha256 has no
-- effect on the index, and re-writing the FTS row for it is pure cost: a
-- backfill touching one scalar column would otherwise rewrite the whole
-- external-content index. Existing databases carry the old bare-UPDATE trigger
-- and are moved over by db.migrate, which drops it so this can be recreated.
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF title, body ON docs BEGIN
  INSERT INTO fts(fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
  INSERT INTO fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- Run-level bookkeeping, so a re-run can report what changed.
CREATE TABLE IF NOT EXISTS runs (
  id        INTEGER PRIMARY KEY,
  started   TEXT NOT NULL,
  finished  TEXT,
  source    TEXT,
  added     INTEGER DEFAULT 0,
  updated   INTEGER DEFAULT 0,
  skipped   INTEGER DEFAULT 0,
  errors    INTEGER DEFAULT 0
);
