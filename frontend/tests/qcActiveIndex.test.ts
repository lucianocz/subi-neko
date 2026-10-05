// Run: npm test   (node's built-in runner + native TS stripping, no extra deps)
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildActiveIndex, findActiveIndices } from '../src/utils/qcActiveIndex.ts';
import { spaceBelongsToTarget } from '../src/utils/qcKeyboard.ts';
import { plainAssText, formatMs } from '../src/utils/qcText.ts';

const ev = (start_ms: number, end_ms: number, is_hidden = false) => ({ start_ms, end_ms, is_hidden });

test('empty list', () => {
  assert.deepEqual(findActiveIndices(buildActiveIndex([]), 100), []);
});

test('half-open interval [start, end)', () => {
  const idx = buildActiveIndex([ev(1000, 2000)]);
  assert.deepEqual(findActiveIndices(idx, 999), []);
  assert.deepEqual(findActiveIndices(idx, 1000), [0]);
  assert.deepEqual(findActiveIndices(idx, 1999.9), [0]);
  assert.deepEqual(findActiveIndices(idx, 2000), []);
});

test('overlapping dialogue + a long sign spanning many short events', () => {
  const events = [ev(0, 10_000), ev(1000, 2000), ev(1500, 3000), ev(2500, 2600), ev(4000, 5000)];
  const idx = buildActiveIndex(events);
  assert.deepEqual(findActiveIndices(idx, 1700), [0, 1, 2]);
  assert.deepEqual(findActiveIndices(idx, 2550), [0, 2, 3]);
  assert.deepEqual(findActiveIndices(idx, 3500), [0]);
  assert.deepEqual(findActiveIndices(idx, 10_000), []);
});

test('equal starts keep list order; zero-length events never active', () => {
  const idx = buildActiveIndex([ev(100, 500), ev(100, 300), ev(100, 100)]);
  assert.deepEqual(findActiveIndices(idx, 100), [0, 1]);
});

test('hidden events are never active, even when covering the time', () => {
  const idx = buildActiveIndex([ev(0, 5000, true), ev(1000, 2000)]);
  assert.deepEqual(findActiveIndices(idx, 1500), [1]);
  assert.deepEqual(findActiveIndices(idx, 4000), []);
});

test('matches a brute-force scan on random overlapping data (seek forward/back)', () => {
  let seed = 12345;
  const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32;
  const events = Array.from({ length: 3000 }, () => {
    const s = Math.floor(rnd() * 600_000);
    const long = rnd() < 0.02;
    return ev(s, s + Math.floor(rnd() * (long ? 120_000 : 4000)), rnd() < 0.05);
  }).sort((a, b) => a.start_ms - b.start_ms);
  const idx = buildActiveIndex(events);
  for (let k = 0; k < 2000; k++) {
    const t = rnd() * 650_000;
    const expected: number[] = [];
    events.forEach((e, i) => { if (!e.is_hidden && e.start_ms <= t && t < e.end_ms) expected.push(i); });
    assert.deepEqual(findActiveIndices(idx, t), expected);
  }
});

test('Space guard: text entry and natively-handled elements keep Space', () => {
  const el = (tagName: string, attrs: Record<string, string> = {}, isContentEditable = false) =>
    ({ tagName, isContentEditable, getAttribute: (n: string) => attrs[n] ?? null });
  for (const t of ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'VIDEO']) assert.equal(spaceBelongsToTarget(el(t)), true, t);
  assert.equal(spaceBelongsToTarget(el('DIV', {}, true)), true);
  assert.equal(spaceBelongsToTarget(el('DIV', { contenteditable: 'plaintext-only' })), true);
  assert.equal(spaceBelongsToTarget(el('DIV', { role: 'textbox' })), true);
  assert.equal(spaceBelongsToTarget(el('DIV')), false);
  assert.equal(spaceBelongsToTarget(el('BODY')), false);
  assert.equal(spaceBelongsToTarget(null), false);
});

test('text helpers', () => {
  assert.equal(plainAssText('{\\an8}Ahoj\\Nsvěte\\h!'), 'Ahoj světe !');
  assert.equal(plainAssText(null), '');
  assert.equal(formatMs(83_450), '1:23.450');
  assert.equal(formatMs(3_723_004), '1:02:03.004');
});
