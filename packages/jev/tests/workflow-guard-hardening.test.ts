/**
 * Regression and battery tests for the workflow guard's class-level hardening.
 *
 * Principle under test: `agent` and `workflow` may only appear as the callee of a direct call, and
 * nothing that can reach them another way (the global object, the Function constructor, reflection,
 * string-built keys) or synthesize code may pass silently. `silent` rows pin the benign shapes.
 *
 * Static analysis is not sound. The `STILL_SLIPS` table at the bottom records, as executable
 * documentation, idioms known to pass; if one starts asking, the table is out of date.
 */
import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { checkScriptText, NO_FS_RESOLVER } from '../src/workflow-guard.ts';

const SESSION = 'claude-sonnet-5-5';
const check = (script: string) => checkScriptText(script, SESSION, NO_FS_RESOLVER);
const FIX = path.join(import.meta.dir, 'fixtures', 'payloads');

type Row = [name: string, script: string, expected: 'ask' | 'silent'];

function table(title: string, rows: Row[]): void {
  describe(title, () => {
    for (const [name, script, expected] of rows) {
      it(`${expected}: ${name}`, () => {
        const sites = check(script);
        if (expected === 'ask') {
          expect(sites.length).toBeGreaterThan(0);
          expect(sites[0].line).toBeGreaterThan(0);
          expect(sites[0].reason.length).toBeGreaterThan(5);
        } else {
          expect(sites).toEqual([]);
        }
      });
    }
  });
}

describe('reviewer findings', () => {
  it('HIGH 1: a destructuring alias of agent from `this` asks', () => {
    expect(check(`const { agent: myAgent } = this; myAgent("p", {model: "opus"})`).length).toBeGreaterThan(0);
  });
  it('HIGH 2: a property alias on `global` asks', () => {
    expect(check(`const a = global.agent; a("p", {model: "opus"})`).length).toBeGreaterThan(0);
  });
  it('HIGH 3: the Function constructor through the prototype chain asks', () => {
    expect(check(`(function(){}).constructor("agent('p', {model: 'opus'})")()`).length).toBeGreaterThan(0);
  });
  it('MEDIUM 4: a local method named like the watched name asks (accepted false positive, fail closed)', () => {
    const sites = check(`const client = { agent() { return 1 } }; client.agent()`);
    expect(sites.length).toBeGreaterThan(0);
    expect(sites[0].reason).toContain('agent');
  });
  it('LOW 6: window and self are escapes like globalThis', () => {
    for (const g of ['global', 'window', 'self', 'globalThis']) expect(check(`${g}.agent('p')`).length).toBeGreaterThan(0);
  });
});

