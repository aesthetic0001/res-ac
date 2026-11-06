import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { encodeCommand } from '../src/command-wire.mjs';

const cases = JSON.parse(readFileSync(new URL('../../test/fixtures/command-wire.json', import.meta.url)));
test('binary command encoding matches the shared Python interoperability vectors', () => {
  for (const { value, hex } of cases) assert.equal(encodeCommand(value).toString('hex'), hex);
  assert.equal(encodeCommand(-0).subarray(4).toString('hex'), '648000000000000000');
});
test('binary command encoding bounds body size, complexity and finite values', () => {
  for (const value of [NaN, Infinity, undefined, () => {}, new Date(), '\ud800', 'x'.repeat(32768),
    Array(1024).fill(null), Object.fromEntries(Array.from({length: 512}, (_, i) => [i, null]))])
    assert.throws(() => encodeCommand(value));
  let nested = null;
  for (let i = 0; i < 17; i++) nested = [nested];
  assert.throws(() => encodeCommand(nested));
});
