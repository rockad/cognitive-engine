import { describe, it, expect } from 'bun:test';
import { checkScriptText } from '../src/workflow-guard.ts';

/**
 * Click-through false positives: `this` and variable-key lookups used to ask even when provably
 * harmless. The guard now follows `const` bindings (declared once in the whole script, in scope at
 * the reference, initialised with a string literal or a flat object of string literals that is only
 * ever read) and `this.member` inside a class body (only as the object of a plain member access:
 * bare `this` as a value asks, workflow-guard-this.test.ts). Everything the
 * earlier reviews found as a bypass must still ask, including the near-misses of these new rules.
 */
const SESSION = 'claude-sonnet-5-5';
const check = (script: string) => checkScriptText(script, SESSION);

describe('provably harmless: silent', () => {
  const silent: Array<[string, string]> = [
    ['a const string model at or below the session', `const M = 'haiku'; await agent('p', { model: M })`],
    ['a shorthand model property over a const', `const model = 'haiku'; await agent('p', { model })`],
    ['a const string agent model, exported', `export const M = 'sonnet'; await agent('p', { model: M })`],
    ['a model looked up in a const object by a const key', `const cfg = { fast: 'haiku', deep: 'opus' }; const key = 'fast'; await agent('p', { model: cfg[key] })`],
    ['a model read by a literal member of a const object', `const cfg = { fast: 'haiku' }; await agent('p', { model: cfg.fast })`],
    ['a model read by a literal computed member', `const cfg = { fast: 'haiku' }; await agent('p', { model: cfg['fast'] })`],
    ['a const object with quoted and numeric keys', `const cfg = { 'a-b': 'haiku', 1: 'sonnet' }; const k = 'a-b'; await agent('p', { model: cfg[k] })`],
    ['a variable-key lookup on plain data with a const key', `const o = { a: 1 }; const k = 'a'; log(o[k])`],
    ['a const key inside a function, used in that function', `function f(o) { const k = 'a'; return o[k] }`],
    ['a const key in a block, used in that block', `{ const k = 'a'; const o = {}; log(o[k]) }`],
    ['a const key used in a nested closure', `const k = 'a'; const o = {}; const f = () => o[k]; f()`],
    ['a computed option key that is a const spelling a harmless name', `const k = 'label'; await agent('p', { [k]: 'x' })`],
    ['a computed destructuring key from a const', `const k = 'a'; const { [k]: v } = args`],
    ['this in a class method', `class C { m() { return this.x } }`],
    ['this in a constructor, field initialiser and static block', `class C { x = this.y; constructor() { this.a = 1 } static { this.z = 1 } }`],
    ['this in an arrow function inside a class method', `class C { m() { return () => this.x } }`],
    ['this.x in a plain function nested in a class method (strict code)', `class C { m() { return function () { return this.x } } }`],
    ['this in a class expression', `const C = class { m() { return this.x } }`]
  ];
  for (const [name, script] of silent) {
    it(name, () => {
      expect(check(script)).toEqual([]);
    });
  }

  it('a const workflow name is resolved like a literal one (and a let is not)', () => {
    const resolver = {
      readScriptFile: () => ({ error: 'no' }),
      findNamedWorkflow: (n: string) => (n === 'known' ? { text: `await agent('x', { model: 'haiku' })`, label: 'known.js' } : { error: `workflow "${n}" not found` }),
      agentDefinition: () => ({ found: false, model: null })
    };
    expect(checkScriptText(`const W = 'known'; await workflow(W)`, SESSION, resolver)).toEqual([]);
    expect(checkScriptText(`const W = 'other'; await workflow(W)`, SESSION, resolver)).toHaveLength(1);
    expect(checkScriptText(`let W = 'known'; await workflow(W)`, SESSION, resolver)).toHaveLength(1);
  });
});

