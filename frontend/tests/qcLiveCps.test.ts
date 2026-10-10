import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { computeCps, cpsSeverity, visibleLength } from '../src/utils/cps.ts';

// Same fixture the backend asserts against `app/subs/readability.compute_cps`.
const cases = JSON.parse(
  readFileSync(new URL('../../backend/tests/fixtures/cps_cases.json', import.meta.url), 'utf8'),
) as { name: string; text: string; start_ms: number; end_ms: number; cps: number | null }[];

for (const c of cases) {
  test(`matches backend: ${c.name}`, () => {
    const got = computeCps(c.text, c.start_ms, c.end_ms);
    if (c.cps === null) assert.equal(got, null);
    else assert.ok(got !== null && Math.abs(got - c.cps) < 1e-9, `${got} vs ${c.cps}`);
  });
}

test('unsaved text changes CPS immediately', () => {
  assert.equal(computeCps('abcde', 0, 1000), 5);
  assert.equal(computeCps('abcdefghij', 0, 1000), 10);
});

test('timing changes recompute CPS', () => {
  assert.equal(computeCps('abcdefghij', 0, 1000), 10);
  assert.equal(computeCps('abcdefghij', 0, 2000), 5);
  assert.equal(computeCps('abcdefghij', 500, 1000), 20);
});

test('escaped line breaks and tags are not visible characters', () => {
  assert.equal(visibleLength(String.raw`{\an8}ab\Ncd`), 5);
  assert.equal(visibleLength(null), 0);
});

test('threshold boundaries are strict and use the unrounded value', () => {
  const limits = { soft: 15, hard: 17 };
  assert.equal(cpsSeverity(computeCps('a'.repeat(15), 0, 1000), limits), 'normal');
  assert.equal(cpsSeverity(computeCps('a'.repeat(16), 0, 1000), limits), 'warning');
  assert.equal(cpsSeverity(computeCps('a'.repeat(17), 0, 1000), limits), 'warning');
  assert.equal(cpsSeverity(computeCps('a'.repeat(18), 0, 1000), limits), 'error');
  // 15.04 displays as "15.0" but is already over the soft limit.
  assert.equal(cpsSeverity(computeCps('a'.repeat(1504), 0, 100_000), limits), 'warning');
});

test('empty text and invalid durations give no CPS', () => {
  assert.equal(computeCps('', 0, 1000), null);
  assert.equal(computeCps('abc', 1000, 1000), null);
  assert.equal(computeCps('abc', 2000, 1000), null);
  assert.equal(computeCps('abc', 0, Number.NaN), null);
  assert.equal(cpsSeverity(null, { soft: 1, hard: 2 }), 'normal');
});

test('detail panel derives CPS locally from the draft and keeps blur-save wiring', () => {
  const src = readFileSync(new URL('../src/components/qc/QcEventDetail.tsx', import.meta.url), 'utf8');
  assert.match(src, /current \? computeCps\(current\.text, current\.startMs, current\.endMs\) : event\.cps/);
  assert.match(src, /<CpsReadout cps=\{liveCps\}/);
  assert.match(src, /onBlur=\{onFlush\}/);
  assert.match(src, /onDraftChange\(\{ id: event\.id, startMs: timing\.startMs/);
  assert.doesNotMatch(src, /useQuery|useMutation|axios|fetch\(/);
});
