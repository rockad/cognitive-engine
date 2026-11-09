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
 * lines (plain multi-line, block scalars `|` `>`, nested collections); single-line quoted and flow
 * values; blank and comment lines. A quoted or flow value that spans several lines is `unreadable`
 * (Claude Code's fallback parser re-quotes single lines and can then read a "continuation" line as a
 * key). A UTF-8 BOM before the opening `---` is ignored. Line endings may be LF, CRLF or CR.
 *
 * The strict answer is then checked against Claude Code's own pipeline (`parseFrontmatterModel`).
 *
 * The top-level `name` and `effort` are read by the same rules. `effort` is the reasoning effort the
 * agent runs at: a plain string is returned as written (the guard ranks it, see `effortReason` in
 * agent-definitions.ts), and anything that is not a plain string, including an integer, is `unreadable`.
 */

export type FrontmatterModel = { kind: 'none' } | { kind: 'model'; value: string } | { kind: 'unreadable'; reason: string };

/** The top-level `name` of a definition: Claude Code registers the agent under it, not under the file name. */
export type FrontmatterName = { kind: 'none' } | { kind: 'name'; value: string } | { kind: 'unreadable'; reason: string };

/** The top-level `effort` of a definition: the reasoning effort Claude Code runs the agent at (a level name, or an integer it reads as a number). */
export type FrontmatterEffort = { kind: 'none' } | { kind: 'effort'; value: string } | { kind: 'unreadable'; reason: string };

/** What the strict reader found: the whole block unreadable, or one answer per key it is asked about. */
type StrictScan = { kind: 'unreadable'; reason: string } | { kind: 'fields'; model: FrontmatterModel; name: FrontmatterName; effort: FrontmatterEffort };

const unreadable = (reason: string): { kind: 'unreadable'; reason: string } => ({ kind: 'unreadable', reason });

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

const NO_FIELDS: StrictScan = { kind: 'fields', model: { kind: 'none' }, name: { kind: 'none' }, effort: { kind: 'none' } };