table('adversarial battery: watched names reached other than by a direct call', [
  ['array holding agent', `const xs = [agent]; await xs[0]('p')`, 'ask'],
  ['[agent][0] called inline', `await [agent][0]('p', {model: 'opus'})`, 'ask'],
  ['Map holding agent', `const m = new Map([['a', agent]]); await m.get('a')('p')`, 'ask'],
  ['closure returning agent', `const mk = () => agent; await mk()('p')`, 'ask'],
  ['getter returning agent', `const o = { get a() { return agent } }; await o.a('p')`, 'ask'],
  ['setter fed agent', `const o = { set a(v) { v('p') } }; o.a = agent`, 'ask'],
  ['agent.call', `await agent.call(null, 'p', {model: 'opus'})`, 'ask'],
  ['agent.apply', `await agent.apply(null, ['p'])`, 'ask'],
  ['agent.bind(null)(...)', `await agent.bind(null)('p', {model: 'opus'})`, 'ask'],
  ['object literal holding agent', `await ({a: agent}).a('p')`, 'ask'],
  ['class instance field holding agent', `class C { f = agent }\nawait new C().f('p')`, 'ask'],
  ['class static field holding agent', `class C { static f = agent }\nawait C.f('p')`, 'ask'],
  ['class extending agent', `class C extends agent {}`, 'ask'],
  ['yield* of an array holding agent', `function* g() { yield* [agent] }\nfor (const f of g()) await f('p')`, 'ask'],
  ['yield agent', `function* g() { yield agent }`, 'ask'],
  ['comma expression', `await (0, agent)('p', {model: 'opus'})`, 'ask'],
  ['ternary', `await (cond ? agent : null)('p')`, 'ask'],
  ['logical or', `await (x || agent)('p')`, 'ask'],
  ['logical-assignment ||=', `let a; a ||= agent; await a('p')`, 'ask'],
  ['logical-assignment ??=', `let a; a ??= agent; await a('p')`, 'ask'],
  ['default parameter alias', `const g = (f = agent) => f('p', {model: 'opus'}); await g()`, 'ask'],
  ['catch-parameter alias', `try { throw agent } catch (e) { await e('p') }`, 'ask'],
  ['export {agent as x}', `export { agent as x }`, 'ask'],
  ['export {agent as default}', `export { agent as default }`, 'ask'],
  ['export {workflow as w}', `export { workflow as w }`, 'ask'],
  ['new agent', `new agent('p')`, 'ask'],
  ['typeof agent', `if (typeof agent === 'function') log('x')`, 'ask'],
  ['agent in a template placeholder', 'log(`${agent}`)', 'ask'],
  ['labelled block aliasing', `l: { const f = agent; await f('p') }`, 'ask'],
  ['workflow.call', `await workflow.call(null, 'x')`, 'ask'],
  ['workflow stored', `const w = [workflow]; await w[0]('x')`, 'ask'],
  ['await agent as a value', `const p = Promise.resolve(agent); (await p)('p')`, 'ask'],
  ['optional-chain call of agent', `await agent?.call(null, 'p')`, 'ask'],
  ['agent as a callback argument', `await Promise.all(items.map(agent))`, 'ask'],
  ['unicode-escaped identifier alias', `const a = \\u0061gent; await a('p')`, 'ask']
]);

table('adversarial battery: destructuring and member access of the watched names', [
  ['ObjectPattern agent from this', `const { agent: a } = this`, 'ask'],
  ['ObjectPattern shorthand agent', `const { agent } = args`, 'ask'],
  ['ObjectPattern renamed agent from args', `const { agent: a } = args; await a('p')`, 'ask'],
  ['ObjectPattern workflow', `function f({ workflow }) {}`, 'ask'],
  ['ObjectPattern with a default', `const { agent: a = null } = args`, 'ask'],
  ['assignment pattern', `let a; ({ agent: a } = args)`, 'ask'],
  ['for-of pattern', `for (const { agent: a } of xs) await a('p')`, 'ask'],
  ['catch pattern', `try {} catch ({ agent }) {}`, 'ask'],
  ['pattern with a string-built key', `const { ['ag' + 'ent']: a } = args`, 'ask'],
  ['pattern with a literal computed key', `const { ['agent']: a } = args`, 'ask'],
  ['pattern reading constructor', `const { constructor: F } = []`, 'ask'],
  ['pattern reading getPrototypeOf', `const { getPrototypeOf } = Object`, 'ask'],
  ['member of a call result', `const a = getTools().agent; await a('p')`, 'ask'],
  ['member of a nested member', `const a = args.tools.agent`, 'ask'],
  ['computed literal member of a call result', `const a = getTools()['agent']`, 'ask'],
  ['optional-chain member', `const a = getTools()?.agent`, 'ask'],
  ['optional computed literal member', `const a = getTools()?.['workflow']`, 'ask'],
  ['unicode-escaped property name', `const a = getTools()['\\u0061gent']`, 'ask'],
  ['unicode-escaped identifier property', `const a = foo().\\u0061gent`, 'ask'],
  ['member call on a local object', `const tools = { agent() {} }; tools.agent()`, 'ask'],
  ['member call through an array element', `await [tools][0].agent('p')`, 'ask'],
  ['reading agent off an array element', `const a = list[0].agent`, 'ask']
]);

