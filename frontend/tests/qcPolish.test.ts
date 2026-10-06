import test from 'node:test';
import assert from 'node:assert/strict';
import { cpsLimitsFrom, cpsSeverity, NO_CPS_LIMITS } from '../src/utils/cps.ts';
import {
  applyIssueResolved, applyIssueSummary, canRestoreAi, mergeEventIntoDetail, summarizeIssues,
} from '../src/utils/qcQa.ts';
import { parsePlayerPrefs, serializePlayerPrefs } from '../src/utils/qcPlayerPrefs.ts';
import { mergeStoredFraction, parseStoredFraction } from '../src/utils/qcLayout.ts';
import { optionsErrorMessage, validateCpsLimits } from '../src/utils/optionsValidation.ts';

// -- CPS severity ------------------------------------------------------------

const LIMITS = { soft: 18, hard: 20 };

test('cps severity: below / exactly soft is normal', () => {
  assert.equal(cpsSeverity(10, LIMITS), 'normal');
  assert.equal(cpsSeverity(18, LIMITS), 'normal');
  assert.equal(cpsSeverity(0, LIMITS), 'normal');
});

test('cps severity: between soft and hard is a warning, exactly hard included', () => {
  assert.equal(cpsSeverity(18.1, LIMITS), 'warning');
  assert.equal(cpsSeverity(19.9, LIMITS), 'warning');
  assert.equal(cpsSeverity(20, LIMITS), 'warning'); // strict `>` like the backend
});

test('cps severity: above hard is an error', () => {
  assert.equal(cpsSeverity(20.01, LIMITS), 'error');
  assert.equal(cpsSeverity(40, LIMITS), 'error');
});

test('cps severity: missing cps and unloaded limits never highlight', () => {
  assert.equal(cpsSeverity(null, LIMITS), 'normal');
  assert.equal(cpsSeverity(undefined, LIMITS), 'normal');
  assert.equal(cpsSeverity(Number.NaN, LIMITS), 'normal');
  assert.equal(cpsSeverity(500, NO_CPS_LIMITS), 'normal');
});

test('cpsLimitsFrom reads both backend thresholds; a bad/missing soft collapses the band', () => {
  assert.deepEqual(cpsLimitsFrom({ cps_limit: 20, soft_cps_limit: 18 }), { soft: 18, hard: 20 });
  assert.deepEqual(cpsLimitsFrom({ cps_limit: 20 }), { soft: 20, hard: 20 });
  assert.deepEqual(cpsLimitsFrom({ cps_limit: 20, soft_cps_limit: 25 }), { soft: 20, hard: 20 });
  assert.equal(cpsLimitsFrom(undefined), NO_CPS_LIMITS);
  assert.equal(cpsLimitsFrom({}), NO_CPS_LIMITS);
});

test('legacy editor and Final QC share the one cps helper', async () => {
  const { readFileSync } = await import('node:fs');
  for (const file of [
    '../src/pages/SubtitleEditorDialog.tsx',
    '../src/components/qc/QcEventList.tsx',
    '../src/components/qc/QcEventDetail.tsx',
  ]) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.match(source, /from '\.\.\/(\.\.\/)?utils\/cps'/, `${file} must import utils/cps`);
    assert.doesNotMatch(source, /cps\s*>\s*cpsLimit|cps\s*>\s*cps_limit/, `${file} must not re-implement the threshold`);
  }
});

// -- options validation --------------------------------------------------------

test('validateCpsLimits requires soft < hard and positive values', () => {
  assert.deepEqual(validateCpsLimits(18, 20), {});
  assert.match(validateCpsLimits(20, 20).soft ?? '', /lower than the hard/);
  assert.match(validateCpsLimits(25, 20).soft ?? '', /lower than the hard/);
  assert.ok(validateCpsLimits(0, 20).soft);
  assert.ok(validateCpsLimits(18, -1).hard);
  // mid-edit blanks are not errors
  assert.deepEqual(validateCpsLimits('', 20), {});
  assert.deepEqual(validateCpsLimits(18, ''), {});
});

test('optionsErrorMessage prefers the server detail', () => {
  assert.equal(optionsErrorMessage({ response: { data: { detail: 'Soft must be lower.' } } }), 'Soft must be lower.');
  assert.equal(optionsErrorMessage(new Error('Network down')), 'Network down');
  assert.equal(optionsErrorMessage(null), 'Unknown error');
});

// -- QA local state ------------------------------------------------------------

const issue = (id: number, severity: string, is_resolved = false) => ({
  id, severity, qa_type: 'x', message: 'm', details_json: null, is_resolved, resolution_note: null, created_at: '',
});
const detail = (issues: ReturnType<typeof issue>[], extra: Record<string, unknown> = {}) => ({
  id: 7, translated_text: 'a', cps: 5, issue_count: 2, max_issue_severity: 'blocker',
  original_ai_translated_text: 'orig', issues, ...extra,
}) as never;

test('summarizeIssues counts unresolved and picks the most severe', () => {
  assert.deepEqual(summarizeIssues([]), { count: 0, severity: null });
  assert.deepEqual(summarizeIssues([issue(1, 'info'), issue(2, 'blocker'), issue(3, 'warning')]),
    { count: 3, severity: 'blocker' });
  assert.deepEqual(summarizeIssues([issue(1, 'info'), issue(2, 'blocker', true)]),
    { count: 1, severity: 'info' });
});