/** The `model` and `name` of the frontmatter in `raw` (the whole file text), read strictly by this module's own parser. */
function strictFrontmatter(raw: string): StrictScan {
  const text = raw.startsWith('\uFEFF') ? raw.slice(1) : raw;
  const lines = text.split(/\r\n|\n|\r/);

  if (!DELIMITER.test(lines[0] ?? '')) {
    // Not a block at the very start. A `---` after blank or indented lines may still be read as one
    // by a parser that tolerates leading whitespace: do not guess, ask.
    const first = lines.findIndex((l) => l.trim() !== '');
    if (first > 0 || (first === 0 && /^\s+---[ \t]*$/.test(lines[0]))) {
      if (lines[first].trim() === '---') return unreadable('the frontmatter delimiter is preceded by whitespace');
    }
    return NO_FIELDS;
  }

  const end =lines.findIndex((l, i) => i > 0 && END_MARKER.test(l));
  if (end < 0) return unreadable('the frontmatter block is not closed');
  const front = lines.slice(1, end);
  if (front.some((l) => /[\u0085\u2028\u2029]/.test(l))) return unreadable('the frontmatter contains a Unicode line separator');

  let seen = false;
  let value: string | null = null;
  let nameSeen = false;
  let nameValue: string | null = null;
  let nameProblem: string | null = null;
  let effortSeen = false;
  let effortValue: string | null = null;
  let effortProblem: string | null = null;

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
    const isName = key.trim() === 'name' || (keyIsQuoted && key === 'name');
    const isEffort = key.trim() === 'effort' || (keyIsQuoted && key === 'effort');
    if (!isModel && folded === 'model') return unreadable(`a key "${key}" differs from "model" only by case or Unicode form`);
    // A look-alike of `effort` taints the effort answer only: the model and name stay readable.
    if (!isEffort && folded === 'effort') effortProblem ??= `a key "${key}" differs from "effort" only by case or Unicode form`;

    // Consume this entry's value. A quoted or flow value must close on its own line: Claude Code's
    // fallback re-quotes single lines of a block it could not parse, which reads a "continuation"
    // line as a top-level key (`model: opus` inside a quoted description becomes a real key).
    let last = n;
    if (!isModel && !isName && !isEffort && (rest[0] === '"' || rest[0] === "'")) {
      const closed = quotedEnd(front, n, rest);
      if (closed === null) return unreadable(`the quoted value of "${key}" is not closed`);
      if (closed !== n) return unreadable(`the quoted value of "${key}" spans several lines, which Claude Code's fallback parser can read differently`);
      last = closed;
    } else if (!isModel && !isName && !isEffort && (rest[0] === '[' || rest[0] === '{')) {
      const closed = flowEnd(front, n, rest);
      if (closed === null) return unreadable(`the flow value of "${key}" is not closed`);
      if (closed !== n) return unreadable(`the flow value of "${key}" spans several lines, which Claude Code's fallback parser can read differently`);
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
    if (isName) {
      if (nameSeen) nameProblem ??= 'the frontmatter has more than one "name" key';
      nameSeen = true;
      if (continuation) nameProblem ??= '"name" has a nested or multi-line value, not a plain string';
      else if (rest === '' || rest[0] === '#') nameValue = null;
      else if (rest[0] === '"') {
        const m = /^"([^"\\]*)"[ \t]*(?:#.*)?$/.exec(rest);
        if (m) nameValue = m[1];
        else nameProblem ??= '"name" is a double-quoted string that is unterminated or uses an escape sequence';
      } else if (rest[0] === "'") {
        const m = /^'([^']*)'[ \t]*(?:#.*)?$/.exec(rest);
        if (m) nameValue = m[1];
        else nameProblem ??= '"name" is a single-quoted string that is unterminated or uses an escaped quote';
      } else {
        const problem = plainProblem(rest);
        const plain = rest.replace(/[ \t]+#.*$/, '').trim();
        if (problem) nameProblem ??= `the value of "name" ${problem}, so it is not a plain string`;
        else if (/:(?:[ \t]|$)/.test(plain)) nameProblem ??= 'the value of "name" contains ": ", which YAML reads as a mapping';
        else nameValue = plain;
      }
    }
    if (isEffort) {
      if (effortSeen) effortProblem ??= 'the frontmatter has more than one "effort" key';
      effortSeen = true;
      if (continuation) effortProblem ??= '"effort" has a nested or multi-line value, not a plain string';
      else if (rest === '' || rest[0] === '#') effortValue = null;
      else if (rest[0] === '"') {
        const m = /^"([^"\\]*)"[ \t]*(?:#.*)?$/.exec(rest);
        if (m) effortValue = m[1];
        else effortProblem ??= '"effort" is a double-quoted string that is unterminated or uses an escape sequence';
      } else if (rest[0] === "'") {
        const m = /^'([^']*)'[ \t]*(?:#.*)?$/.exec(rest);
        if (m) effortValue = m[1];
        else effortProblem ??= '"effort" is a single-quoted string that is unterminated or uses an escaped quote';
      } else {
        const problem = plainProblem(rest);
        const plain = rest.replace(/[ \t]+#.*$/, '').trim();
        if (problem) effortProblem ??= `the value of "effort" ${problem}, so it is not a plain string`;
        else if (/:(?:[ \t]|$)/.test(plain)) effortProblem ??= 'the value of "effort" contains ": ", which YAML reads as a mapping';
        else effortValue = plain;
      }
    }
    n = entryEnd;
  }

  return {
    kind: 'fields',
    effort:
      effortProblem !== null ? unreadable(effortProblem) : effortSeen && effortValue !== null && effortValue !== '' ? { kind: 'effort', value: effortValue } : { kind: 'none' },
    model: seen && value !== null ? { kind: 'model', value } : { kind: 'none' },
    name: nameProblem !== null ? unreadable(nameProblem) : nameSeen && nameValue !== null && nameValue !== '' ? { kind: 'name', value: nameValue } : { kind: 'none' }
  };
}

