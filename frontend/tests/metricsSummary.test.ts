import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeMetrics, summarizeOtherUsage } from '../src/utils/metricsSummary.ts';
import type { FileMetrics } from '../src/hooks/useProjectMetrics.ts';
import { readFileSync } from 'node:fs';

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

test('Other adds only project-level cost and tokens, without counting episode usage twice', () => {
  const episodes = [m({}), m({ file_id: 2, llm_cost_usd: 2 })];
  const before = structuredClone(episodes);
  const other = summarizeOtherUsage([
    { category: 'Episodes', model: 'model-a', calls: 5, prompt_tokens: 2000, completion_tokens: 1000, cost_usd: 3 },
    { category: 'Other', model: 'model-a', calls: 2, prompt_tokens: 400, completion_tokens: 100, cost_usd: 0.25 },
    { category: 'Other', model: 'model-b', calls: 1, prompt_tokens: 300, completion_tokens: 200, cost_usd: 0.75 },
  ]);
  assert.deepEqual(other, { llm_cost_usd: 1, tokens: 1000 });
  const original = summarizeMetrics(episodes);
  const combined = summarizeMetrics(episodes, other);
  assert.equal(combined.llm_cost_usd, 4);
  assert.equal(combined.tokens, 4000);
  assert.deepEqual({ ...combined, llm_cost_usd: original.llm_cost_usd, tokens: original.tokens }, original);
  assert.deepEqual(episodes, before, 'displayed episode values remain unchanged');
});

test('empty Other adds zero and project-only totals work before any episode metrics exist', () => {
  const empty = summarizeOtherUsage([]);
  assert.deepEqual(empty, { llm_cost_usd: 0, tokens: 0 });
  assert.deepEqual(summarizeMetrics([m({})], empty), summarizeMetrics([m({})]));
  const projectOnly = summarizeMetrics([], { llm_cost_usd: 0.5, tokens: 1500 });
  assert.equal(projectOnly.llm_cost_usd, 0.5);
  assert.equal(projectOnly.tokens, 1500);
  assert.equal(projectOnly.events_total, null);
  assert.equal(projectOnly.mean_confidence, null);
});

test('unpriced Other usage preserves tokens and the existing unknown-cost convention', () => {
  const other = summarizeOtherUsage([
    { category: 'Other', model: 'unpriced', calls: 1, prompt_tokens: 200, completion_tokens: 100, cost_usd: null },
  ]);
  assert.deepEqual(other, { llm_cost_usd: null, tokens: 300 });
  assert.equal(summarizeMetrics([m({})], other).llm_cost_usd, 1);
  assert.equal(summarizeMetrics([m({})], other).tokens, 1800);
});

const dialog = readFileSync(new URL('../src/pages/MetricsDialog.tsx', import.meta.url), 'utf8');

test('one Other row follows episodes and immediately precedes the existing Summary footer', () => {
  assert.equal((dialog.match(/<OtherRow\b/g) ?? []).length, 1);
  assert.match(dialog, /\{metrics\.map\([\s\S]*?<OtherRow other=\{other\} \/>\s*<\/Table\.Tbody>\s*<Table\.Tfoot[^>]*>\s*<SummaryRow rows=\{metrics\} other=\{other\} \/>/);
  assert.doesNotMatch(dialog, /metrics\.length === 0/);
});

test('Other has only its label, cost and combined tokens in the original nine columns', () => {
  const row = dialog.slice(dialog.indexOf('function OtherRow('), dialog.indexOf('function SummaryRow('));
  assert.equal((row.match(/<Table\.Td(?:\s|>)/g) ?? []).length, 9);
  assert.equal((row.match(/<Table\.Td \/>/g) ?? []).length, 6);
  assert.match(row, />Other<\/Text>/);
  assert.match(row, /cost\(other\?\.llm_cost_usd \?\? null\)/);
  assert.match(row, /other\.tokens \/ 1000\)\.toFixed\(0\)/);
  assert.equal((row.match(/ta=\{'end'\}/g) ?? []).length, 2);
});

test('separate LLM usage section, model breakdowns and extra totals are removed', () => {
  assert.equal((dialog.match(/<Table(?:\s|>)/g) ?? []).length, 1);
  assert.equal((dialog.match(/<Table\.Tfoot\b/g) ?? []).length, 1);
  assert.doesNotMatch(dialog, /LLM usage|Category \/ model|Input tokens|Output tokens|recorded attempts|Historical Style Bible|r\.model/);
  assert.match(dialog, /summarizeMetrics\(rows, other\)/);
});