table('adversarial battery: the global object and its aliases', [
  ['globalThis.agent', `await globalThis.agent('p', {model: 'opus'})`, 'ask'],
  ['global.agent', `await global.agent('p')`, 'ask'],
  ['window.agent', `await window.agent('p')`, 'ask'],
  ['self.agent', `await self.agent('p')`, 'ask'],
  ['globalThis string-built key', `await globalThis['ag' + 'ent']('p')`, 'ask'],
  ['globalThis destructuring', `const { agent: a } = globalThis`, 'ask'],
  ['unicode-escaped globalThis', `await \\u0067lobalThis.agent('p')`, 'ask'],
  ['bare this at top level', `const g = this`, 'ask'],
  ['this.agent', `await this.agent('p')`, 'ask'],
  ["this['agent']", `await this['agent']('p')`, 'ask'],
  ['this with a variable key', `const k = args.k; await this[k]('p')`, 'ask'],
  ['this in a sloppy function', `const g = (function () { return this })(); await g.agent('p')`, 'ask'],
  ['this in an arrow', `const g = (() => this)()`, 'ask'],
  ['this in an object method', `const o = { m() { return this } }; const m = o.m; m()`, 'ask'],
  ['this in a class method', `class C { m() { return this } }`, 'ask'],
  ['arguments object', `const f = function () { return arguments[0] }`, 'ask'],
  ['arguments.callee', `const f = function () { return arguments.callee }`, 'ask'],
  ['fn.caller', `const f = function () { return f.caller }`, 'ask'],
  ['Reflect.get(globalThis)', `await Reflect.get(globalThis, 'agent')('p')`, 'ask'],
  ['Reflect alone', `const r = Reflect`, 'ask'],
  ['Proxy', `const p = new Proxy({}, {})`, 'ask'],
  ['process', `const e = process.env`, 'ask'],
  ['require', `const fs = require('fs')`, 'ask'],
  ['Bun', `await Bun.spawn(['ls'])`, 'ask'],
  ['Deno', `await Deno.readTextFile('x')`, 'ask'],
  ['module', `module.exports = 1`, 'ask'],
  ['exports', `exports.x = 1`, 'ask'],
  ['import.meta', `const u = import.meta.url`, 'ask'],
  ['Error.prepareStackTrace getThis', `Error.prepareStackTrace = (e, s) => s[0].getThis(); const g = new Error().stack`, 'ask'],
  ['Error string-built prepareStackTrace write', `Error['prepare' + 'StackTrace'] = f`, 'ask'],
  ['Error.captureStackTrace', `Error.captureStackTrace(o)`, 'ask'],
  ['window in a conditional', `const w = typeof window !== 'undefined' ? window : null`, 'ask'],
  ['frames', `const f = frames`, 'ask'],
  ['document', `const d = document`, 'ask']
]);

