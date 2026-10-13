import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { followScrollTarget, FOLLOW_BOTTOM_MARGIN } from '../src/utils/qcFollowScroll.ts';

const RH = 60;
const VH = 600; // 10 rows; margin 132 px
const TOTAL = 1000 * RH;
const run = (indices: number[], scrollTop: number, over: Partial<{ viewportHeight: number; totalHeight: number }> = {}) =>
  followScrollTarget({ indices, scrollTop, viewportHeight: VH, rowHeight: RH, totalHeight: TOTAL, ...over });

test('comfortably visible active rows do not scroll', () => {
  assert.equal(run([3], 0), null);
  assert.equal(run([2, 4], 0), null); // bottom 300 + 132 < 600
});

test('scrolls before the active row reaches the bottom edge (margin ≈ 22% of viewport)', () => {
  assert.equal(FOLLOW_BOTTOM_MARGIN, 0.22);
  // Row 7 ends at 480; 480 + 132 = 612 > 600 → scroll even though it is fully visible.
  assert.notEqual(run([7], 0), null);
  // Row 6 ends at 420 (+132 = 552 ≤ 600): still fine.
  assert.equal(run([6], 0), null);
});

test('after a follow scroll the active span sits clear of the margin and does not re-trigger', () => {
  for (const idx of [[7], [12], [40, 41], [100, 103]]) {
    const t = run(idx, 0)!;
    assert.notEqual(t, null);
    assert.equal(run(idx, t), null, `loop for ${idx}`);
  }
});

test('lands the span in the top quarter, so the next scroll is a few events away', () => {
  assert.equal(run([20], 0), 20 * RH - 150); // 25% of 600
});

test('overlapping events: the lowest active row decides, not the first or the selected one', () => {
  assert.equal(run([3, 4], 0), null);
  assert.notEqual(run([3, 7], 0), null); // same first row, but an overlap reaching row 7
  assert.equal(run([7, 3], 0), run([3, 7], 0)); // order of indices is irrelevant
});

test('active row below the viewport (virtualized, unmounted) is reached by arithmetic alone', () => {
  const t = run([500], 0)!;
  assert.equal(t, 500 * RH - 150);
  assert.equal(run([500], t), null);
});

test('backward seek / replay above the viewport scrolls up', () => {
  const t = run([5], 3000)!;
  assert.equal(t, 5 * RH - 150);
  assert.equal(run([5], t), null);
  assert.equal(run([0], 3000), 0); // clamped at the top
});

test('span partly above the viewport counts as out of view', () => {
  assert.notEqual(run([49, 51], 3000), null); // row 49 starts at 2940 < 3000
});

test('tall active group: aligns the top, then stays put (no impossible fitting)', () => {
  const group = Array.from({ length: 15 }, (_, k) => 100 + k); // 900 px > 600
  const t = run(group, 0)!;
  assert.equal(t, 100 * RH);
  assert.equal(run(group, t), null);
  assert.equal(run(group, t - 120), null); // top still visible: leave it alone
  assert.equal(run(group, t + 200), t); // top left the viewport: bring it back
});

test('end of list: nothing to scroll to, clamps to the maximum scroll', () => {
  const total = 20 * RH; // maxScroll 600
  assert.equal(run([19], 600, { totalHeight: total }), null);
  assert.equal(run([19], 0, { totalHeight: total }), 600);
});

test('short list that fits the viewport never scrolls', () => {
  assert.equal(run([8], 0, { totalHeight: 9 * RH }), null);
});

test('degenerate input is a no-op', () => {
  assert.equal(run([], 0), null);
  assert.equal(run([3], 0, { viewportHeight: 0 }), null);
});

test('tiny viewport (about one row) still makes progress without looping', () => {
  const input = { indices: [10], viewportHeight: 70, rowHeight: RH, totalHeight: TOTAL };
  const t = followScrollTarget({ ...input, scrollTop: 0 })!;
  assert.notEqual(t, null);
  assert.equal(followScrollTarget({ ...input, scrollTop: t }), null);
});

test('list wiring: no user-scroll handlers, Follow only via its toggle, no per-row DOM reads', () => {
  const list = readFileSync(new URL('../src/components/qc/QcEventList.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(list, /onUserScroll|onWheel|onTouchMove|onScroll|SCROLL_KEYS/);
  assert.doesNotMatch(list, /getBoundingClientRect|offsetTop|querySelector|ResizeObserver|IntersectionObserver/);
  assert.match(list, /followScrollTarget\(/);
  assert.match(list, /virtualizer\.scrollToOffset\(target\)/);
  const page = readFileSync(new URL('../src/pages/FinalQcPage.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(page, /handleUserScroll|onUserScroll/);
  assert.equal((page.match(/setFollow\(/g) ?? []).length, 1);
  assert.match(page, /onChange=\{\(e\) => setFollow\(e\.currentTarget\.checked\)\}/);
});
