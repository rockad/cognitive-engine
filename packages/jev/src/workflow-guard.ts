/**
 * Static guard for the Claude Code `Workflow` tool.
 *
 * Agents spawned inside a workflow never pass through PreToolUse[Agent|Task], so this is the only
 * gate for them. It reads the workflow script (inline `script`, a `scriptPath` file, or a saved
 * workflow `name`, plus nested `workflow()` references one level deep), parses it with acorn and
 * visits EVERY reference to the identifiers `agent` and `workflow`.
 *
 * Fail closed: only a small set of shapes is understood, and everything else asks.
 * - `agent(prompt)` / `agent(prompt, {model: 'literal', agentType: 'literal', ...})`: understood.
 *   A literal model is ranked against the session model (models.ts); no model inherits (allowed);
 *   an agentType's model comes from its definition file, and a missing definition asks.
 * - `workflow('name')` / `workflow({scriptPath: 'literal'})`: resolved and analysed one level deep.
 * - Anything else asks: aliasing, passing the function as a value, member or computed access,
 *   spread arguments or options, computed keys, a model from a variable, a template with `${}`,
 *   dynamic import; parse errors, unreadable or oversized files.
 *
 * Principle: `agent` and `workflow` may appear only as the callee of a direct call. Every other way
 * to reach them, and every way to synthesize code, asks:
 * - destructuring a property named `agent`/`workflow` (or `constructor`, `getPrototypeOf`, ...), and
 *   reading them off anything but a plain local name (`x.agent`, `f().agent`, `x?.['agent']`);
 * - the escape set: `this`, `arguments`, `globalThis`, `global`, `window`, `self`, `Function`,
 *   `eval`, `Reflect`, `Proxy`, `process`, `require`, `Bun`, `Deno`, `module`, `exports`,
 *   `import.meta`, and the member names `constructor`, `__proto__`, `prototype`, `getPrototypeOf`,
 *   `setPrototypeOf`, `getOwnPropertyDescriptor(s)`, `caller`, `callee`, `prepareStackTrace`, ...;
 * - computed member access whose key is not a literal or provably a number (it could spell
 *   `constructor`); writes into a fresh local object or array literal are exempt;
 * - `setTimeout`/`setInterval` given anything but a function or a name; `__proto__` in a literal.
 *
 * The analysis is text only: comments and strings that mention "model" are never read as code.
 *
 * Known limits (static analysis cannot be sound; this is a fail-closed filter, not a sandbox):
 * - "Provably a number" trusts `.length`, the second parameter of map, forEach, filter, some,
 *   every, find, findIndex, findLast, findLastIndex and flatMap callbacks, and numeric-only names. A user-defined `map`, or an object whose
 *   `length` is a string, defeats it.
 * - `setTimeout(variable)` is allowed, because `setTimeout(resolve, ms)` is the common shape. Node
 *   and Bun throw on a string there; a runtime that evaluates it would not be covered.
 * - `x.agent` read off a plain unbound name (such as `args`) is allowed: it is data unless the
 *   sandbox exposes an object that holds the function. Unknown sandbox objects are not modelled.
 * - Options of `pipeline`/`parallel`/`phase` helpers are not inspected, only `agent()` and `meta`.
 * - The script is checked as written. It cannot see a file changing between the check and the run,
 *   or what a saved workflow resolves to if Claude Code's lookup differs from `fsResolver`.
 * - Intentional false positives (always ask): any `this`, any variable-key lookup such as `o[k]`
 *   on data, and a local method that happens to be called `agent` or `workflow`.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as acorn from 'acorn';
import { readAgentDefinition, type AgentDefinition } from './agent-definitions.js';
import { assessModelRequest } from './models.js';

/** Largest script (inline or file) the guard will parse. Bigger asks. */
export const MAX_SCRIPT_BYTES = 512 * 1024;

export interface CallSite {
  /** Where the script came from: `<inline>`, a file path, or a workflow name. */
  file: string;
  line: number;
  /** 1-based column. */
  col: number;
  reason: string;
}

export interface WorkflowDecision {
  decision: 'allow' | 'ask';
  sites: CallSite[];
  /** Ask text naming each offending call site; empty when allowed. */
  message: string;
}

