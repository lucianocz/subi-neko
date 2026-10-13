import type { FileMetrics } from '../hooks/useProjectMetrics';

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
export function summarizeMetrics(rows: readonly FileMetrics[]): MetricsSummary {
  return {
    events_total: avg(rows.map((m) => m.events_total)),
    edit_distance_norm: avg(rows.map((m) => m.edit_distance_norm)),
    events_user_edited: avg(rows.map((m) => m.events_user_edited)),
    polish_churn_norm: avg(rows.map((m) => m.polish_churn_norm)),
    qa_blockers: avg(rows.map((m) => m.qa_blockers)),
    qa_warnings: avg(rows.map((m) => m.qa_warnings)),
    qa_info: avg(rows.map((m) => m.qa_info)),
    mean_confidence: avg(rows.map((m) => m.mean_confidence)),
    llm_cost_usd: rows.reduce((s, m) => s + (m.llm_cost_usd ?? 0), 0),
    tokens: rows.reduce((s, m) => s + m.prompt_tokens + m.completion_tokens, 0),
  };
}