table('adversarial battery: code synthesis and reflection', [
  ['eval', `eval("agent('p', {model: 'opus'})")`, 'ask'],
  ['indirect eval', `(0, eval)("agent('p')")`, 'ask'],
  ['new Function', `new Function("return agent")()('p')`, 'ask'],
  ['Function call', `Function("return agent")()('p')`, 'ask'],
  ['Function tagged template', 'Function`return agent`()', 'ask'],
  ['eval tagged template', 'eval`agent("p")`', 'ask'],
  ['instanceof Function', `if (f instanceof Function) log('x')`, 'ask'],
  ['[].constructor.constructor', `[].constructor.constructor("return agent")()('p')`, 'ask'],
  ['function constructor', `(function(){}).constructor("agent('p', {model: 'opus'})")()`, 'ask'],
  ['arrow constructor', `(() => {}).constructor('return this')()`, 'ask'],
  ['async arrow constructor', `(async () => {}).constructor('return this')()`, 'ask'],
  ['string constructor chain', `''.constructor.constructor('return this')()`, 'ask'],
  ['getPrototypeOf(async function).constructor', `Object.getPrototypeOf(async function () {}).constructor`, 'ask'],
  ['Object.getPrototypeOf', `Object.getPrototypeOf(x)`, 'ask'],
  ['Reflect.getPrototypeOf', `Reflect.getPrototypeOf(x)`, 'ask'],
  ['Object.setPrototypeOf', `Object.setPrototypeOf(opts, { model: 'opus' })`, 'ask'],
  ['Object.getOwnPropertyDescriptor', `Object.getOwnPropertyDescriptor(Object.prototype, '__proto__')`, 'ask'],
  ['Object.getOwnPropertyDescriptors', `Object.getOwnPropertyDescriptors(x)`, 'ask'],
  ['x.__proto__', `const p = x.__proto__`, 'ask'],
  ["x['__proto__']", `const p = x['__proto__']`, 'ask'],
  ['Object.prototype', `const p = Object.prototype`, 'ask'],
  ['__lookupGetter__', `({}).__lookupGetter__('x')`, 'ask'],
  ['__defineGetter__', `({}).__defineGetter__('x', f)`, 'ask'],
  ['unicode-escaped constructor key', `({})['\\u0063onstructor']`, 'ask'],
  ['string-built constructor key', `const k = 'con' + 'structor'; ({})[k][k]('return this')()`, 'ask'],
  ['string-built constructor inline', `({})['con' + 'structor']['con' + 'structor']('return this')()`, 'ask'],
  ['template-built key', 'x[`con${"structor"}`]', 'ask'],
  ['variable key from args', `const k = args.key; const v = data[k]`, 'ask'],
  ['for-in key lookup', `for (const k in o) { log(o[k]) }`, 'ask'],
  ['Object.entries key lookup', `for (const [k, v] of Object.entries(o)) log(v[k])`, 'ask'],
  ['reduce element used as key', `['constructor','constructor'].reduce((acc, i) => acc[i], [])`, 'ask'],
  ['dynamic read from a global-ish Error', `const E = Error; const v = E[args.k]`, 'ask'],
  ['setTimeout with a string', `setTimeout("agent('p', {model: 'opus'})", 0)`, 'ask'],
  ['setInterval with a template', 'setInterval(`agent("p")`, 1)', 'ask'],
  ['setTimeout with a concatenation', `setTimeout("agent(" + q + ")", 0)`, 'ask'],
  ['setTimeout kept as a value', `const t = setTimeout; t(code, 1)`, 'ask'],
  ['dynamic import', `await import('node:vm')`, 'ask'],
  ['import declaration with attributes', `import d from './x.json' with { type: 'json' }`, 'ask'],
  ['export * from', `export * from 'x'`, 'ask'],
  ['export {y} from', `export { y } from 'x'`, 'ask'],
  ['with statement (strict parse error)', `with (globalThis) { await agent('p') }`, 'ask'],
  ['importScripts', `importScripts('x')`, 'ask'],
  ['Worker', `const w = new Worker('x')`, 'ask']
]);

table('adversarial battery: options and model smuggling', [
  ['__proto__ in the options literal', `await agent('p', { label: 'x', __proto__: { model: 'opus' } })`, 'ask'],
  ["quoted '__proto__' in the options literal", `await agent('p', { label: 'x', '__proto__': { model: 'opus' } })`, 'ask'],
  ['getter model', `await agent('p', { get model() { return 'opus' } })`, 'ask'],
  ['unicode-escaped model key with opus', `await agent('p', { 'mod\\u0065l': 'opus' })`, 'ask'],
  ['Object.assign options', `await agent('p', Object.assign({}, a))`, 'ask'],
  ['Object.create options', `await agent('p', Object.create({ model: 'opus' }))`, 'ask'],
  ['__proto__ key on an unrelated object', `const o = { __proto__: base }`, 'ask']
]);

