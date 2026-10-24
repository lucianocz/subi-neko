import assert from 'node:assert/strict';
import test from 'node:test';
import { DescriptionRequestGuard, refreshDescription } from '../src/utils/webPublishing.ts';
import { summarizeUsage } from '../src/utils/metricsSummary.ts';

test('refresh updates source and preserves translated or manually edited description', () => {
  const fresh = { original_description: 'New English', description: 'New English' };
  assert.deepEqual(refreshDescription({ original_description: 'Old English', description: 'Překlad' }, fresh),
    { original_description: 'New English', description: 'Překlad' });
  assert.deepEqual(refreshDescription({ original_description: 'Old English', description: 'Old English' }, fresh), fresh);
});

test('TMDB omission preserves both values including approved empty description', () => {
  const before = { original_description: 'English', description: 'Překlad' };
  assert.deepEqual(refreshDescription(before, { original_description: null, description: null }), before);
  assert.equal(refreshDescription({ ...before, description: null },
    { original_description: 'New', description: 'New' }).description, null);
});

test('delayed AI response is discarded after manual edit, metadata change or close', async () => {
  for (const reason of ['manual edit', 'title change', 'refresh', 'close']) {
    const guard = new DescriptionRequestGuard();
    const revision = guard.capture();
    let saved = false;
    let resolve!: () => void;
    const delayed = new Promise<void>(r => { resolve = r; });
    const apply = delayed.then(() => { if (guard.isCurrent(revision)) saved = true; });
    guard.invalidate();
    resolve();
    await apply;
    assert.equal(saved, false, reason);
    assert.equal(guard.isCurrent(guard.capture()), true);
  }
});

test('LLM totals sum disjoint categories and models once, preserving unknown cost', () => {
  const rows = [
    { category: 'Other' as const, model: 'a', calls: 2, prompt_tokens: 10, completion_tokens: 5, cost_usd: 0.1 },
    { category: 'Episodes' as const, model: 'a', calls: 3, prompt_tokens: 20, completion_tokens: 8, cost_usd: 0.2 },
    { category: 'Other' as const, model: 'b', calls: 1, prompt_tokens: 4, completion_tokens: 2, cost_usd: null },
  ];
  const total = summarizeUsage(rows);
  assert.equal(total.calls, 6);
  assert.equal(total.prompt_tokens, 34);
  assert.equal(total.completion_tokens, 15);
  assert.ok(Math.abs(total.cost_usd! - 0.3) < 1e-10);
  assert.equal(summarizeUsage([]).cost_usd, null);
  assert.equal(summarizeUsage(rows.filter(r => r.category === 'Other')).calls, 3);
});
