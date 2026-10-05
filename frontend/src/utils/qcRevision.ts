/**
 * Output-revision bookkeeping for the QC page. Pure.
 *
 * Every changing mutation bumps the project's revision by exactly one, so a
 * mutation response stamped `R` is "ours alone" iff `R <= known + 1`. A larger
 * jump means somebody else changed the output in between.
 */
export type RevisionVerdict = { kind: 'ours'; known: number } | { kind: 'external' };

export function classifyMutationRevision(known: number, responseRevision: number): RevisionVerdict {
  if (!Number.isFinite(responseRevision)) return { kind: 'ours', known };
  if (responseRevision > known + 1) return { kind: 'external' };
  return { kind: 'ours', known: Math.max(known, responseRevision) };
}

/**
 * Latest-wins gate: `begin()` returns a token; only the most recently begun
 * token is `current`. An older async result (slow preview fetch) must check
 * `isCurrent` before it touches the renderer or state.
 */
export function createLatestGate() {
  let latest = 0;
  return {
    begin: () => ++latest,
    isCurrent: (token: number) => token === latest,
  };
}