table('benign scripts stay silent', [
  ['JSON round trip', `const o = JSON.parse(JSON.stringify({ a: 1 })); return o`, 'silent'],
  ['Object.keys/entries/values/assign/fromEntries/freeze', `const o = { a: 1, b: 2 }
const ks = Object.keys(o), es = Object.entries(o), vs = Object.values(o)
const m = Object.assign({}, o, { c: 3 })
const f = Object.fromEntries(es.map(([k, v]) => [k, v * 2]))
Object.freeze(m); return { ks, vs, f }`, 'silent'],
  ['Array methods', `const r = [3, 1, 2].map((x) => x * 2).filter(Boolean).sort((a, b) => a - b).slice(0, 2).concat([9]).flat().reduce((a, b) => a + b, 0)
const j = ['a', 'b'].join(','), has = ['a'].includes('a'), idx = ['a'].indexOf('a')`, 'silent'],
  ['Math, Number, parseInt', `const m = Math.max(1, Math.min(2, Math.floor(3.5))) + Number('4') + parseInt('5', 10)`, 'silent'],
  ['template strings with placeholders', 'const p = `Review ${args.path} for ${args.focus}`\nawait agent(p)', 'silent'],
  ['async/await with Promise.all', `const [a, b] = await Promise.all([agent('a'), agent('b')]); return [a, b]`, 'silent'],
  ['sleep via setTimeout with a callback name', `await new Promise((resolve) => setTimeout(resolve, 100))`, 'silent'],
  ['sleep via setTimeout with an arrow', `await new Promise((r) => setTimeout(() => r(1), 100))`, 'silent'],
  ['phase, log, budget, args', `phase('Scan'); log('start ' + args.n)
const n = budget.total ? Math.floor(budget.total / 100_000) : 5
while (budget.total && budget.remaining() > 50_000) { log('x'); break }`, 'silent'],
  ['pipeline of agents', `const out = await pipeline(args.files, (f) => agent('read ' + f, { label: f }), (r, f, i) => agent('check ' + r, { label: 'c' + i }))
return out.filter(Boolean)`, 'silent'],
  ['parallel thunks', `const rs = await parallel(args.items.map((x) => () => agent('do ' + x, { schema: { type: 'object', properties: {} } })))
return rs.filter(Boolean)`, 'silent'],
  ['Array.from of thunks', `const votes = await parallel(Array.from({ length: 3 }, () => () => agent('refute', { schema: V })))
const ok = votes.filter(Boolean).filter((v) => !v.refuted).length >= 2`, 'silent'],
  ['.then on a parallel', `const j = await parallel(fresh.map((b) => () => parallel(['a','b'].map((lens) => () => agent('judge ' + lens, { phase: 'Verify' }))).then((vs) => ({ b, real: vs.filter(Boolean).length >= 2 }))))`, 'silent'],
  ['loop until dry with a Set', `const seen = new Set(), confirmed = []
let dry = 0
while (dry < 2) {
  const found = (await parallel(FINDERS.map((f) => () => agent(f.prompt, { phase: 'Find', schema: BUGS })))).filter(Boolean).flatMap((r) => r.bugs)
  const fresh = found.filter((b) => !seen.has(key(b)))
  if (!fresh.length) { dry++; continue }
  dry = 0; fresh.forEach((b) => seen.add(key(b)))
}
return confirmed`, 'silent'],
  ['indexed loop reading results[i]', `const results = []
for (let i = 0; i < args.items.length; i++) { results.push(await agent('x' + args.items[i])); log(results[i]) }
const last = results[results.length - 1], first = results[0], next = results[1 + 1]`, 'silent'],
  ['callback index', `const out = await Promise.all(items.map(async (x, i) => ({ x, prev: items[i - 1], cur: items[i] })))`, 'silent'],
  ['write into a fresh local map', `const counts = {}
for (const w of words) { counts[w] = w.length }
return counts`, 'silent'],
  ['reading agent and workflow as data off args or locals', `const v = args.agent + args.workflow + data['agent']`, 'silent'],
  ['object keys named agent and workflow', `const cfg = { agent: 1, workflow: 2, constructor: 3, prototype: 4, model: 'opus' }`, 'silent'],
  ['a label named agent', `agent: for (;;) { break agent }`, 'silent'],
  ['array and object spread', `const a = [...x, ...y], o = { ...p, q: 1 }`, 'silent'],
  ['destructuring of plain names', `const { a, b: c } = args; const [d, e] = await parallel([() => agent('1'), () => agent('2')])`, 'silent'],
  ['try/catch with Error', `try { throw new Error('x') } catch (e) { log(e.message); if (e instanceof Error) log('err') }`, 'silent'],
  ['Date-free string work', `const s = String(x).split('\\n').map((l) => l.trim()).filter((l) => /^foo/.test(l)).join('|')`, 'silent'],
  ['direct calls from nested functions', `const run = async (p) => agent(p, { label: p })\nconst go = async function (p) { return agent(p) }`, 'silent'],
  ['typeof of other names', `if (typeof x === 'string') log(x)`, 'silent'],
  ['meta with phases', `export const meta = { name: 'a', description: 'b', phases: [{ title: 'T', detail: 'd' }] }\nphase('T')`, 'silent'],
  ['optional chaining on data', `const n = args.items?.length ?? 0, f = args.cfg?.name`, 'silent'],
  ['Symbol and Number constants', `const big = Number.MAX_SAFE_INTEGER, nan = Number.isNaN(1)`, 'silent'],
  ['classes without this', `class Box { static of(v) { return new Box() } }`, 'silent'],
  ['getter and setter on a plain object', `const o = { get a() { return 1 }, set a(v) {} }`, 'silent'],
  ['regexp and string replace', `const t = x.replace(/a/g, 'b').toUpperCase()`, 'silent'],
  ['direct agent call with every option', `await agent('p', { label: 'l', phase: 'P', schema: S, model: 'sonnet', effort: 'low', isolation: 'worktree' })`, 'silent']
]);

