import type { QaIssue } from '../types';

export const SEVERITY_RANK: Record<string, number> = {
  blocker: 0,
  critical: 0,
  error: 0,
  high: 0,
  warning: 1,
  medium: 1,
  info: 2,
  low: 2,
};

export function sortIssues(issues: QaIssue[]) {
  return [...issues].sort((a, b) => {
    if (a.is_resolved !== b.is_resolved) return a.is_resolved ? 1 : -1;
    const ar = SEVERITY_RANK[a.severity.toLowerCase()] ?? 99;
    const br = SEVERITY_RANK[b.severity.toLowerCase()] ?? 99;
    if (ar !== br) return ar - br;
    return a.id - b.id;
  });
}

// Every severity (blocker, warning, info, …) is always shown; only resolved
// issues are togglable.
export function filterIssues(issues: QaIssue[], showResolved: boolean) {
  return showResolved ? issues : issues.filter((issue) => !issue.is_resolved);
}
