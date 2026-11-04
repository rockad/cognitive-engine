import { Database } from 'bun:sqlite';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import * as crypto from 'node:crypto';
import { getCacheConfig } from './config.js';

export const DEFAULT_SHM_CACHE_FILE = '/dev/shm/jev/cache.sqlite';
export const DEFAULT_FALLBACK_CACHE_FILE = path.join(os.homedir(), '.config', 'jev', 'cache.sqlite');
/** Short TTL (5 minutes) to deduplicate identical prompts between pre-invocation hooks and main agent turns */
export const DECISION_CACHE_TTL_MS = 5 * 60 * 1000;
export const DECISION_CACHE_TTL_SEC = 300;

export interface DecisionCacheEntry {
  at: number;
  data: any;
}

/**
 * Dynamic Tiered Path Resolution:
 * 1) process.env.JEV_CACHE_FILE override.
 * 2) shmPath from getCacheConfig() (Linux/WSL RAM tmpfs). If directory doesn't exist, create it. Verify write accessibility.
 * 3) fallbackPath from getCacheConfig() (disk fallback).
 * 4) :memory: fail-safe.
 */
export function resolveDbPath(): string {
  if (process.env.JEV_CACHE_FILE) {
    return process.env.JEV_CACHE_FILE;
  }

  const cacheCfg = getCacheConfig();
  const shmPath = cacheCfg.shmPath;
  const fallbackPath = cacheCfg.fallbackPath;

  // 2) shmPath (RAM tmpfs on Linux/WSL)
  try {
    const shmDir = path.dirname(shmPath);
    if (fs.existsSync(path.dirname(shmDir)) || fs.existsSync(shmDir)) {
      if (!fs.existsSync(shmDir)) {
        fs.mkdirSync(shmDir, { recursive: true, mode: 0o700 });
      } else {
        try {
          fs.chmodSync(shmDir, 0o700);
        } catch {}
      }

      // Owner verification: ensure directory is owned by the current process user
      if (typeof process.getuid === 'function') {
        const stat = fs.statSync(shmDir);
        if (stat.uid !== process.getuid()) {
          throw new Error('shm directory is owned by another user');
        }
      }

      fs.accessSync(shmDir, fs.constants.W_OK | fs.constants.R_OK);
      return shmPath;
    }
  } catch {
    // fall through to fallbackPath
  }

  // 3) fallbackPath (disk fallback)
  try {
    const configDir = path.dirname(fallbackPath);
    if (!fs.existsSync(configDir)) {
      fs.mkdirSync(configDir, { recursive: true, mode: 0o700 });
    }
    fs.accessSync(configDir, fs.constants.W_OK | fs.constants.R_OK);
    return fallbackPath;
  } catch {
    // fall through
  }

  // 4) :memory: fail-safe
  return ':memory:';
}

/** Backward-compatible alias for resolveDbPath */
export function resolveCacheFilePath(): string {
  return resolveDbPath();
}

export const JEV_CACHE_FILE = resolveDbPath();

let activeDb: Database | null = null;
let activeDbPath = '';

function isJsonPath(filePath: string): boolean {
  return filePath.endsWith('.json');
}

/**
 * Get or initialize the bun:sqlite Database instance.
 * Sets busy timeout FIRST to prevent SQLITE_BUSY on first-open contention,
 * then sets WAL mode, normal synchronous, and in-memory temp store.
 */
function getDatabase(): Database {
  const dbPath = resolveDbPath();
  if (activeDb && activeDbPath === dbPath) {
    return activeDb;
  }

  if (activeDb) {
    try {
      activeDb.close();
    } catch {}
    activeDb = null;
  }

  if (dbPath !== ':memory:') {
    const dir = path.dirname(dbPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    }
  }

  const db = new Database(dbPath, { create: true });

  // SQLite DB file permissions: ensure the .sqlite file is mode 0o600
  if (dbPath !== ':memory:') {
    try {
      if (fs.existsSync(dbPath)) fs.chmodSync(dbPath, 0o600);
    } catch {}
  }

  db.exec(`
    PRAGMA busy_timeout = 2000;
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA temp_store = MEMORY;
    CREATE TABLE IF NOT EXISTS decision_cache (
      key TEXT PRIMARY KEY,
      data TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_decision_cache_expires ON decision_cache(expires_at);
  `);

  if (dbPath !== ':memory:') {
    try {
      if (fs.existsSync(dbPath)) fs.chmodSync(dbPath, 0o600);
      if (fs.existsSync(`${dbPath}-wal`)) fs.chmodSync(`${dbPath}-wal`, 0o600);
      if (fs.existsSync(`${dbPath}-shm`)) fs.chmodSync(`${dbPath}-shm`, 0o600);
    } catch {}
  }

  activeDb = db;
  activeDbPath = dbPath;
  return activeDb;
}

export function getDatabaseForTesting(): Database {
  return getDatabase();
}

/**
 * Deterministically compute a cache key from task/prompt, candidates, and model tier.
 */