/** Everything the analysis needs from the outside world, injectable so tests need no filesystem. */
export interface WorkflowResolver {
  readScriptFile(file: string): { text: string; label: string } | { error: string };
  findNamedWorkflow(name: string): { text: string; label: string } | { error: string };
  agentDefinition(agentType: string): AgentDefinition;
}

/** A resolver that finds nothing: every file, name and agent definition is "not found". */
export const NO_FS_RESOLVER: WorkflowResolver = {
  readScriptFile: (file) => ({ error: `cannot read ${file}` }),
  findNamedWorkflow: (name) => ({ error: `workflow "${name}" not found` }),
  agentDefinition: () => ({ found: false, model: null })
};

function readCapped(file: string): { text: string; label: string } | { error: string } {
  try {
    const { size } = fs.statSync(file);
    if (size > MAX_SCRIPT_BYTES) return { error: `${file} is ${size} bytes, over the ${MAX_SCRIPT_BYTES} byte limit` };
    return { text: fs.readFileSync(file, 'utf8'), label: file };
  } catch (err) {
    return { error: `cannot read ${file} (${err instanceof Error ? err.message : String(err)})` };
  }
}

/** Resolver backed by the filesystem: `<cwd>/.claude/...` first, then `~/.claude/...`. */
export function fsResolver(cwd: string | undefined): WorkflowResolver {
  const base = cwd || process.cwd();
  return {
    readScriptFile: (file) => readCapped(path.resolve(base, file)),
    findNamedWorkflow: (name) => {
      if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(name)) return { error: `workflow name "${name}" is not a plain name` };
      const home = process.env.HOME || os.homedir();
      for (const dir of [path.join(base, '.claude', 'workflows'), path.join(home, '.claude', 'workflows')]) {
        for (const ext of ['.js', '.mjs']) {
          const file = path.join(dir, `${name}${ext}`);
          if (fs.existsSync(file)) return readCapped(file);
        }
      }
      return { error: `workflow "${name}" not found in .claude/workflows (project or user)` };
    },
    agentDefinition: (agentType) => readAgentDefinition(agentType, base)
  };
}

// ── Parsing ────────────────────────────────────────────────────────────────────────────────────

type Finding =
  | { kind: 'model'; model: string; where: string; line: number; col: number }
  | { kind: 'agentType'; agentType: string; line: number; col: number }
  | { kind: 'nested'; ref: { name?: string; scriptPath?: string }; line: number; col: number }
  | { kind: 'unverifiable'; reason: string; line: number; col: number };

const WATCHED = new Set(['agent', 'workflow']);
/**
 * Identifiers that give a script a way to reach `agent` through the global object, to synthesize
 * code the guard cannot read, or to reflect its way to either. Any reference asks.
 */
const ESCAPES = new Set([
  'eval', 'Function', 'Reflect', 'Proxy',
  'globalThis', 'global', 'window', 'self', 'frames', 'document',
  'arguments', 'process', 'require', 'module', 'exports', 'Bun', 'Deno',
  'importScripts', 'Worker', 'SharedWorker'
]);
/**
 * Property names that reach the Function constructor, a prototype, a property descriptor or a stack
 * frame's `this`/function. Used as `x.name`, `x['name']` and as a destructured key.
 */
const MEMBER_ESCAPES = new Set([
  'constructor', '__proto__', 'prototype', 'getPrototypeOf', 'setPrototypeOf',
  'getOwnPropertyDescriptor', 'getOwnPropertyDescriptors',
  '__lookupGetter__', '__lookupSetter__', '__defineGetter__', '__defineSetter__',
  'caller', 'callee', 'prepareStackTrace', 'captureStackTrace', 'getThis', 'getFunction'
]);
/** Timers whose first argument can be a string of code in some runtimes. */
const TIMERS = new Set(['setTimeout', 'setInterval']);
/** Array methods whose callback receives (element, index): the second parameter is a number. */
const INDEX_METHODS = new Set(['map', 'forEach', 'filter', 'some', 'every', 'find', 'findIndex', 'findLast', 'findLastIndex', 'flatMap']);
const SKIP_KEYS = new Set(['type', 'start', 'end', 'loc', 'range']);

