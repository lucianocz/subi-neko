const collator = new Intl.Collator(undefined, { sensitivity: 'base', numeric: true });

/**
 * Glossary order: Original Term (`source_term`) ascending, locale-aware and
 * case-insensitive; ties fall back to the translation, then id, so the order is
 * deterministic. Returns a new array — query-cache data is never mutated.
 */
export function sortGlossaryTerms<T extends { id: number; source_term: string; target_term: string }>(
  terms: readonly T[],
): T[] {
  return [...terms].sort((a, b) =>
    collator.compare(a.source_term, b.source_term)
    || collator.compare(a.target_term, b.target_term)
    || a.id - b.id);
}