describe('benign scripts: the captured fixtures stay silent', () => {
  it('the inline script fixture', () => {
    const p = JSON.parse(fs.readFileSync(path.join(FIX, 'PreToolUse.workflow-script.json'), 'utf8'));
    expect(check(p.tool_input.script)).toEqual([]);
  });
});

describe('accepted false positives are asked, not silenced', () => {
  it('a local method called agent asks (fail closed beats a bypass)', () => {
    expect(check(`const client = { agent() { return 1 } }; client.agent()`).length).toBeGreaterThan(0);
  });
  it('a variable-key lookup asks even on plain data', () => {
    expect(check(`const o = {}; log(o[args.key])`).length).toBeGreaterThan(0);
  });
  it('`this` asks even inside a class method', () => {
    expect(check(`class C { m() { return this.x } }`).length).toBeGreaterThan(0);
  });
});

/**
 * Idioms that still pass. Listed so the report is honest and a future tightening is visible here:
 * when one of these starts asking, move it into an `ask` table above.
 */
describe('STILL_SLIPS: documented limits of static analysis (these pass today)', () => {
  const slips: Array<[string, string]> = [
    ['setTimeout given a string held in a variable (Node and Bun throw on it; a browser runtime would evaluate it)', `const s = args.code; setTimeout(s, 0)`],
    ['a user-defined map() that passes a string as the index parameter', `const o = { map(f) { return f('a', 'constructor') } }; o.map((x, i) => ({})[i])`],
    ['a `.length` that is not a number is trusted as a numeric key', `const o = { length: 'constructor' }; const F = ({})[o.length][o.length]`],
    ['a member read of the watched names off a plain, unbound name (the sandbox could expose one)', `const f = args.agent`]
  ];
  for (const [name, script] of slips) {
    it(name, () => {
      expect(check(script)).toEqual([]);
    });
  }

  it('a binding that receives a string is not trusted as an index', () => {
    expect(check(`let i = 0; i = args.k; log(({})[i])`).length).toBeGreaterThan(0);
  });
});
