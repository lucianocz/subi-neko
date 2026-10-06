import type { QaIssue } from '../types';
import type { QcEvent, QcEventDetail } from '../types/qc';

// Same order the backend sorts by (`_severity_rank`): lower = more severe.
const RANK: Record<string, number> = {
  blocker: 0, critical: 0, error: 0, high: 0, warning: 1, medium: 1, info: 2, low: 2,
};
const rank = (severity: string) => RANK[severity.toLowerCase()] ?? 3;

export interface IssueSummary {
  count: number;
  severity: string | null;
}

/** Unresolved count + most severe unresolved severity (the list row's badge). */
export function summarizeIssues(issues: readonly Pick<QaIssue, 'severity' | 'is_resolved'>[]): IssueSummary {
  let count = 0;
  let severity: string | null = null;
  for (const issue of issues) {
    if (issue.is_resolved) continue;
    count += 1;
    if (severity === null || rank(issue.severity) < rank(severity)) severity = issue.severity;
  }
  return { count, severity };
}

/** Mark one issue resolved on a cached detail and re-derive its counters. */
export function applyIssueResolved(detail: QcEventDetail, issueId: number): QcEventDetail {
  const issues = detail.issues.map((i) => (i.id === issueId ? { ...i, is_resolved: true } : i));
  const { count, severity } = summarizeIssues(issues);
  return { ...detail, issues, issue_count: count, max_issue_severity: severity };
}

/** Same counters on a compact list row (same object back when nothing changed). */
export function applyIssueSummary(event: QcEvent, summary: IssueSummary): QcEvent {
  if (event.issue_count === summary.count && event.max_issue_severity === summary.severity) return event;
  return { ...event, issue_count: summary.count, max_issue_severity: summary.severity };
}

/**
 * Fold an authoritative mutation response (a compact row, e.g. from restore-ai)
 * into the cached detail, keeping the detail-only fields and the issue list
 * (which the mutation does not change).
 */
export function mergeEventIntoDetail(detail: QcEventDetail, saved: QcEvent): QcEventDetail {
  return { ...detail, ...saved, issues: detail.issues };
}

/**
 * Whether "Restore AI translation" has something to do: there is an AI baseline
 * and what the editor currently shows (draft included) differs from it.
 */
export function canRestoreAi(baseline: string | null | undefined, shownText: string | null | undefined): boolean {
  return baseline != null && baseline !== (shownText ?? '');
}