/** A YAML parser (`Bun.YAML.parse` under bun, where the hooks and the MCP server run). */
export type YamlParse = (text: string) => unknown;

function bunYaml(): YamlParse | null {
  const parse = (globalThis as { Bun?: { YAML?: { parse?: unknown } } }).Bun?.YAML?.parse;
  return typeof parse === 'function' ? (parse as YamlParse) : null;
}

/** Claude Code's frontmatter block regex (2.1.312). */
const CC_BLOCK = /^---\s*\n([\s\S]*?)---\s*\n?/;
/** What Claude Code's fallback quotes: a special character, or ": " inside a plain value. */
const CC_SPECIAL = /[{}[\]*&#!|>%@`]|: /;
/** A top-level `key: value` line, as Claude Code's fallback sees it. */
const CC_PAIR = /^([a-zA-Z_-]+):\s+(\S.*)$/;

/** The text of the frontmatter block as Claude Code finds it, or null when it finds none. */
function ccBlock(raw: string): string | null {
  const text = raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw;
  const m = text.indexOf('---', 3) < 0 ? null : CC_BLOCK.exec(text);
  return m ? (m[1] ?? '') : null;
}

/** Whether any line of `block` is one Claude Code's fallback might re-quote (without a YAML parser: assume it can). */
function fallbackMightApply(block: string): boolean {
  return block.split('\n').some((line) => {
    const m = CC_PAIR.exec(line);
    if (!m) return false;
    const w = m[2];
    if ((w.startsWith('"') && w.endsWith('"')) || (w.startsWith("'") && w.endsWith("'"))) return false;
    return CC_SPECIAL.test(w);
  });
}

/** Claude Code's `dm`: quote the value of every top-level `key: value` line that holds a YAML special character. */
function ccQuoteFallback(block: string, yaml: YamlParse): string {
  return block
    .split('\n')
    .map((line) => {
      const m = CC_PAIR.exec(line);
      if (!m) return line;
      const [, key, w] = m;
      if ((w.startsWith('"') && w.endsWith('"')) || (w.startsWith("'") && w.endsWith("'"))) return line;
      if (w.startsWith('[') && w.endsWith(']')) {
        try {
          if (Array.isArray(yaml(w))) return line;
        } catch {
          // not a sequence: fall through to quoting
        }
      }
      if (CC_SPECIAL.test(w)) return `${key}: "${w.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"`;
      return line;
    })
    .join('\n');
}

/** The frontmatter Claude Code reads for `raw`: its mapping, `{}` when there is none, or null when both parses throw. */
function ccFrontmatter(raw: string, yaml: YamlParse): Record<string, unknown> | null {
  const block = ccBlock(raw);
  if (block === null) return {};
  let value: unknown;
  try {
    value = yaml(block);
  } catch {
    try {
      value = yaml(ccQuoteFallback(block, yaml).replace(/^\t+/gm, (tabs) => '  '.repeat(tabs.length)));
    } catch {
      return null;
    }
  }
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** The string Claude Code would read for the top-level `key` of `raw`, trimmed ('' for none), or an `unreadable` reason. */
function ccString(raw: string, yaml: YamlParse, key: 'model' | 'name' | 'effort'): { ok: true; value: string } | { ok: false; reason: string } {
  const front = ccFrontmatter(raw, yaml);
  if (front === null) return { ok: false, reason: "Claude Code's YAML parser rejects this frontmatter, so what it would run is not known" };
  const v = front[key];
  if (v === undefined || v === null) return { ok: true, value: '' };
  if (typeof v !== 'string') return { ok: false, reason: `Claude Code reads "${key}" as ${typeof v === 'object' ? 'a collection' : `the ${typeof v} ${String(v)}`}, not a string` };
  return { ok: true, value: v.trim() };
}

/**
 * Check a strict answer for `key` against Claude Code's pipeline: the strict answer when both agree
 * (`mine` is its value, '' for none), else the reason they differ. Without a YAML parser (`yaml`
 * null, not under bun) only a conservative rule applies: a block with a line the fallback could
 * re-quote is not trusted.
 */
function crossCheck(raw: string, yaml: YamlParse | null, key: 'model' | 'name' | 'effort', mine: string): string | null {
  if (yaml === null) {
    const block = ccBlock(raw);
    return block !== null && fallbackMightApply(block)
      ? "a line could be re-quoted by Claude Code's fallback frontmatter parser, and no YAML parser is available here to check it"
      : null;
  }
  const cc = ccString(raw, yaml, key);
  if (!cc.ok) return cc.reason;
  if (cc.value === mine) return null;
  return `Jev reads ${key} ${mine === '' ? 'as none' : JSON.stringify(mine)} but Claude Code's parser reads ${cc.value === '' ? 'none' : JSON.stringify(cc.value)}`;
}

/**
 * The `model`, `name` and `effort` of the frontmatter in `raw` (the whole file text). Jev's strict reader and
 * a faithful copy of Claude Code's pipeline (its block regex, `Bun.YAML.parse`, then its quoting
 * fallback when that throws) both answer; if either cannot, or they differ on a key, that key is
 * `unreadable` so the caller asks. Without a YAML parser only the strict reader runs, plus a
 * conservative rule (see `crossCheck`).
 */
export function parseFrontmatterFields(raw: string, yaml: YamlParse | null = bunYaml()): { model: FrontmatterModel; name: FrontmatterName; effort: FrontmatterEffort } {
  const strict = strictFrontmatter(raw);
  if (strict.kind === 'unreadable') return { model: strict, name: strict, effort: strict };
  let { model, name, effort } = strict;
  if (model.kind !== 'unreadable') {
    const problem = crossCheck(raw, yaml, 'model', model.kind === 'model' ? model.value.trim() : '');
    if (problem !== null) model = unreadable(problem);
  }
  if (name.kind !== 'unreadable') {
    const problem = crossCheck(raw, yaml, 'name', name.kind === 'name' ? name.value.trim() : '');
    if (problem !== null) name = unreadable(problem);
  }
  if (effort.kind !== 'unreadable') {
    const problem = crossCheck(raw, yaml, 'effort', effort.kind === 'effort' ? effort.value.trim() : '');
    if (problem !== null) effort = unreadable(problem);
  }
  return { model, name, effort };
}

/**
 * The `name` as Claude Code's pipeline alone reads it (no strict reader, no cross-check): the
 * second opinion for a file whose strict `name` is unreadable. `unreadable` when Claude Code's
 * parser rejects the block, reads a non-string, or no YAML parser is available.
 */
export function readClaudeCodeName(raw: string, yaml: YamlParse | null = bunYaml()): FrontmatterName {
  if (yaml === null) return unreadable('no YAML parser is available here');
  const cc = ccString(raw, yaml, 'name');
  if (!cc.ok) return unreadable(cc.reason);
  return cc.value === '' ? { kind: 'none' } : { kind: 'name', value: cc.value };
}

/** The `model` of the frontmatter in `raw`; see `parseFrontmatterFields`. */
export function parseFrontmatterModel(raw: string, yaml: YamlParse | null = bunYaml()): FrontmatterModel {
  return parseFrontmatterFields(raw, yaml).model;
}

/**
 * The model value an agent definition reports when its frontmatter is present but its `model` cannot
 * be established. It is deliberately not a model Jev can rank and not `inherit`, so every caller asks
 * (an unrankable model asks) and the ask names the problem. It must never read as "no model".
 */
export function unreadableModelValue(reason: string): string {
  return `<unreadable agent frontmatter: ${reason}>`;
}

/** The effort value an agent definition reports when its frontmatter is present but its `effort` cannot be established; it is no effort level, so `effortReason` asks. */
export function unreadableEffortValue(reason: string): string {
  return `<unreadable agent frontmatter: ${reason}>`;
}
