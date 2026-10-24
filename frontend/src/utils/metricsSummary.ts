import type { FileMetrics } from '../hooks/useProjectMetrics';
import type { LlmUsage } from '../hooks/useProjectMetrics';

export function summarizeUsage(rows: readonly LlmUsage[]) {
  return { calls: rows.reduce((s, r) => s + r.calls, 0),
    prompt_tokens: rows.reduce((s, r) => s + r.prompt_tokens, 0),
    completion_tokens: rows.reduce((s, r) => s + r.completion_tokens, 0),
    cost_usd: rows.some(r => r.cost_usd !== null) ? rows.reduce((s, r) => s + (r.cost_usd ?? 0), 0) : null };
}

export interface OtherMetrics {
  llm_cost_usd: number | null;
  tokens: number;
}

/** Episode-associated calls are already accounted for by the file metrics. */
export function summarizeOtherUsage(rows: readonly LlmUsage[]): OtherMetrics {
  const other = rows.filter(row => row.category === 'Other');
  const usage = summarizeUsage(other);
  return {
    llm_cost_usd: other.length === 0 ? 0 : usage.cost_usd,
    tokens: usage.prompt_tokens + usage.completion_tokens,
  };
}

export interface MetricsSummary {
  events_total: number | null;
  edit_distance_norm: number | null;
  events_user_edited: number | null;
  polish_churn_norm: number | null;
  qa_blockers: number | null;
  qa_warnings: number | null;
  qa_info: number | null;
  mean_confidence: number | null;
  llm_cost_usd: number;
  tokens: number;
}

/** Mean over the non-null values; null when there are none. */
function avg(values: readonly (number | null)[]): number | null {
  const present = values.filter((v): v is number => v !== null);
  return present.length === 0 ? null : present.reduce((a, b) => a + b, 0) / present.length;
}

/** Summary row: averages for ratios/counts per file, sums for cost and tokens. */
export function summarizeMetrics(rows: readonly FileMetrics[], other?: OtherMetrics): MetricsSummary {
  return {
    events_total: avg(rows.map((m) => m.events_total)),
    edit_distance_norm: avg(rows.map((m) => m.edit_distance_norm)),
    events_user_edited: avg(rows.map((m) => m.events_user_edited)),
    polish_churn_norm: avg(rows.map((m) => m.polish_churn_norm)),
    qa_blockers: avg(rows.map((m) => m.qa_blockers)),
    qa_warnings: avg(rows.map((m) => m.qa_warnings)),
    qa_info: avg(rows.map((m) => m.qa_info)),
    mean_confidence: avg(rows.map((m) => m.mean_confidence)),
    llm_cost_usd: rows.reduce((s, m) => s + (m.llm_cost_usd ?? 0), 0) + (other?.llm_cost_usd ?? 0),
    tokens: rows.reduce((s, m) => s + m.prompt_tokens + m.completion_tokens, 0) + (other?.tokens ?? 0),
  };
}