/** Facts about the names a script binds, computed once per script. See `collectBindings`. */
interface Bindings {
  /** Names whose every binding in the script is provably a number. */
  numeric: Set<string>;
  /** Names whose every binding is an object or array literal (a fresh local container). */
  plain: Set<string>;
  /** Every name the script binds anywhere. */
  bound: Set<string>;
}

type Node = any;

const isNode = (v: unknown): v is Node => !!v && typeof v === 'object' && typeof (v as any).type === 'string';
const pos = (n: Node) => ({ line: n.loc?.start?.line ?? 0, col: (n.loc?.start?.column ?? 0) + 1 });

/** String value of a string literal or an expression-free template literal; null otherwise. */
function literalString(n: Node): string | null {
  if (n?.type === 'Literal' && typeof n.value === 'string') return n.value;
  if (n?.type === 'TemplateLiteral' && n.expressions.length === 0 && n.quasis.length === 1) return n.quasis[0].value.cooked ?? null;
  return null;
}

/** The statically known name of a property key, or undefined when it is computed from an expression. */
function keyName(prop: Node): string | undefined {
  const k = prop.key;
  if (!prop.computed) return k.type === 'Identifier' ? k.name : typeof k.value === 'string' || typeof k.value === 'number' ? String(k.value) : undefined;
  return literalString(k) ?? undefined;
}

/**
 * Whether this Identifier node, seen under `parent` via `key`, is a name rather than a variable
 * reference. Everything else counts as a reference, so unknown AST shapes err toward asking.
 */
function isNonReference(parent: Node, key: string): boolean {
  switch (parent?.type) {
    case 'MemberExpression':
      return key === 'property' && !parent.computed;
    case 'Property':
    case 'PropertyDefinition':
    case 'MethodDefinition':
      return key === 'key' && !parent.computed;
    case 'ImportSpecifier':
      return key === 'imported';
    case 'ExportSpecifier':
      return key === 'exported';
    case 'LabeledStatement':
    case 'BreakStatement':
    case 'ContinueStatement':
      return key === 'label';
    case 'MetaProperty':
      return true;
    default:
      return false;
  }
}

function analyseOptions(opts: Node, out: Finding[]): void {
  let modelSeen = false;
  let agentTypeNode: { value: string; node: Node } | null = null;
  for (const prop of opts.properties) {
    if (prop.type === 'SpreadElement') {
      out.push({ kind: 'unverifiable', reason: 'options use a spread, so `model` cannot be read', ...pos(prop) });
      continue;
    }
    const name = keyName(prop);
    if (name === undefined) {
      out.push({ kind: 'unverifiable', reason: 'options use a computed key, which could be `model`', ...pos(prop) });
      continue;
    }
    if (name === 'model') {
      modelSeen = true;
      const value = prop.kind === 'init' && !prop.method ? literalString(prop.value) : null;
      if (value === null) {
        out.push({ kind: 'unverifiable', reason: 'model is not a plain string literal (variable, expression, template with ${} or args)', ...pos(prop) });
      } else {
        out.push({ kind: 'model', model: value, where: 'model', ...pos(prop) });
      }
    } else if (name === 'agentType') {
      const value = prop.kind === 'init' && !prop.method ? literalString(prop.value) : null;
      if (value === null) {
        out.push({ kind: 'unverifiable', reason: 'agentType is not a plain string literal, so its model cannot be read', ...pos(prop) });
      } else {
        agentTypeNode = { value, node: prop };
      }
    }
  }
  // An explicit model wins over the agent definition's, as for the Agent tool.
  if (agentTypeNode && !modelSeen) out.push({ kind: 'agentType', agentType: agentTypeNode.value, ...pos(agentTypeNode.node) });
}

function analyseAgentCall(call: Node, out: Finding[]): void {
  if (call.optional) {
    out.push({ kind: 'unverifiable', reason: 'optional call of agent()', ...pos(call) });
    return;
  }
  if (call.arguments.some((a: Node) => a.type === 'SpreadElement')) {
    out.push({ kind: 'unverifiable', reason: 'agent() called with spread arguments', ...pos(call) });
    return;
  }
  const [first, second] = call.arguments;
  if (first?.type === 'ObjectExpression') analyseOptions(first, out);
  if (second !== undefined) {
    if (second.type === 'ObjectExpression') analyseOptions(second, out);
    else out.push({ kind: 'unverifiable', reason: 'agent() options are not an object literal', ...pos(second) });
  }
}

