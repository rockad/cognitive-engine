/**
 * The `model:` of a subagent definition's YAML frontmatter, read strictly.
 *
 * No YAML library is a dependency of this package, so this is a small parser for exactly one
 * question: what does the top-level `model` key hold? It is deliberately NOT lenient. The answer is
 * one of three things:
 * - `none`: the file has no frontmatter block, or the block is fully understood and has no `model`
 *   key (or an empty one). The subagent inherits the session model.
 * - `model`: the block is fully understood and `model` is a plain, single-line string (bare, or
 *   in single or double quotes without escapes, optionally followed by a comment).
 * - `unreadable`: anything else. A block that is present but cannot be fully understood, a `model`
 *   whose value is not a plain string (sequence, mapping, flow, block scalar, anchor, alias, tag),
 *   a duplicate `model` key, a key that only differs from `model` by case or Unicode form, a merge
 *   key, a flow-style or otherwise non-mapping block, an unterminated block. The caller must ASK for
 *   these. Reading them as "no model" would let `"model": opus` or a BOM hide an escalation.
 *
 * Understood as a top-level block mapping: `key: value` lines; values that continue on indented
 * lines (plain multi-line, block scalars `|` `>`, nested collections); quoted and flow values that
 * span lines for keys other than `model`; blank and comment lines. A UTF-8 BOM before the opening
 * `---` is ignored. Line endings may be LF, CRLF or CR.
 */

export type FrontmatterModel = { kind: 'none' } | { kind: 'model'; value: string } | { kind: 'unreadable'; reason: string };

const unreadable = (reason: string): FrontmatterModel => ({ kind: 'unreadable', reason });

const DELIMITER = /^---[ \t]*$/;
const END_MARKER = /^(?:---|\.\.\.)[ \t]*$/;
const INVISIBLE = /[\u00AD\u200B-\u200F\u2060\uFEFF]/g;

