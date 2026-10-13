import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeMetrics } from '../src/utils/metricsSummary.ts';
import type { FileMetrics } from '../src/hooks/useProjectMetrics.ts';

const m = (o: Partial<FileMetrics>): FileMetrics => ({
  file_id: 1, filename: 'f', episode_number: null, prompt_version: null, events_total: 100,
  events_user_edited: 10, events_approved: 0, edit_distance_norm: 0.1, polish_churn_norm: 0.2,
  polish_edit_count: 5, qa_blockers: 0, qa_warnings: 2, qa_info: 4, mean_confidence: 0.8,
  mean_confidence_edited: null, llm_cost_usd: 1, prompt_tokens: 1000, completion_tokens: 500,
  created_at: '', ...o,
});

test('averages per-file values, sums cost and tokens', () => {
  const s = summarizeMetrics([m({}), m({ events_total: 200, events_user_edited: 20, qa_warnings: 4,
    edit_distance_norm: 0.3, llm_cost_usd: 0.5, prompt_tokens: 2000, completion_tokens: 0 })]);
  assert.equal(s.events_total, 150);
  assert.equal(s.events_user_edited, 15);
  assert.ok(Math.abs(s.edit_distance_norm! - 0.2) < 1e-9);
  assert.equal(s.qa_warnings, 3);
  assert.equal(s.llm_cost_usd, 1.5);
  assert.equal(s.tokens, 3500);
});

test('null ratios are skipped in averages; all-null stays null', () => {
  const s = summarizeMetrics([m({ edit_distance_norm: null, mean_confidence: null }),
    m({ edit_distance_norm: 0.2, mean_confidence: null, llm_cost_usd: null })]);
  assert.equal(s.edit_distance_norm, 0.2);
  assert.equal(s.mean_confidence, null);
  assert.equal(s.llm_cost_usd, 1);
});
