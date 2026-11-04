import { describe, it, expect } from 'bun:test';
import { checkScriptText } from '../src/workflow-guard.ts';

/**
 * `this` inside a class body. Strict mode only stops the implicit substitution of the global
 * object: a caller can still pass it (`C.run.call(globalThis)`, a host API that invokes a callback
 * with the global as `this`), and then `Object.values(this)` hands the script `agent`. So inside a
 * class body `this` is allowed only as the object of a non-computed member read or write whose name
 * is neither watched nor an escape (`this.x`, `this.x = v`, `this.x()`). Bare `this` flowing as a
 * value (argument, spread, return, assignment right-hand side, computed member object, ...) asks.
 */
const SESSION = 'claude-sonnet-5-5';
const check = (script: string) => checkScriptText(script, SESSION);

describe('this as the object of a plain member access in a class body is silent', () => {
  const silent: Array<[string, string]> = [
    ['read', `class C { m() { return this.x } }`],
    ['write', `class C { m() { this.x = 1 } }`],
    ['compound write', `class C { m() { this.n += 1 } }`],
    ['call of a method', `class C { m() { return this.helper(1) } helper(v) { return v } }`],
    ['chained read', `class C { m() { return this.a.b.c } }`],
    ['private field', `class C { #p = 1; m() { return this.#p } }`],
    ['optional chain read', `class C { m() { return this?.x } }`],
    ['constructor, field initialiser and static block', `class C { x = this.y; constructor() { this.a = 1 } static { this.z = 1 } }`],
    ['arrow inside a method', `class C { m() { return () => this.x } }`],
    ['class expression', `const C = class { m() { return this.x } }`],
    ['static method', `class C { static make() { return this.count } }`],
    ['member of this passed on', `class C { m() { return list.map(this.fn) } }`],
    ['this.x in a template', 'class C { m() { return `${this.x}` } }'],
    ['an agent() call with a model inside a method', `class C { async m() { return agent('p', { model: 'haiku' }) } }`]
  ];
  for (const [name, script] of silent) {
    it(name, () => expect(check(script)).toEqual([]));
  }
});

describe('bare this as a value in a class body asks', () => {
  const asks: Array<[string, string]> = [
    ['the reviewed exploit: Object.values(this) with .call(null)', `class C { static run(){ return Object.values(this) } } const fs = C.run.call(null); fs[0]('p', {model:'opus'})`],
    ['Object.getOwnPropertyNames(this)', `class C { static run() { return Object.getOwnPropertyNames(this) } }`],
    ['Object.entries(this)', `class C { m() { return Object.entries(this) } }`],
    ['Object.keys(this)', `class C { m() { return Object.keys(this) } }`],
    ['return this', `class C { m() { return this } }`],
    ['return this from a nested function', `class C { m() { return function () { return this } } }`],
    ['return an arrow that returns this', `class C { m() { return () => this } }`],
    ['assign to a local', `class C { m() { const s = this; return s } }`],
    ['assign to a property', `class C { m() { other.self = this } }`],
    ['assignment right-hand side', `class C { m() { let s; s = this; return s } }`],
    ['argument of a call', `class C { m() { return f(this) } }`],
    ['argument of new', `class C { m() { return new Foo(this) } }`],
    ['array element', `class C { m() { return [this] } }`],
    ['array spread', `class C { m() { return [...this] } }`],
    ['object spread', `class C { m() { return { ...this } } }`],
    ['object value', `class C { m() { return { self: this } } }`],
    ['for-in over this', `class C { m() { for (const k in this) log(k) } }`],
    ['for-of over this', `class C { m() { for (const v of this) log(v) } }`],
    ['destructuring this', `class C { m() { const { a } = this; return a } }`],
    ['computed member with a literal number', `class C { m() { return this[0] } }`],
    ['computed member with a variable', `class C { m(k) { return this[k] } }`],
    ['computed member with a string literal', `class C { m() { return this['x'] } }`],
    ['optional computed member', `class C { m(k) { return this?.[k] } }`],
    ['typeof this', `class C { m() { return typeof this } }`],
    ['template with this', 'class C { m() { return `${this}` } }'],
    ['conditional on this', `class C { m() { return this ? 1 : 2 } }`],
    ['logical or with this', `class C { m() { return this || other } }`],
    ['this as a callee argument via call', `class C { m() { return helper.call(this) } }`],
    ['field initialiser holding this', `class C { self = this }`],
    ['static field initialiser holding this', `class C { static self = this }`],
    ['static block passing this', `class C { static { register(this) } }`],
    ['yield this', `class C { *m() { yield this } }`],
    ['await this', `class C { async m() { return await this } }`],
    ['comma expression', `class C { m() { return (0, this) } }`],
    ['this.constructor', `class C { m() { return this.constructor } }`],
    ['this.agent read', `class C { m() { return this.agent } }`],
    ['this.agent called', `class C { m() { return this.agent('p', { model: 'opus' }) } }`],
    ["this['agent'] called", `class C { m() { return this['agent']('p') } }`],
    ['this.__proto__', `class C { m() { return this.__proto__ } }`],
    ['this.workflow called', `class C { m() { return this.workflow('x') } }`]
  ];
  for (const [name, script] of asks) {
    it(name, () => expect(check(script).length).toBeGreaterThan(0));
  }
});

describe('this outside a class body still asks', () => {
  const asks: Array<[string, string]> = [
    ['top level', `const t = this`],
    ['top level member', `const t = this.x`],
    ['plain function', `function f() { return this.x }`],
    ['object method', `const o = { m() { return this.x } }`],
    ['arrow at the top level', `const f = () => this.x`],
    ['computed class key (the outer this)', `class C { [this.k]() {} }`],
    ['class extends clause (the outer this)', `class C extends this.B {}`]
  ];
  for (const [name, script] of asks) {
    it(name, () => expect(check(script).length).toBeGreaterThan(0));
  }
});