describe('literals are followed, not trusted: the model is still ranked', () => {
  it('a const string above the session model asks as a literal would', () => {
    const s = check(`const M = 'opus'; await agent('p', { model: M })`);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('stronger than the session model');
  });
  it('a const-object lookup that lands on a stronger model asks', () => {
    const s = check(`const cfg = { fast: 'haiku', deep: 'opus' }; const key = 'deep'; await agent('p', { model: cfg[key] })`);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('stronger than the session model');
  });
  it('an unrankable const model asks', () => {
    expect(check(`const M = 'gpt-9'; await agent('p', { model: M })`)).toHaveLength(1);
  });
  it('a key that is not an own property of the const object asks', () => {
    expect(check(`const cfg = { fast: 'haiku' }; const key = 'slow'; await agent('p', { model: cfg[key] })`)).toHaveLength(1);
    expect(check(`const cfg = { fast: 'haiku' }; await agent('p', { model: cfg.toString })`)).toHaveLength(1);
    expect(check(`const cfg = { fast: 'haiku' }; const k = 'constructor'; await agent('p', { model: cfg[k] })`).length).toBeGreaterThan(0);
  });
  it('a const agentType is looked up like a literal one', () => {
    const s = check(`const T = 'ghost'; await agent('p', { agentType: T })`);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('"ghost"');
  });
  it('a const computed key spelling `model` is read as model', () => {
    expect(check(`const k = 'model'; await agent('p', { [k]: 'opus' })`)).toHaveLength(1);
    expect(check(`const k = 'model'; await agent('p', { [k]: args.m })`)).toHaveLength(1);
    expect(check(`const k = 'model'; await agent('p', { [k]: 'haiku' })`)).toEqual([]);
  });
});