export function computeDecisionCacheKey(
  taskOrPrompt: string,
  candidates?: unknown,
  modelTier?: string
): string {
  const normPrompt = (taskOrPrompt || '').trim();
  let candStr = '';
  if (candidates) {
    if (Array.isArray(candidates)) {
      const serialized = candidates.map((c) =>
        typeof c === 'object' && c !== null
          ? JSON.stringify(c, Object.keys(c).sort())
          : String(c)
      );
      candStr = serialized.sort().join('|');
    } else if (typeof candidates === 'object') {
      candStr = JSON.stringify(candidates, Object.keys(candidates as Record<string, unknown>).sort());
    } else {
      candStr = String(candidates);
    }
  }
  const tierStr = (modelTier || '').trim();
  const raw = `${normPrompt}:::${candStr}:::${tierStr}`;
  const hash = crypto.createHash('sha256').update(raw).digest('hex');
  return `decision:${hash}`;
}

/**
 * Get a cached decision by key/hash.
 * Queries SELECT data FROM decision_cache WHERE key = ? AND expires_at > ? with unix timestamp.
 */
export function getCachedDecision(key: string): any | null {
  const dbPath = resolveDbPath();

  // Backward compatibility: JSON file fallback if JEV_CACHE_FILE points to a .json file
  if (isJsonPath(dbPath)) {
    try {
      if (fs.existsSync(dbPath)) {
        const raw = fs.readFileSync(dbPath, 'utf8');
        const cache: Record<string, DecisionCacheEntry> = JSON.parse(raw);
        const hit = cache[key];
        const ttl = Number(process.env.JEV_CACHE_TTL_MS) || DECISION_CACHE_TTL_MS;
        if (hit && typeof hit.at === 'number' && Date.now() - hit.at < ttl) {
          return hit.data;
        }
      }
    } catch {}
    return null;
  }

  try {
    const db = getDatabase();
    const now = Math.floor(Date.now() / 1000);
    const row = db.query(
      'SELECT data FROM decision_cache WHERE key = ? AND expires_at > ?'
    ).get(key, now) as { data: string } | null;

    if (!row || !row.data) return null;
    try {
      return JSON.parse(row.data);
    } catch {
      return row.data;
    }
  } catch {
    return null;
  }
}

/**
 * Store a decision result in the cache.
 * Executes INSERT OR REPLACE INTO decision_cache (key, data, created_at, expires_at) VALUES (?, ?, ?, ?).
 * On a 5% sample, automatically prunes expired rows.
 */
export function setCachedDecision(key: string, data: any, ttlSec?: number): void {
  const dbPath = resolveDbPath();

  // Backward compatibility: JSON file fallback if JEV_CACHE_FILE points to a .json file
  if (isJsonPath(dbPath)) {
    try {
      const dir = path.dirname(dbPath);
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

      let diskCache: Record<string, DecisionCacheEntry> = {};
      if (fs.existsSync(dbPath)) {
        try {
          diskCache = JSON.parse(fs.readFileSync(dbPath, 'utf8'));
        } catch {
          diskCache = {};
        }
      }

      const now = Date.now();
      const ttl = Number(process.env.JEV_CACHE_TTL_MS) || DECISION_CACHE_TTL_MS;

      for (const [k, v] of Object.entries(diskCache)) {
        if (!v || typeof v.at !== 'number' || now - v.at > ttl * 2) {
          delete diskCache[k];
        }
      }

      diskCache[key] = { at: now, data };

      const tmpFile = path.join(
        dir,
        `.cache.${process.pid}.${now}.${Math.random().toString(36).slice(2)}.tmp`
      );
      fs.writeFileSync(tmpFile, JSON.stringify(diskCache, null, 2), 'utf8');
      fs.renameSync(tmpFile, dbPath);
    } catch {}
    return;
  }

  try {
    const db = getDatabase();
    const now = Math.floor(Date.now() / 1000);
    const cacheCfg = getCacheConfig();
    const ttl = ttlSec !== undefined
      ? ttlSec
      : (process.env.JEV_CACHE_TTL_MS
          ? Math.max(1, Math.round(Number(process.env.JEV_CACHE_TTL_MS) / 1000))
          : (cacheCfg.ttlSec || DECISION_CACHE_TTL_SEC));
    const expiresAt = now + ttl;
    const dataStr = typeof data === 'string' ? data : JSON.stringify(data);

    db.query(
      'INSERT OR REPLACE INTO decision_cache (key, data, created_at, expires_at) VALUES (?, ?, ?, ?)'
    ).run(key, dataStr, now, expiresAt);

    // 5% sampling to prune expired rows
    if (Math.random() < 0.05) {
      try {
        db.query('DELETE FROM decision_cache WHERE expires_at <= ?').run(now);
      } catch {}
    }
  } catch {
    // Cache write is best-effort
  }
}

/**
 * Clears all rows from the decision cache.
 */
export function clearDecisionCache(): void {
  const dbPath = resolveDbPath();
  if (isJsonPath(dbPath)) {
    try {
      if (fs.existsSync(dbPath)) {
        fs.unlinkSync(dbPath);
      }
    } catch {}
    return;
  }

  try {
    const db = getDatabase();
    db.query('DELETE FROM decision_cache;').run();
  } catch {}
}