function analyseWorkflowCall(call: Node, out: Finding[]): void {
  const here = pos(call);
  const first = call.arguments[0];
  if (call.optional || call.arguments.some((a: Node) => a.type === 'SpreadElement') || !first) {
    out.push({ kind: 'unverifiable', reason: 'workflow() called in a form the guard cannot read', ...here });
    return;
  }
  const name = literalString(first);
  if (name !== null) {
    out.push({ kind: 'nested', ref: { name }, ...here });
    return;
  }
  if (first.type === 'ObjectExpression') {
    const ref: { name?: string; scriptPath?: string } = {};
    for (const prop of first.properties) {
      const k = prop.type === 'SpreadElement' ? undefined : keyName(prop);
      if (k === 'scriptPath' || k === 'name') {
        const v = literalString(prop.value);
        if (v === null) {
          out.push({ kind: 'unverifiable', reason: `workflow() ${k} is not a plain string literal`, ...pos(prop) });
          return;
        }
        ref[k] = v;
      } else if (k === undefined) {
        out.push({ kind: 'unverifiable', reason: 'workflow() reference uses a spread or computed key', ...pos(prop) });
        return;
      }
    }
    if (ref.scriptPath !== undefined || ref.name !== undefined) {
      out.push({ kind: 'nested', ref, ...here });
      return;
    }
  }
  out.push({ kind: 'unverifiable', reason: 'workflow() target is not a literal name or {scriptPath: literal}', ...here });
}

/** `export const meta = {phases: [{model: ...}]}` is metadata, but a phase `model` is checked too. */
function analyseMeta(decl: Node, out: Finding[]): void {
  for (const d of decl.declarations ?? []) {
    if (d.id?.type !== 'Identifier' || d.id.name !== 'meta' || d.init?.type !== 'ObjectExpression') continue;
    for (const prop of d.init.properties) {
      if (prop.type === 'SpreadElement' || keyName(prop) !== 'phases' || prop.value?.type !== 'ArrayExpression') continue;
      for (const phase of prop.value.elements) {
        if (phase?.type !== 'ObjectExpression') continue;
        for (const p of phase.properties) {
          if (p.type === 'SpreadElement' || keyName(p) !== 'model') continue;
          const v = literalString(p.value);
          out.push(v === null
            ? { kind: 'unverifiable', reason: 'meta.phases model is not a plain string literal', ...pos(p) }
            : { kind: 'model', model: v, where: 'meta.phases model', ...pos(p) });
        }
      }
    }
  }
}

// ── Binding facts ──────────────────────────────────────────────────────────────────────────────
//
// Scope-free and conservative: a name counts as "numeric" or "plain" only if EVERY binding of that
// name anywhere in the script qualifies, so shadowing can only make the guard ask more, not less.

const NUMERIC_BINARY = new Set(['-', '*', '/', '%', '**', '&', '|', '^', '<<', '>>', '>>>']);
const NUMERIC_GLOBAL_FNS = new Set(['Number', 'parseInt', 'parseFloat']);
const MATH_FNS = new Set(['abs', 'ceil', 'floor', 'round', 'trunc', 'max', 'min', 'pow', 'sqrt', 'sign']);
const NUMERIC_ASSIGN_OPS = new Set(['-=', '*=', '/=', '%=', '**=', '&=', '|=', '^=', '<<=', '>>=', '>>>=']);