/** Characters that start a YAML construct a plain key or value cannot start with. */
const INDICATOR_START = /^[\[\]{}&*!|>%@`,#'"]/;

/** The index of the line where a quoted scalar opened on `lines[start]` (at `rest`) closes, or null. */
function quotedEnd(lines: string[], start: number, rest: string): number | null {
  const quote = rest[0];
  let n = start;
  let text = rest;
  let i = 1;
  for (;;) {
    while (i < text.length) {
      const c = text[i];
      if (quote === '"' && c === '\\') {
        i += 2;
        continue;
      }
      if (c === quote) {
        if (quote === "'" && text[i + 1] === "'") {
          i += 2;
          continue;
        }
        return n;
      }
      i += 1;
    }
    n += 1;
    if (n >= lines.length) return null;
    text = lines[n];
    i = 0;
  }
}

/** The index of the line where a flow collection opened on `lines[start]` (at `rest`) balances, or null. */
function flowEnd(lines: string[], start: number, rest: string): number | null {
  let depth = 0;
  let quote: string | null = null;
  let n = start;
  let text = rest;
  for (;;) {
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quote) {
        if (quote === '"' && c === '\\') i += 1;
        else if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'") quote = c;
      else if (c === '#' && (i === 0 || /\s/.test(text[i - 1]))) break;
      else if (c === '[' || c === '{') depth += 1;
      else if (c === ']' || c === '}') {
        depth -= 1;
        if (depth === 0) return n;
      }
    }
    n += 1;
    if (n >= lines.length) return null;
    text = lines[n];
  }
}

const isBlankOrComment = (line: string) => /^[ \t]*(?:#.*)?$/.test(line);

/** A plain (unquoted) key or value that cannot be read as a plain scalar. */
function plainProblem(text: string): string | null {
  if (INDICATOR_START.test(text)) return `starts with the YAML indicator ${JSON.stringify(text[0])}`;
  if (/^[-?:](?:[ \t]|$)/.test(text)) return `starts with the YAML indicator ${JSON.stringify(text[0])}`;
  if (/^<</.test(text)) return 'is a YAML merge key';
  return null;
}

/** The `model` of the frontmatter in `raw` (the whole file text). */
export function parseFrontmatterModel(raw: string): FrontmatterModel {
  const text = raw.startsWith('\uFEFF') ? raw.slice(1) : raw;
  const lines = text.split(/\r\n|\n|\r/);

  if (!DELIMITER.test(lines[0] ?? '')) {
    // Not a block at the very start. A `---` after blank or indented lines may still be read as one
    // by a parser that tolerates leading whitespace: do not guess, ask.
    const first = lines.findIndex((l) => l.trim() !== '');
    if (first > 0 || (first === 0 && /^\s+---[ \t]*$/.test(lines[0]))) {
      if (lines[first].trim() === '---') return unreadable('the frontmatter delimiter is preceded by whitespace');
    }
    return { kind: 'none' };
  }

  const end = lines.findIndex((l, i) => i > 0 && END_MARKER.test(l));
  if (end < 0) return unreadable('the frontmatter block is not closed');
  const front = lines.slice(1, end);
  if (front.some((l) => /[\u0085\u2028\u2029]/.test(l))) return unreadable('the frontmatter contains a Unicode line separator');

  let seen = false;
  let value: string | null = null;

  for (let n = 0; n < front.length; n++) {
    const line = front[n];
    if (isBlankOrComment(line)) continue;
    if (/^[ \t]/.test(line)) return unreadable('an indented line has no key above it');

    let key: string;
    let keyIsQuoted = false;
    let rest: string;
    const quotedKey = /^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*')[ \t]*:(?:[ \t]+(.*)|[ \t]*)$/.exec(line);
    if (line[0] === '"' || line[0] === "'") {
      if (!quotedKey) return unreadable('a quoted key could not be read');
      key = quotedKey[1].slice(1, -1);
      if (key.includes('\\')) return unreadable('a quoted key uses an escape sequence');
      keyIsQuoted = true;
      rest = quotedKey[2] ?? '';
    } else {
      const colon = /:(?=[ \t]|$)/.exec(line);
      if (!colon) return unreadable(`line ${n + 2} of the file is not a "key: value" pair`);
      key = line.slice(0, colon.index).trimEnd();
      const problem = key === '' ? 'has an empty key' : plainProblem(key);
      if (problem) return unreadable(`line ${n + 2} of the file ${problem}`);
      rest = line.slice(colon.index + 1).trim();
    }
    rest = rest.trim();

    const folded = key.normalize('NFKC').replace(INVISIBLE, '').trim().toLowerCase();
    const isModel = key.trim() === 'model' || (keyIsQuoted && key === 'model');
    if (!isModel && folded === 'model') return unreadable(`a key "${key}" differs from "model" only by case or Unicode form`);

    // Consume this entry's value, which may span lines.
    let last = n;
    if (!isModel && (rest[0] === '"' || rest[0] === "'")) {
      const closed = quotedEnd(front, n, rest);
      if (closed === null) return unreadable(`the quoted value of "${key}" is not closed`);
      last = closed;
    } else if (!isModel && (rest[0] === '[' || rest[0] === '{')) {
      const closed = flowEnd(front, n, rest);
      if (closed === null) return unreadable(`the flow value of "${key}" is not closed`);
      last = closed;
    }
    let continuation = false;
    while (last + 1 < front.length && (isBlankOrComment(front[last + 1]) || /^[ \t]/.test(front[last + 1]))) {
      last += 1;
      if (!isBlankOrComment(front[last])) continuation = true;
    }
    const entryEnd = last;

    if (isModel) {
      if (seen) return unreadable('the frontmatter has more than one "model" key');
      seen = true;
      if (continuation) return unreadable('"model" has a nested or multi-line value, not a plain string');
      if (rest === '' || rest[0] === '#') {
        value = null;
      } else if (rest[0] === '"') {
        const m = /^"([^"\\]*)"[ \t]*(?:#.*)?$/.exec(rest);
        if (!m) return unreadable('"model" is a double-quoted string that is unterminated or uses an escape sequence');
        value = m[1];
      } else if (rest[0] === "'") {
        const m = /^'([^']*)'[ \t]*(?:#.*)?$/.exec(rest);
        if (!m) return unreadable('"model" is a single-quoted string that is unterminated or uses an escaped quote');
        value = m[1];
      } else {
        const problem = plainProblem(rest);
        if (problem) return unreadable(`the value of "model" ${problem}, so it is not a plain string`);
        const plain = rest.replace(/[ \t]+#.*$/, '').trim();
        if (/:(?:[ \t]|$)/.test(plain)) return unreadable('the value of "model" contains ": ", which YAML reads as a mapping');
        value = plain;
      }
    }
    n = entryEnd;
  }

  return seen && value !== null ? { kind: 'model', value } : { kind: 'none' };
}

/**
 * The model value an agent definition reports when its frontmatter is present but its `model` cannot
 * be established. It is deliberately not a model Jev can rank and not `inherit`, so every caller asks
 * (an unrankable model asks) and the ask names the problem. It must never read as "no model".
 */
export function unreadableModelValue(reason: string): string {
  return `<unreadable agent frontmatter: ${reason}>`;
}
