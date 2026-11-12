import { describe, it, expect } from 'bun:test';
import { parseUserIntent } from '../src/advisor-logic.ts';

// Review findings on the explicit-instruction parser: a model named inside code, a quotation, a
// question, a supposition, a pasted log line or a poem is not an instruction.

describe('the parser ignores a model that is not an instruction (table)', () => {
  it.each([
    ['inside a quotation, then a question', 'The docs say: "use opus for hard problems". What does that mean'],
    ['inside a single-quoted string', "The config has 'use opus' somewhere in it"],
    ['inside a curly-quoted string', 'The docs say “use opus for hard problems” and nothing else'],
    ['a question that ends in ?', 'Should I use opus for this?'],
    ['a polite request phrased as a question (a ? sentence never counts)', 'Can you use opus for this?'],
    ['a supposition starting with if', 'if we need opus, tell me first'],
    ['a how-question', 'How do I use opus for the review'],
    ['inside an inline code span, in a question', 'Why does `use haiku` appear in the config?'],
    ['inside an inline code span', 'The config line `use opus` is old'],
    ['inside a fenced code block', 'Fix this config:\n```\nuse opus\n```\nand tell me what changed'],
    ['inside an unterminated fence', 'Here is the log:\n```\nuse opus'],
    ['a pasted log line from the agent', 'agent: I will use opus to analyse'],
    ['a first-person statement of intent', "I'll use opus to analyse the diff"],
    ['a quoted reply line', '> use opus for the review\nwhat do you think'],
    ['a flag in prose about the flag', 'What does the --model opus flag do'],
    ['a flag without an imperative verb', 'The default is --model opus in that script'],
    ['a poem request', 'we need a haiku about spring'],
    ['a poem format', 'use haiku format for the release notes'],
    ['a poet', 'pick a sonnet by Shakespeare'],
    ['a numbered sonnet', 'use sonnet 130 as the example'],
    ['two different models for two jobs (ambiguous)', 'use sonnet for the tests and opus for the review'],
    ['a non-English verb stays a safe no-match', 'Используй opus']
  ])('%s: %j gives no model', (_name, prompt) => {
    expect(parseUserIntent(prompt).model).toBeUndefined();
  });

  it.each([
    ['a minimal viable product', 'Write a minimal viable product spec'],
    ['the MVP abbreviation', 'Build an MVP of the dashboard'],
    ['a question about the cheapest model', 'What is the cheapest model for this?'],
    ['quoted', 'The ticket says "choose minimal viable model" and I disagree'],
    ['in a code span', 'Is `cheapest` a valid value in the config']
  ])('%s: %j gives no cost instruction', (_name, prompt) => {
    expect(parseUserIntent(prompt).cost).toBeUndefined();
  });
});

describe('the parser still reads a real instruction (table)', () => {
  it.each([
    ['a full model id', 'use claude-opus-5-5', 'opus'],
    ['a full model id with a date suffix', 'run it with claude-sonnet-5-5-20261028', 'sonnet'],
    ['the flag in an imperative clause', 'run with --model opus', 'opus'],
    ['the flag with an equals sign', 'start it with --model=haiku', 'haiku'],
    ['upper case', 'USE OPUS', 'opus'],
    ['a version after the name', 'use sonnet 5.5 for the edits', 'sonnet'],
    ['a second line', 'Fix the parser.\nuse haiku for the rename', 'haiku'],
    ['a quote earlier in the prompt', 'The ticket says "fix it". Use sonnet for it', 'sonnet'],
    ['an apostrophe is not a quote', "Don't overthink it, use sonnet; it's fine", 'sonnet'],
    ['a replaced model', 'use sonnet instead of opus', 'sonnet'],
    ['a preference over another', 'use opus over sonnet', 'opus'],
    ['a rejected alternative', 'use sonnet, not opus', 'sonnet']
  ])('%s: %j names %s', (_name, prompt, model) => {
    expect(parseUserIntent(prompt).model).toBe(model as 'opus');
  });

  it('a real cost instruction after a quotation is kept', () => {
    expect(parseUserIntent('The ticket says "fast". Choose minimal viable model for each').cost).toBe('minimal');
  });
});