/** Whether an expression always evaluates to a number (or bigint), so it cannot spell a property name like "constructor". */
function isNumeric(e: Node, numeric: Set<string>, bound: Set<string>): boolean {
  switch (e?.type) {
    case 'Literal':
      return typeof e.value === 'number' || typeof e.value === 'bigint';
    case 'Identifier':
      return numeric.has(e.name);
    case 'UnaryExpression':
      return e.operator === '-' || e.operator === '+' || e.operator === '~';
    case 'UpdateExpression':
      return true;
    case 'BinaryExpression':
      return e.operator === '+' ? isNumeric(e.left, numeric, bound) && isNumeric(e.right, numeric, bound) : NUMERIC_BINARY.has(e.operator);
    case 'ConditionalExpression':
      return isNumeric(e.consequent, numeric, bound) && isNumeric(e.alternate, numeric, bound);
    case 'SequenceExpression':
      return isNumeric(e.expressions[e.expressions.length - 1], numeric, bound);
    case 'MemberExpression':
      return !e.computed && e.property.type === 'Identifier' && e.property.name === 'length';
    case 'CallExpression': {
      const c = e.callee;
      if (c.type === 'Identifier') return NUMERIC_GLOBAL_FNS.has(c.name) && !bound.has(c.name);
      return c.type === 'MemberExpression' && !c.computed && c.object.type === 'Identifier' && c.object.name === 'Math' && !bound.has('Math') &&
        c.property.type === 'Identifier' && MATH_FNS.has(c.property.name);
    }
    default:
      return false;
  }
}

function patternNames(p: Node, out: string[]): void {
  switch (p?.type) {
    case 'Identifier': out.push(p.name); break;
    case 'ObjectPattern': for (const q of p.properties) patternNames(q.type === 'RestElement' ? q.argument : q.value, out); break;
    case 'ArrayPattern': for (const q of p.elements) patternNames(q, out); break;
    case 'RestElement': patternNames(p.argument, out); break;
    case 'AssignmentPattern': patternNames(p.left, out); break;
    default:
  }
}

type Fact = { name: string; kind: 'other' | 'num' } | { name: string; kind: 'expr' | 'numIf'; expr: Node | null };

function collectBindings(ast: Node): Bindings {
  const facts: Fact[] = [];
  const indexCallbacks = new Set<Node>();
  const forDeclarators = new Set<Node>();
  const other = (p: Node) => {
    const names: string[] = [];
    patternNames(p, names);
    for (const name of names) facts.push({ name, kind: 'other' });
  };
  const walk = (n: Node): void => {
    switch (n.type) {
      case 'VariableDeclarator':
        if (forDeclarators.has(n) || n.id.type !== 'Identifier') other(n.id);
        else facts.push({ name: n.id.name, kind: 'expr', expr: n.init ?? null });
        break;
      case 'ForInStatement':
      case 'ForOfStatement':
        if (n.left.type === 'VariableDeclaration') for (const d of n.left.declarations) forDeclarators.add(d);
        else other(n.left);
        break;
      case 'FunctionDeclaration':
      case 'FunctionExpression':
      case 'ArrowFunctionExpression':
        if (n.id) other(n.id);
        n.params.forEach((p: Node, i: number) => {
          if (i === 1 && p.type === 'Identifier' && indexCallbacks.has(n)) facts.push({ name: p.name, kind: 'num' });
          else other(p);
        });
        break;
      case 'ClassDeclaration':
      case 'ClassExpression':
        if (n.id) other(n.id);
        break;
      case 'CatchClause':
        if (n.param) other(n.param);
        break;
      case 'ImportSpecifier':
      case 'ImportDefaultSpecifier':
      case 'ImportNamespaceSpecifier':
        other(n.local);
        break;
      case 'CallExpression': {
        const c = n.callee;
        const m = c.type === 'MemberExpression' && !c.computed && c.property.type === 'Identifier' ? c.property.name : null;
        const cb = n.arguments[0];
        if (m !== null && INDEX_METHODS.has(m) && (cb?.type === 'ArrowFunctionExpression' || cb?.type === 'FunctionExpression')) indexCallbacks.add(cb);
        break;
      }
      case 'AssignmentExpression':
        if (n.left.type === 'Identifier') {
          if (n.operator === '=') facts.push({ name: n.left.name, kind: 'expr', expr: n.right });
          else if (NUMERIC_ASSIGN_OPS.has(n.operator)) facts.push({ name: n.left.name, kind: 'num' });
          else if (n.operator === '+=') facts.push({ name: n.left.name, kind: 'numIf', expr: n.right });
          else facts.push({ name: n.left.name, kind: 'other' });
        } else if (n.left.type !== 'MemberExpression') {
          other(n.left);
        }
        break;
      default:
    }
    for (const k of Object.keys(n)) {
      if (SKIP_KEYS.has(k)) continue;
      const child = n[k];
      if (Array.isArray(child)) {
        for (const c of child) if (isNode(c)) walk(c);
      } else if (isNode(child)) {
        walk(child);
      }
    }
  };
  walk(ast);

  const byName = new Map<string, Fact[]>();
  for (const f of facts) {
    const list = byName.get(f.name);
    if (list) list.push(f);
    else byName.set(f.name, [f]);
  }
  const bound = new Set(byName.keys());
  // Least fixpoint: a name becomes numeric only when every one of its bindings is, given the names already proven.
  const numeric = new Set<string>();
  const isNumFact = (f: Fact): boolean =>
    f.kind === 'num' || ((f.kind === 'expr' || f.kind === 'numIf') && f.expr !== null && isNumeric(f.expr, numeric, bound));
  for (let changed = true; changed; ) {
    changed = false;
    for (const [name, list] of byName) {
      if (!numeric.has(name) && list.every(isNumFact)) {
        numeric.add(name);
        changed = true;
      }
    }
  }
  const plain = new Set<string>();
  for (const [name, list] of byName) {
    if (list.every((f) => f.kind === 'expr' && (f.expr?.type === 'ObjectExpression' || f.expr?.type === 'ArrayExpression'))) plain.add(name);
  }
  return { numeric, plain, bound };
}

