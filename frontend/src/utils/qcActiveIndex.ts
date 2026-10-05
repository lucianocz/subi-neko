/**
 * Active-event lookup for the Final QC page.
 *
 * Events are sorted by `start_ms` (the backend order). `prefixMaxEnd[i]` is the
 * largest end among events 0..i, so walking back from the last event that has
 * started, the walk can stop as soon as `prefixMaxEnd <= t`: nothing earlier can
 * still be running. Cost: O(log n + overlap neighbourhood) per lookup — never a
 * full scan, which matters with 10k+ sign events and a lookup per video frame.
 *
 * Pure (no DOM/React) so it can be tested with `node --test`.
 */

export interface TimedEvent {
  start_ms: number;
  end_ms: number;
  is_hidden?: boolean;
}

export interface ActiveIndex {
  starts: Float64Array;
  /** Effective end; `-Infinity` for events that can never be active (hidden). */
  ends: Float64Array;
  prefixMaxEnd: Float64Array;
}

export function buildActiveIndex(events: readonly TimedEvent[]): ActiveIndex {
  const n = events.length;
  const starts = new Float64Array(n);
  const ends = new Float64Array(n);
  const prefixMaxEnd = new Float64Array(n);
  let max = -Infinity;
  for (let i = 0; i < n; i++) {
    const e = events[i];
    starts[i] = e.start_ms;
    // Hidden events are not in the translated preview, so they are never active.
    ends[i] = e.is_hidden ? -Infinity : e.end_ms;
    if (ends[i] > max) max = ends[i];
    prefixMaxEnd[i] = max;
  }
  return { starts, ends, prefixMaxEnd };
}

/** Index of the last event with `start <= t`, or -1. */
function lastStartedIndex(starts: Float64Array, t: number): number {
  let lo = 0;
  let hi = starts.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    if (starts[mid] <= t) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

/**
 * Indices (ascending, i.e. by start time) of events with `start <= t < end`.
 * Zero-length events are never active.
 */
export function findActiveIndices(index: ActiveIndex, tMs: number): number[] {
  const out: number[] = [];
  const { ends, prefixMaxEnd, starts } = index;
  for (let i = lastStartedIndex(starts, tMs); i >= 0 && prefixMaxEnd[i] > tMs; i--) {
    if (ends[i] > tMs) out.push(i);
  }
  return out.reverse();
}

export function sameNumberList(a: readonly number[], b: readonly number[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
