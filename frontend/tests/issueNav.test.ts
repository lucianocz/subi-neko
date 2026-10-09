import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  centeredScrollTop,
  findNextIssue,
  findPrevIssue,
  pageForPosition,
  withoutIssueEvent,
} from '../src/utils/issueNav.ts';
import type { IssueTarget } from '../src/utils/issueNav.ts';

const t = (line_index: number, position = line_index): IssueTarget => ({
  event_id: line_index + 100, line_index, position,
});
const index = [t(3), t(12), t(28)];

test('next / previous move strictly from the anchor, in table order', () => {
  assert.equal(findNextIssue(index, null)?.line_index, 3);
  assert.equal(findNextIssue(index, 3)?.line_index, 12);
  assert.equal(findNextIssue(index, 5)?.line_index, 12);
  assert.equal(findPrevIssue(index, 12)?.line_index, 3);
  assert.equal(findPrevIssue(index, 20)?.line_index, 12);
  // repeated clicks never land on the same event
  const seen: number[] = [];
  let anchor: number | null = null;
  for (let n = 0; n < 3; n += 1) {
    const next = findNextIssue(index, anchor);
    assert.ok(next);
    seen.push(next.line_index);
    anchor = next.line_index;
  }
  assert.deepEqual(seen, [3, 12, 28]);
});

test('navigation does not wrap and boundaries yield null (disabled)', () => {
  assert.equal(findNextIssue(index, 28), null);
  assert.equal(findNextIssue(index, 99), null);
  assert.equal(findPrevIssue(index, 3), null);
  assert.equal(findPrevIssue(index, 0), null);
  assert.equal(findPrevIssue(index, null), null);
  assert.equal(findNextIssue([], null), null);
  assert.equal(findPrevIssue([], 5), null);
});

test('anchor on a non-issue event navigates to the neighbours around it', () => {
  assert.equal(findPrevIssue(index, 13)?.line_index, 12);
  assert.equal(findNextIssue(index, 13)?.line_index, 28);
});

test('resolving the last issue of an event removes its single destination', () => {
  const after = withoutIssueEvent(index, 112);
  assert.deepEqual(after.map((x) => x.line_index), [3, 28]);
  // anchor stays on the resolved event: next/prev still step strictly around it
  assert.equal(findNextIssue(after, 12)?.line_index, 28);
  assert.equal(findPrevIssue(after, 12)?.line_index, 3);
  // unknown event: same array instance (no cache churn)
  assert.equal(withoutIssueEvent(index, 1), index);
});

test('target on another page maps position to the right page', () => {
  assert.equal(pageForPosition(0, 1000), 1);
  assert.equal(pageForPosition(999, 1000), 1);
  assert.equal(pageForPosition(1000, 1000), 2);
  assert.equal(pageForPosition(9500, 1000), 10);
});

test('centred scroll accounts for the sticky header and clamps at the top', () => {
  const top = centeredScrollTop({
    scrollTop: 1000, viewportTop: 100, viewportHeight: 600,
    headerHeight: 40, rowTop: 900, rowHeight: 100,
  });
  // row sits at 1800 in content; visible area below header is 560 high
  assert.equal(top, 1800 - 40 - (600 - 40 - 100) / 2);
  assert.equal(centeredScrollTop({
    scrollTop: 0, viewportTop: 0, viewportHeight: 600, headerHeight: 40, rowTop: 50, rowHeight: 60,
  }), 0);
});