// ── Visitor ────────────────────────────────────────────────────────────────────────────────────

/** Static property name of a member access: a string, null for a private name, undefined when computed from an expression. */
function memberPropName(m: Node): string | null | undefined {
  if (!m.computed) return m.property.type === 'Identifier' ? m.property.name : null;
  return literalString(m.property) ?? undefined;
}

function isFunctionNode(n: Node): boolean {
  return n?.type === 'ArrowFunctionExpression' || n?.type === 'FunctionExpression';
}

function visit(node: Node, parent: Node, key: string, out: Finding[], ctx: Bindings): void {
  const ask = (reason: string, at: Node = node) => out.push({ kind: 'unverifiable', reason, ...pos(at) });
  switch (node.type) {
    case 'Identifier': {
      if (isNonReference(parent, key)) return;
      const isDirectCall = parent?.type === 'CallExpression' && key === 'callee';
      if (WATCHED.has(node.name)) {
        if (!isDirectCall) ask(`\`${node.name}\` is aliased, rebound or used as a value, not called directly`);
      } else if (ESCAPES.has(node.name)) {
        ask(`\`${node.name}\` can reach agent() or run code the guard cannot read`);
      } else if (TIMERS.has(node.name) && !isDirectCall) {
        ask(`\`${node.name}\` is used as a value, and a timer can run a string as code`);
      }
      return;
    }
    case 'ThisExpression':
      ask('`this` can be the global object, which holds agent()');
      break;
    case 'MetaProperty':
      if (node.meta?.name === 'import') ask('`import.meta` cannot be inspected');
      break;
    case 'MemberExpression': {
      const name = memberPropName(node);
      const isCallee = parent?.type === 'CallExpression' && key === 'callee';
      if (name === undefined) {
        const isPlainWrite = parent?.type === 'AssignmentExpression' && key === 'left' && parent.operator === '=' && node.object.type === 'Identifier' && ctx.plain.has(node.object.name);
        if (!isPlainWrite && !isNumeric(node.property, ctx.numeric, ctx.bound)) {
          ask('computed property access with a key that is not a literal or a number could spell `constructor` or `agent`');
        }
      } else if (name !== null) {
        if (MEMBER_ESCAPES.has(name)) ask(`\`.${name}\` can reach the Function constructor, a prototype or the global object`);
        else if (WATCHED.has(name) && !isCallee && node.object.type !== 'Identifier') {
          ask(`\`${name}\` read through a member of something other than a plain local name`);
        }
      }
      break;
    }
    case 'Property': {
      if (parent?.type === 'ObjectPattern') {
        const name = keyName(node);
        if (name === undefined) {
          if (!isNumeric(node.key, ctx.numeric, ctx.bound)) ask('destructuring with a computed key that is not a literal or a number could pick `agent` or `constructor`');
        } else if (WATCHED.has(name) || MEMBER_ESCAPES.has(name)) {
          ask(`destructuring pulls \`${name}\` out of an object`);
        }
      } else if (parent?.type === 'ObjectExpression' && keyName(node) === '__proto__') {
        ask('`__proto__` in an object literal changes its prototype');
      }
      break;
    }
    case 'CallExpression': {
      const callee = node.callee;
      if (callee.type === 'Identifier' && callee.name === 'agent') analyseAgentCall(node, out);
      else if (callee.type === 'Identifier' && callee.name === 'workflow') analyseWorkflowCall(node, out);
      else if (callee.type === 'Identifier' && TIMERS.has(callee.name)) {
        const first = node.arguments[0];
        if (!(isFunctionNode(first) || first?.type === 'Identifier')) ask(`${callee.name}() is not given a function, so it may run a string as code`);
      } else if (callee.type === 'MemberExpression') {
        const prop = memberPropName(callee);
        if (typeof prop === 'string' && WATCHED.has(prop)) ask(`\`${prop}\` called through member or computed access`);
      }
      break;
    }
    case 'ImportExpression':
      ask('dynamic import() cannot be inspected');
      break;
    case 'ImportDeclaration':
      ask('import declarations cannot be inspected');
      break;
    case 'ExportAllDeclaration':
      ask('`export * from` imports a module the guard cannot inspect');
      break;
    case 'ExportNamedDeclaration':
      if (node.source) ask('`export { ... } from` imports a module the guard cannot inspect');
      if (node.declaration?.type === 'VariableDeclaration') analyseMeta(node.declaration, out);
      break;
    default:
  }
  for (const k of Object.keys(node)) {
    if (SKIP_KEYS.has(k)) continue;
    const child = node[k];
    if (Array.isArray(child)) {
      for (const c of child) if (isNode(c)) visit(c, node, k, out, ctx);
    } else if (isNode(child)) {
      visit(child, node, k, out, ctx);
    }
  }
}

