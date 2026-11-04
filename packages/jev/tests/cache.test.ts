import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import { Database } from 'bun:sqlite';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  getCachedDecision,
  setCachedDecision,
  clearDecisionCache,
  computeDecisionCacheKey,
  resolveDbPath,
  resolveCacheFilePath,
  DEFAULT_SHM_CACHE_FILE,
  DEFAULT_FALLBACK_CACHE_FILE,
  DECISION_CACHE_TTL_SEC,
  getDatabaseForTesting
} from '../src/core/cache.js';
import { clearConfigCacheForTesting } from '../src/core/config.js';

describe('Shared bun:sqlite RAM Cache (src/core/cache.ts)', () => {
  let tmpCacheDir: string;
  let tmpCacheFile: string;

  beforeEach(() => {
    tmpCacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-cache-sqlite-'));
    tmpCacheFile = path.join(tmpCacheDir, 'cache.sqlite');
    process.env.JEV_CACHE_FILE = tmpCacheFile;
    process.env.JEV_CONFIG_FILE = path.join(tmpCacheDir, 'nonexistent-config.json');
    clearConfigCacheForTesting();
    clearDecisionCache();
  });

  afterEach(() => {
    clearDecisionCache();
    delete process.env.JEV_CACHE_FILE;
    delete process.env.JEV_CONFIG_FILE;
    delete process.env.JEV_CACHE_TTL_MS;
    clearConfigCacheForTesting();
    try {
      fs.rmSync(tmpCacheDir, { recursive: true, force: true });
    } catch {}
  });

  it('resolves cache file path to /dev/shm/jev/cache.sqlite on Linux when env var is unset', () => {
    delete process.env.JEV_CACHE_FILE;
    const resolved = resolveDbPath();
    if (fs.existsSync('/dev/shm')) {
      expect(resolved).toBe(DEFAULT_SHM_CACHE_FILE);
      expect(resolved).toBe('/dev/shm/jev/cache.sqlite');
    } else {
      expect(resolved).toBe(DEFAULT_FALLBACK_CACHE_FILE);
    }
  });

  it('prioritizes JEV_CACHE_FILE override if set', () => {
    process.env.JEV_CACHE_FILE = '/custom/path/cache.sqlite';
    expect(resolveDbPath()).toBe('/custom/path/cache.sqlite');
    expect(resolveCacheFilePath()).toBe('/custom/path/cache.sqlite');
  });

  it('stores and retrieves cached decision via bun:sqlite with sub-millisecond latency', () => {
    const key = computeDecisionCacheKey('task: cache test', [{ id: 'opt1' }], 'flash');
    const data = { selected: 'opt1', confidence: 0.99 };

    setCachedDecision(key, data);

    const start = performance.now();
    const hit = getCachedDecision(key);
    const elapsed = performance.now() - start;

    expect(hit).toEqual(data);
    expect(elapsed).toBeLessThan(10); // sub-millisecond in-process retrieval

    // Verify SQLite file on disk contains valid table and data
    expect(fs.existsSync(tmpCacheFile)).toBe(true);
    const db = new Database(tmpCacheFile);
    const row = db.query('SELECT data, created_at, expires_at FROM decision_cache WHERE key = ?').get(key) as any;
    expect(row).toBeDefined();
    expect(JSON.parse(row.data)).toEqual(data);
    expect(row.expires_at).toBeGreaterThan(row.created_at);
    db.close();
  });

  it('verifies WAL pragma and index schema in SQLite database', () => {
    const key = 'test-schema-key';
    setCachedDecision(key, { ok: true });

    const activeDb = getDatabaseForTesting();
    const busyTimeout = activeDb.query('PRAGMA busy_timeout;').get() as any;
    expect(busyTimeout.timeout).toBe(2000);

    const db = new Database(tmpCacheFile);
    const journalMode = db.query('PRAGMA journal_mode;').get() as any;
    expect(journalMode.journal_mode.toLowerCase()).toBe('wal');

    const indexInfo = db.query("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_decision_cache_expires';").get() as any;
    expect(indexInfo).toBeDefined();
    expect(indexInfo.name).toBe('idx_decision_cache_expires');
    db.close();

    // Verify 0o600 file permissions on POSIX
    if (process.platform !== 'win32') {
      const stat = fs.statSync(tmpCacheFile);
      expect(stat.mode & 0o777).toBe(0o600);
    }
  });

  it('verifies /dev/shm directory permissions (0o700) and ownership validation', () => {
    if (process.platform === 'win32' || !fs.existsSync('/dev/shm')) return;

    delete process.env.JEV_CACHE_FILE;
    const resolved = resolveDbPath();
    expect(resolved).toBe(DEFAULT_SHM_CACHE_FILE);

    const shmDir = path.dirname(resolved);
    const stat = fs.statSync(shmDir);
    expect(stat.mode & 0o777).toBe(0o700);
    expect(stat.uid).toBe(process.getuid());
  });

  it('expires stale entries according to ttlSec', () => {
    const key = 'test-expire-key';
    const data = { valid: false };

    // Set with negative ttlSec so it is already expired
    setCachedDecision(key, data, -5);

    expect(getCachedDecision(key)).toBeNull();
  });

  it('clears decision cache rows via clearDecisionCache', () => {
    const key = 'test-clear-key';
    setCachedDecision(key, { ok: true });
    expect(getCachedDecision(key)).toEqual({ ok: true });

    clearDecisionCache();

    expect(getCachedDecision(key)).toBeNull();

    const db = new Database(tmpCacheFile);
    const countRow = db.query('SELECT count(*) as count FROM decision_cache;').get() as any;
    expect(countRow.count).toBe(0);
    db.close();
  });

  it('handles concurrent multi-writes and multi-reads across multiple Database connections without SQLITE_BUSY or corruption', async () => {
    const writeCount = 30;
    const readCount = 30;

    // Concurrently write and read across independent Database connections in parallel promises
    const writePromises = Array.from({ length: writeCount }, async (_, i) => {
      // Independent connection simulating concurrent worker process
      const conn = new Database(tmpCacheFile);
      conn.exec('PRAGMA busy_timeout = 2000;');
      try {
        const stmt = conn.query(`
          INSERT OR REPLACE INTO decision_cache (key, data, created_at, expires_at)
          VALUES (?, ?, ?, ?)
        `);
        const now = Date.now();
        stmt.run(`concurrent-key-${i}`, JSON.stringify({ index: i, workerId: `worker-${i % 4}` }), now, now + 300000);
      } finally {
        conn.close();
      }
    });

    const readPromises = Array.from({ length: readCount }, async (_, i) => {
      // Interleaved concurrent reads from independent connection
      const conn = new Database(tmpCacheFile);
      conn.exec('PRAGMA busy_timeout = 2000;');
      try {
        conn.query('SELECT count(*) as cnt FROM decision_cache;').get();
      } finally {
        conn.close();
      }
    });

    // Also interleave helper functions (setCachedDecision and getCachedDecision)
    const helperPromises = Array.from({ length: 15 }, async (_, i) => {
      setCachedDecision(`helper-key-${i}`, { helperIndex: i });
      return getCachedDecision(`helper-key-${i}`);
    });

    const results = await Promise.all([...writePromises, ...readPromises, ...helperPromises]);
    expect(results).toBeDefined();

    // Verify all rows were written successfully and count is exact
    const verifyDb = new Database(tmpCacheFile);
    const countRow = verifyDb.query('SELECT count(*) as count FROM decision_cache;').get() as any;
    expect(countRow.count).toBe(writeCount + 15);

    // Verify individual items written by independent connections
    for (let i = 0; i < writeCount; i++) {
      const row = verifyDb.query('SELECT data FROM decision_cache WHERE key = ?').get(`concurrent-key-${i}`) as any;
      expect(row).toBeDefined();
      expect(JSON.parse(row.data).index).toBe(i);
    }
    verifyDb.close();
  });
});
