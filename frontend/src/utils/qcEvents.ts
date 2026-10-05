/**
 * Local, in-memory updates of the QC event list after a successful mutation, so a
 * single edit never refetches the multi-MB list. Pure (no DOM/React).
 */
export interface SortableEvent {
  id: number;
  line_index: number;
  start_ms: number;
  is_hidden: boolean;
}

/** Backend order: (start_ms, line_index). */
export function compareEvents(a: SortableEvent, b: SortableEvent): number {
  return a.start_ms - b.start_ms || a.line_index - b.line_index;
}

/** Index where `event` belongs in the sorted `events` (after equal keys). */
export function insertionIndex<T extends SortableEvent>(events: readonly T[], event: T): number {
  let lo = 0;
  let hi = events.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (compareEvents(events[mid], event) <= 0) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Replace/insert `updated` (matched by id): the row is removed and re-inserted by
 * binary search. When hidden events are not part of the list (`includeHidden`
 * false) a hidden `updated` is dropped.
 */
export function upsertEvent<T extends SortableEvent>(events: readonly T[], updated: T, includeHidden: boolean): T[] {
  const without = events.filter((e) => e.id !== updated.id);
  if (updated.is_hidden && !includeHidden) return without;
  without.splice(insertionIndex(without, updated), 0, updated);
  return without;
}

export interface ListMeta {
  event_count: number;
  total_count: number;
  hidden_count: number;
}

/**
 * New list counters. `previous` is the event as it was in this list (or
 * `undefined`: absent because hidden-and-excluded, or because it is new).
 */
export function nextMeta(
  meta: ListMeta,
  events: readonly unknown[],
  previous: { is_hidden: boolean } | undefined,
  updated: { is_hidden: boolean },
  created: boolean,
): ListMeta {
  const wasHidden = created ? false : previous ? previous.is_hidden : true;
  return {
    event_count: events.length,
    total_count: meta.total_count + (created ? 1 : 0),
    hidden_count: Math.max(0, meta.hidden_count + (updated.is_hidden ? 1 : 0) - (wasHidden ? 1 : 0)),
  };
}

/** Nearest remaining neighbour of position `index` once that row is gone. */
export function neighbourAfterRemoval<T>(events: readonly T[], index: number): T | null {
  return events[index + 1] ?? events[index - 1] ?? null;
}