/** Parse a script and collect findings. Never throws: a parse error is a finding. */
function parseFindings(script: string): Finding[] {
  const out: Finding[] = [];
  let ast: Node;
  try {
    ast = acorn.parse(script, {
      ecmaVersion: 'latest',
      sourceType: 'module',
      allowAwaitOutsideFunction: true,
      allowReturnOutsideFunction: true,
      allowHashBang: true,
      locations: true
    });
  } catch (err) {
    const loc = (err as any)?.loc;
    return [{ kind: 'unverifiable', reason: `script does not parse (${err instanceof Error ? err.message : String(err)})`, line: loc?.line ?? 1, col: (loc?.column ?? 0) + 1 }];
  }
  try {
    visit(ast, null, '', out, collectBindings(ast));
  } catch (err) {
    return [{ kind: 'unverifiable', reason: `script analysis failed (${err instanceof Error ? err.message : String(err)})`, line: 1, col: 1 }];
  }
  return out;
}

// ── Evaluation ─────────────────────────────────────────────────────────────────────────────────

function modelSite(file: string, f: { line: number; col: number }, requested: string, session: string | null, what: string): CallSite | null {
  const a = assessModelRequest(requested, session);
  if (a.kind === 'allowed') return null;
  return { file, line: f.line, col: f.col, reason: `${what} ${a.reason}` };
}

/**
 * Analyse one script text against the session model. Pure given a resolver, so tests can pass a
 * fake one (or none, which finds nothing). `depth` is 0 for the script the tool was called with and
 * 1 for a workflow it references; a workflow() reference inside a depth-1 script asks.
 */
