import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { filterIssues, sortIssues } from '../src/utils/editorIssues.ts';
import type { QaIssue } from '../src/types/index.ts';

const issue = (id: number, severity: string, is_resolved = false) =>
  ({ id, severity, is_resolved, qa_type: 'x', message: 'm' }) as unknown as QaIssue;

const all = [issue(1, 'info'), issue(2, 'warning'), issue(3, 'blocker'), issue(4, 'warning', true)];

test('every severity stays visible; only resolved issues are togglable', () => {
  assert.deepEqual(filterIssues(all, false).map((i) => i.id), [1, 2, 3]);
  assert.deepEqual(filterIssues(all, true).map((i) => i.id), [1, 2, 3, 4]);
});

test('issues sort unresolved first, then by severity', () => {
  assert.deepEqual(sortIssues(all).map((i) => i.id), [3, 2, 1, 4]);
});

// No DOM harness in this project (node --test), so the removals are guarded at
// source level: the Info checkbox and Review Queue key handlers must stay gone.
test('Info filter and Review Queue shortcuts are gone', () => {
  const editor = readFileSync(new URL('../src/pages/SubtitleEditorDialog.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(editor, /showInfo|label="Info"/);
  const queue = readFileSync(new URL('../src/pages/ReviewQueueDialog.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(queue, /onKeyDown|handleKeyDown|<Kbd|addEventListener/);
});