describe('near-misses of the new rules still ask', () => {
  const asks: Array<[string, string]> = [
    ['let is not const', `let k = 'a'; const o = {}; log(o[k])`],
    ['var is not const', `var k = 'a'; const o = {}; log(o[k])`],
    ['a const initialised from data', `const k = args.key; const o = {}; log(o[k])`],
    ['a const initialised with a call', `const k = String(args.k); const o = {}; log(o[k])`],
    ['a const initialised from a template with an expression', 'const k = `a${args.x}`; const o = {}; log(o[k])'],
    ['a const initialised by concatenation', `const k = 'a' + args.x; const o = {}; log(o[k])`],
    ['a name bound twice (shadowed by a parameter)', `const k = 'a'; function f(k) { return ({})[k] }`],
    ['a name bound twice (shadowed by an inner const)', `const k = 'a'; { const k = args.k; log(({})[k]) }`],
    ['a name bound twice (a catch parameter)', `const k = 'a'; try { f() } catch (k) { log(({})[k]) }`],
    ['a name bound twice (a function declaration)', `const k = 'a'; function k2() {} function k() {} log(({})[k])`],
    ['a const out of scope at the reference (block)', `{ const k = 'a' } log(({})[k])`],
    ['a const out of scope at the reference (function)', `function f() { const k = 'a' } log(({})[k])`],
    ['a const key spelling constructor', `const k = 'constructor'; const F = ({})[k]`],
    ['a const object whose .length is a followed string used as a key (the old STILL_SLIPS shape)', `const o = { length: 'constructor' }; const F = ({})[o.length][o.length]`],
    ['a shorthand model over a const above the session', `const model = 'opus'; await agent('p', { model })`],
    ['a const key spelling __proto__', `const k = '__proto__'; const F = ({})[k]`],
    ['a const key spelling prototype', `const k = 'prototype'; const F = (() => 1)[k]`],
    ['a const key spelling agent called through a member', `const k = 'agent'; const o = {}; o[k]('p', { model: 'opus' })`],
    ['a const key spelling agent read off a call result', `const k = 'agent'; const f = g()[k]`],
    ['a const key spelling a destructured agent', `const k = 'agent'; const { [k]: a } = args`],
    ['key concatenation', `const k = 'con'; const F = ({})[k + 'structor']`],
    ['key template literal with an expression', 'const F = ({})[`con${args.x}`]'],
    ['key template literal over a const', 'const k = "constructor"; const F = ({})[`${k}`]'],
    ['a model from a template with a const', 'const M = "haiku"; await agent("p", { model: `${M}` })'],
    ['a model by concatenation', `const M = 'hai'; await agent('p', { model: M + 'ku' })`],
    ['a model from a let', `let M = 'haiku'; await agent('p', { model: M })`],
    ['a model from args', `await agent('p', { model: args.model })`],
    ['a model from an unresolved lookup', `const cfg = { a: 'haiku' }; await agent('p', { model: cfg[args.k] })`],
    ['a model from a lookup on a non-const object', `let cfg = { a: 'haiku' }; await agent('p', { model: cfg.a })`],
    ['a model from a lookup on a parameter', `function f(cfg) { return agent('p', { model: cfg.a }) }`],
    ['a model from a const object with a spread', `const cfg = { ...args, a: 'haiku' }; await agent('p', { model: cfg.a })`],
    ['a model from a const object with a computed key', `const cfg = { [args.k]: 'haiku' }; await agent('p', { model: cfg.a })`],
    ['a model from a const object with a getter', `const cfg = { get a() { return args.m } }; await agent('p', { model: cfg.a })`],
    ['a model from a const object with a method', `const cfg = { a() { return 'haiku' } }; await agent('p', { model: cfg.a })`],
    ['a model from a const object with a non-literal value', `const cfg = { a: args.m }; await agent('p', { model: cfg.a })`],
    ['a const object whose property is assigned', `const cfg = { a: 'haiku' }; cfg.a = args.m; await agent('p', { model: cfg.a })`],
    ['a const object whose property is assigned by a computed key', `const cfg = { a: 'haiku' }; cfg[args.k] = args.m; await agent('p', { model: cfg.a })`],
    ['a const object whose property is updated with an operator', `const cfg = { a: 'haiku' }; cfg.a += args.m; await agent('p', { model: cfg.a })`],
    ['a const object whose property is deleted', `const cfg = { a: 'haiku' }; delete cfg.a; await agent('p', { model: cfg.a })`],
    ['a const object that is aliased', `const cfg = { a: 'haiku' }; const c2 = cfg; c2.a = args.m; await agent('p', { model: cfg.a })`],
    ['a const object passed to a call', `const cfg = { a: 'haiku' }; Object.assign(cfg, args); await agent('p', { model: cfg.a })`],
    ['a const object spread into another', `const cfg = { a: 'haiku' }; const c2 = { ...cfg }; await agent('p', { model: cfg.a })`],
    ['a const object destructured by assignment', `const cfg = { a: 'haiku' }; ({ a: cfg.a } = args); await agent('p', { model: cfg.a })`],
    ['a const object written in a for-of head', `const cfg = { a: 'haiku' }; for (cfg.a of args.xs) {} await agent('p', { model: cfg.a })`],
    ['a const object returned from a function', `const cfg = { a: 'haiku' }; const g = () => cfg; g().a = args.m; await agent('p', { model: cfg.a })`],
    ['aliasing agent', `const a = agent; await a('p', { model: 'opus' })`],
    ['aliasing agent through a const object', `const o = { a: agent }; await o.a('p')`],
    ['agent as a value', `const fns = [agent]`],
    ['spread options', `await agent('p', { ...args.opts })`],
    ['spread options beside a const model', `const M = 'haiku'; await agent('p', { model: M, ...args.opts })`],
    ['a computed option key from data', `await agent('p', { [args.k]: 'opus' })`],
    ['a computed option key from a let', `let k = 'label'; await agent('p', { [k]: 'x' })`],
    ['dynamic import', `await import('x')`],
    ['dynamic import of a const', `const m = 'x'; await import(m)`],
    ['eval', `eval('agent("p", {model: "opus"})')`],
    ['eval through a const alias', `const e = eval; e('1')`],
    ['Function constructor', `new Function('return agent')()`],
    ['Function through a const key', `const k = 'constructor'; const F = (() => 1)[k]; F('return agent')()`],
    ['globalThis', `const g = globalThis`],
    ['this at the top level', `const t = this`],
    ['this in a plain function', `function f() { return this }`],
    ['this in an object method', `const o = { m() { return this } }`],
    ['this in an arrow at the top level', `const f = () => this`],
    ['this in a class computed key (the outer this)', `class C { [this.k]() {} }`],
    ['this in a class extends clause (the outer this)', `class C extends this.B {}`],
    ['this in a class decorator-like computed field key', `class C { [this.x] = 1 }`],
    ['this.constructor inside a class', `class C { m() { return this.constructor } }`],
    ['this[k] with an unresolved key inside a class', `class C { m(k) { return this[k] } }`],
    ['this.agent read inside a class', `class C { m() { return this.agent } }`],
    ['this.agent called inside a class', `class C { m() { return this.agent('p') } }`],
    ['this[const constructor] inside a class', `class C { m() { const k = 'constructor'; return this[k] } }`],
    ['a template model with an expression', 'await agent("p", { model: `${args.m}` })'],
    ['a workflow name from a let', `let W = 'known'; await workflow(W)`]
  ];
  for (const [name, script] of asks) {
    it(name, () => {
      expect(check(script).length).toBeGreaterThan(0);
    });
  }
});