export function checkScriptText(
  script: string,
  sessionModel: string | null,
  resolver: WorkflowResolver = NO_FS_RESOLVER,
  label = '<inline>',
  depth = 0
): CallSite[] {
  if (Buffer.byteLength(script, 'utf8') > MAX_SCRIPT_BYTES) {
    return [{ file: label, line: 1, col: 1, reason: `script is over the ${MAX_SCRIPT_BYTES} byte limit` }];
  }
  const sites: CallSite[] = [];
  for (const f of parseFindings(script)) {
    switch (f.kind) {
      case 'unverifiable':
        sites.push({ file: label, line: f.line, col: f.col, reason: f.reason });
        break;
      case 'model': {
        const s = modelSite(label, f, f.model, sessionModel, f.where === 'model' ? 'agent() call:' : 'meta.phases:');
        if (s) sites.push(s);
        break;
      }
      case 'agentType': {
        const def = resolver.agentDefinition(f.agentType);
        if (!def.found) {
          sites.push({ file: label, line: f.line, col: f.col, reason: `agentType "${f.agentType}" has no definition in .claude/agents, so its model cannot be read` });
        } else if (def.model !== null) {
          const s = modelSite(label, f, def.model, sessionModel, `agentType "${f.agentType}" definition:`);
          if (s) sites.push(s);
        }
        break;
      }
      case 'nested': {
        if (depth >= 1) {
          sites.push({ file: label, line: f.line, col: f.col, reason: 'workflow() nested more than one level deep cannot be followed' });
          break;
        }
        const target = f.ref.scriptPath !== undefined ? resolver.readScriptFile(f.ref.scriptPath) : resolver.findNamedWorkflow(f.ref.name as string);
        if ('error' in target) sites.push({ file: label, line: f.line, col: f.col, reason: `nested workflow: ${target.error}` });
        else sites.push(...checkScriptText(target.text, sessionModel, resolver, target.label, depth + 1));
        break;
      }
    }
  }
  return sites;
}

const MAX_LISTED = 10;

function describe(sites: CallSite[], sessionModel: string | null): string {
  const shown = sites.slice(0, MAX_LISTED).map((s) => `- ${s.file}:${s.line}:${s.col} ${s.reason}`);
  if (sites.length > MAX_LISTED) shown.push(`- and ${sites.length - MAX_LISTED} more`);
  return (
    `[jev] This workflow cannot be cleared against the session model ${sessionModel ? `"${sessionModel}"` : '(unknown)'}:\n` +
    `${shown.join('\n')}\n` +
    'Approve to run it as is, or deny and change those calls to inherit the session model.'
  );
}

/**
 * Decide a Workflow tool call from its `tool_input`. Understood forms: `{script}`,
 * `{scriptPath, args?, resumeFromRunId?}` and `{name}`. Every present source is analysed. An input
 * with none of them (or the wrong types) asks.
 */
export function evaluateWorkflowInput(
  toolInput: unknown,
  cwd: string | undefined,
  sessionModel: string | null,
  resolver: WorkflowResolver = fsResolver(cwd)
): WorkflowDecision {
  const ask = (sites: CallSite[]): WorkflowDecision => ({ decision: 'ask', sites, message: describe(sites, sessionModel) });
  const shape = (reason: string) => ask([{ file: '<tool_input>', line: 0, col: 0, reason }]);

  if (!toolInput || typeof toolInput !== 'object' || Array.isArray(toolInput)) return shape('Workflow tool_input is not an object');
  const input = toolInput as Record<string, unknown>;
  const present = (['script', 'scriptPath', 'name'] as const).filter((k) => input[k] !== undefined);
  if (present.length === 0) return shape('Workflow tool_input has none of script, scriptPath or name');
  for (const k of present) if (typeof input[k] !== 'string' || input[k] === '') return shape(`Workflow tool_input.${k} is not a non-empty string`);

  const sites: CallSite[] = [];
  try {
    if (input.script !== undefined) sites.push(...checkScriptText(input.script as string, sessionModel, resolver, '<inline>'));
    if (input.scriptPath !== undefined) {
      const r = resolver.readScriptFile(input.scriptPath as string);
      if ('error' in r) sites.push({ file: input.scriptPath as string, line: 0, col: 0, reason: r.error });
      else sites.push(...checkScriptText(r.text, sessionModel, resolver, r.label));
    }
    if (input.name !== undefined) {
      const r = resolver.findNamedWorkflow(input.name as string);
      if ('error' in r) sites.push({ file: input.name as string, line: 0, col: 0, reason: r.error });
      else sites.push(...checkScriptText(r.text, sessionModel, resolver, r.label));
    }
  } catch (err) {
    sites.push({ file: '<guard>', line: 0, col: 0, reason: `workflow analysis failed (${err instanceof Error ? err.message : String(err)})` });
  }
  return sites.length === 0 ? { decision: 'allow', sites: [], message: '' } : ask(sites);
}
