import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  formatGap, identityLabel, neighbourGaps, parseTimeInput, roundToPrecision, unresolvedSeverityCounts,
} from '../src/utils/qcDetail.ts';
import { canRestoreAi } from '../src/utils/qcQa.ts';
import { setBoundary } from '../src/utils/qcTime.ts';

const issue = (severity: string, is_resolved = false) => ({ severity, is_resolved });

test('header counts only unresolved issues, omit zero groups, most severe first', () => {
  const issues = [issue('info'), issue('blocker', true), issue('warning'), issue('warning'), issue('high'), issue('low', true)];
  assert.deepEqual(unresolvedSeverityCounts(issues), [
    { severity: 'blocker', count: 1 },
    { severity: 'warning', count: 2 },
    { severity: 'info', count: 1 },
  ]);
  assert.deepEqual(unresolvedSeverityCounts([issue('blocker', true), issue('info', true)]), []);
  assert.deepEqual(unresolvedSeverityCounts([]), []);
});

test('issues tab total is the full list (resolved included), unchanged by resolving', () => {
  const issues = [issue('info'), issue('blocker', true), issue('warning')];
  assert.equal(issues.length, 3);
  assert.equal(unresolvedSeverityCounts(issues).reduce((n, g) => n + g.count, 0), 2);
});

test('identity label: character, speaker and fallbacks', () => {
  assert.equal(identityLabel('Leon Fou Bartfort', 'LEON'), 'Leon Fou Bartfort (LEON)');
  assert.equal(identityLabel('Olivia', 'OLIVIA'), 'Olivia');
  assert.equal(identityLabel(null, 'LUCAS'), 'LUCAS');
  assert.equal(identityLabel('Mia', null), 'Mia');
  assert.equal(identityLabel('  ', ''), '—');
  assert.equal(identityLabel(null, undefined), '—');
});

test('strict time parsing refuses malformed and sub-10ms input', () => {
  assert.deepEqual(parseTimeInput('00:01:02.340'), { ok: true, ms: 62_340 });
  assert.deepEqual(parseTimeInput('1:02.5'), { ok: true, ms: 62_500 });
  assert.equal(parseTimeInput('00:01:02.345').ok, false); // would be silently re-quantized
  assert.equal(parseTimeInput('nope').ok, false);
  assert.equal(parseTimeInput('').ok, false);
  assert.equal(parseTimeInput('00:61:00.000').ok, false);
});

test('step results stay on the 10 ms grid and cannot go negative / invert', () => {
  assert.equal(roundToPrecision(1234), 1230);
  assert.equal(roundToPrecision(1235), 1240);
  const t = { startMs: 1000, endMs: 2000 };
  assert.equal(setBoundary(t, 'start', roundToPrecision(1000 - 1500)).ok, false);
  assert.equal(setBoundary(t, 'start', roundToPrecision(1000 + 50)).ok, true);
  assert.equal(setBoundary(t, 'end', roundToPrecision(1000 + 0)).ok, false);
  // custom step
  assert.deepEqual(setBoundary(t, 'end', 2000 + 250), { ok: true, timing: { startMs: 1000, endMs: 2250 } });
});

const ev = (id: number, line_index: number, start_ms: number, end_ms: number, is_hidden = false) =>
  ({ id, line_index, start_ms, end_ms, is_hidden });

test('neighbour gaps use chronological order and the edited timing', () => {
  const events = [ev(1, 5, 0, 1000), ev(2, 1, 2000, 3000), ev(3, 2, 3500, 4000)];
  const self = { id: 2, line_index: 1 };
  assert.deepEqual(neighbourGaps(events, self, { startMs: 2000, endMs: 3000 }), { previousMs: 1000, nextMs: 500 });
  // overlap with the next event while editing
  assert.deepEqual(neighbourGaps(events, self, { startMs: 2000, endMs: 3700 }), { previousMs: 1000, nextMs: -200 });
  // retimed past event 3: neighbours change (3 is now before, nothing after)
  assert.deepEqual(neighbourGaps(events, self, { startMs: 4500, endMs: 5000 }), { previousMs: 500, nextMs: null });
  // first / last events have no neighbour on one side
  assert.deepEqual(neighbourGaps(events, { id: 1, line_index: 5 }, { startMs: 0, endMs: 1000 }), { previousMs: null, nextMs: 1000 });
});

test('identical start times resolve by line_index, deterministically', () => {
  const events = [ev(1, 1, 1000, 1500), ev(2, 2, 1000, 2000), ev(3, 3, 1000, 1800)];
  assert.deepEqual(neighbourGaps(events, { id: 2, line_index: 2 }, { startMs: 1000, endMs: 2000 }), { previousMs: -500, nextMs: -1000 });
});

test('gap formatting and overlap flag', () => {
  assert.deepEqual(formatGap('Previous', 420), { text: 'Previous gap: 420 ms', overlap: false });
  assert.deepEqual(formatGap('Next', 1200), { text: 'Next gap: 1.2 s', overlap: false });
  assert.deepEqual(formatGap('Next', 0), { text: 'Next gap: 0 ms', overlap: false });
  assert.deepEqual(formatGap('Previous', -150), { text: 'Previous overlap: 150 ms', overlap: true });
  assert.deepEqual(formatGap('Next', null), { text: 'Next gap: —', overlap: false });
});

test('duration is derived from the edited timing', () => {
  const t = setBoundary({ startMs: 1000, endMs: 2000 }, 'end', 3500);
  assert.ok(t.ok);
  assert.equal(((t.timing.endMs - t.timing.startMs) / 1000).toFixed(2), '2.50');
});

test('restore-AI availability', () => {
  assert.equal(canRestoreAi(null, 'x'), false);
  assert.equal(canRestoreAi('ai', 'ai'), false);
  assert.equal(canRestoreAi('ai', 'edited'), true);
});

test('detail layout: no tabs, 3fr/2fr grid, CPS beside Duration, two-row issues', () => {
  const src = readFileSync(new URL('../src/components/qc/QcEventDetail.tsx', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/components/qc/qc.css', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /Tabs/);
  assert.match(css, /minmax\(0, 3fr\) minmax\(0, 2fr\)/);
  assert.match(src, /'Restore event' : 'Hide event'/);
  assert.match(src, /aria-label="Restore AI translation"/);
  assert.match(src, /readOnly aria-label="Duration"/);
  assert.match(src, /\{cps\}\s*<\/Group>/); // CPS right after Duration in the timing row
  assert.doesNotMatch(src, /line-clamp|lineClamp|truncate/);
  // issue code and Resolve share the first row, the description is its own full-width row
  assert.match(src, /justify="space-between"[\s\S]*qc-resolve-issue[\s\S]*qc-issue-text/);
});