test('applyIssueResolved keeps the issue, marks it resolved and re-derives counters', () => {
  const d = detail([issue(1, 'blocker'), issue(2, 'warning')]);
  const next = applyIssueResolved(d, 1) as unknown as {
    issues: { id: number; is_resolved: boolean }[]; issue_count: number; max_issue_severity: string | null;
  };
  assert.equal(next.issues.length, 2);                       // resolved issues stay visible
  assert.equal(next.issues[0].is_resolved, true);
  assert.equal(next.issues[1].is_resolved, false);
  assert.equal(next.issue_count, 1);
  assert.equal(next.max_issue_severity, 'warning');          // severity drops with the blocker
  const last = applyIssueResolved(next as never, 2) as unknown as { issue_count: number; max_issue_severity: string | null };
  assert.equal(last.issue_count, 0);
  assert.equal(last.max_issue_severity, null);
});

test('applyIssueSummary returns the same row when nothing changed', () => {
  const row = { id: 1, issue_count: 2, max_issue_severity: 'warning' } as never;
  assert.equal(applyIssueSummary(row, { count: 2, severity: 'warning' }), row);
  const changed = applyIssueSummary(row, { count: 1, severity: 'info' }) as unknown as { issue_count: number };
  assert.equal(changed.issue_count, 1);
});

// -- restore AI local state -------------------------------------------------------

test('canRestoreAi: needs a baseline that differs from what is shown (draft included)', () => {
  assert.equal(canRestoreAi(null, 'x'), false);
  assert.equal(canRestoreAi(undefined, 'x'), false);
  assert.equal(canRestoreAi('ai', 'ai'), false);
  assert.equal(canRestoreAi('ai', 'edited'), true);
  assert.equal(canRestoreAi('ai', null), true);
  assert.equal(canRestoreAi('', 'x'), true);
});

test('mergeEventIntoDetail takes the authoritative row but keeps detail-only fields and issues', () => {
  const d = detail([issue(1, 'info')], { layer: 3, is_locked: false, is_user_edited: true });
  const saved = { id: 7, translated_text: 'orig', cps: 12.5, is_locked: true, is_user_edited: false,
    issue_count: 1, max_issue_severity: 'info' } as never;
  const merged = mergeEventIntoDetail(d, saved) as unknown as Record<string, unknown>;
  assert.equal(merged.translated_text, 'orig');
  assert.equal(merged.cps, 12.5);
  assert.equal(merged.is_locked, true);
  assert.equal(merged.is_user_edited, false);
  assert.equal(merged.layer, 3);
  assert.equal(merged.original_ai_translated_text, 'orig');
  assert.equal((merged.issues as unknown[]).length, 1);
});

// -- player prefs -----------------------------------------------------------------

test('player prefs round-trip', () => {
  assert.deepEqual(parsePlayerPrefs(serializePlayerPrefs({ volume: 0.35, muted: true })), { volume: 0.35, muted: true });
  assert.deepEqual(parsePlayerPrefs('{"volume":0,"muted":false}'), { volume: 0, muted: false });
  assert.deepEqual(parsePlayerPrefs('{"volume":1}'), { volume: 1, muted: false });
});

test('player prefs ignore malformed values', () => {
  for (const bad of [null, undefined, '', 'nope', '[]', '42', 'null', '{}', '{"volume":"0.5"}',
    '{"volume":1.5}', '{"volume":-0.1}', '{"volume":null}', '{"muted":true}']) {
    assert.equal(parsePlayerPrefs(bad), null, String(bad));
  }
});

// -- splitter persistence ------------------------------------------------------------

test('both splitters persist in one object and never clobber each other', () => {
  let raw: string | null = null;
  raw = mergeStoredFraction(raw, 'h', 0.7);
  raw = mergeStoredFraction(raw, 'v', 0.4);
  assert.equal(parseStoredFraction(raw, 'h'), 0.7);
  assert.equal(parseStoredFraction(raw, 'v'), 0.4);
  raw = mergeStoredFraction(raw, 'h', 0.55);
  assert.equal(parseStoredFraction(raw, 'h'), 0.55);
  assert.equal(parseStoredFraction(raw, 'v'), 0.4);
});

test('stored splitter fractions are validated; corrupt storage is replaced', () => {
  assert.equal(parseStoredFraction(null, 'h'), null);
  assert.equal(parseStoredFraction('garbage', 'h'), null);
  assert.equal(parseStoredFraction('{"h":0}', 'h'), null);
  assert.equal(parseStoredFraction('{"h":1}', 'h'), null);
  assert.equal(parseStoredFraction('{"h":"0.5"}', 'h'), null);
  assert.equal(parseStoredFraction('{"v":0.3}', 'h'), null);
  assert.equal(parseStoredFraction(mergeStoredFraction('garbage', 'v', 0.3), 'v'), 0.3);
  assert.equal(parseStoredFraction(mergeStoredFraction('[1,2]', 'v', 0.3), 'v'), 0.3);
});
