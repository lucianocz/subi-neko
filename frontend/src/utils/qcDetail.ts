/** Pure view-logic of the Final QC detail panel (no DOM/React). */
import { compareEvents } from './qcEvents.ts';
import { parseTimeField } from './qcTime.ts';

// -- QA severity indicators ------------------------------------------------------

export type SeverityBucket = 'blocker' | 'warning' | 'info';
export const SEVERITY_BUCKETS: readonly SeverityBucket[] = ['blocker', 'warning', 'info'];

const BUCKET: Record<string, SeverityBucket> = {
  blocker: 'blocker', critical: 'blocker', error: 'blocker', high: 'blocker',
  warning: 'warning', medium: 'warning',
  info: 'info', low: 'info',
};

/** Legacy severity names fold into the one blocker|warning|info scale. */
export const severityBucket = (severity: string): SeverityBucket => BUCKET[severity.toLowerCase()] ?? 'info';

/** Unresolved issues per severity bucket, most severe first, zero groups omitted. */
export function unresolvedSeverityCounts(
  issues: readonly { severity: string; is_resolved: boolean }[],
): { severity: SeverityBucket; count: number }[] {
  const counts: Record<SeverityBucket, number> = { blocker: 0, warning: 0, info: 0 };
  for (const issue of issues) if (!issue.is_resolved) counts[severityBucket(issue.severity)] += 1;
  return SEVERITY_BUCKETS.filter((s) => counts[s] > 0).map((s) => ({ severity: s, count: counts[s] }));
}

// -- Character / speaker / gender -------------------------------------------------

/**
 * `Character (SPEAKER)` / `Character` / `SPEAKER` / `—`. The speaker is dropped
 * when it only repeats the character name (case-insensitive).
 */
export function identityLabel(characterName: string | null | undefined, speaker: string | null | undefined): string {
  const c = characterName?.trim() || '';
  const s = speaker?.trim() || '';
  if (c && s && c.toLowerCase() !== s.toLowerCase()) return `${c} (${s})`;
  return c || s || '—';
}

// -- Neighbour gaps ---------------------------------------------------------------

interface TimedEvent {
  id: number;
  line_index: number;
  start_ms: number;
  end_ms: number;
  is_hidden: boolean;
}

export interface NeighbourGaps {
  /** `start - previous.end`; null without a previous event. Negative = overlap. */
  previousMs: number | null;
  /** `next.start - end`; null without a next event. Negative = overlap. */
  nextMs: number | null;
}

/**
 * Gaps to the chronological neighbours of an event at the given (possibly
 * mid-edit) timing. The event is placed by the same (start_ms, line_index) order
 * the list uses, so a retimed event is compared against its new neighbours and
 * identical timestamps resolve by line_index. `events` is the loaded list, so
 * hidden events only count when the list shows them.
 */
export function neighbourGaps(
  events: readonly TimedEvent[],
  self: { id: number; line_index: number },
  timing: { startMs: number; endMs: number },
): NeighbourGaps {
  const probe = { id: self.id, line_index: self.line_index, start_ms: timing.startMs, is_hidden: false };
  let prev: TimedEvent | null = null;
  let next: TimedEvent | null = null;
  for (const e of events) {
    if (e.id === self.id) continue;
    if (compareEvents(e, probe) < 0) {
      if (prev === null || compareEvents(e, prev) > 0) prev = e;
    } else if (next === null || compareEvents(e, next) < 0) next = e;
  }
  return {
    previousMs: prev ? timing.startMs - prev.end_ms : null,
    nextMs: next ? next.start_ms - timing.endMs : null,
  };
}

export interface GapText {
  text: string;
  overlap: boolean;
}

function compactDuration(ms: number): string {
  return ms >= 1000 ? `${Number((ms / 1000).toFixed(2))} s` : `${ms} ms`;
}

/** `Previous gap: 420 ms` / `Next gap: 1.2 s` / `Previous overlap: 150 ms` / `Next gap: —`. */
export function formatGap(kind: 'Previous' | 'Next', gapMs: number | null): GapText {
  if (gapMs === null) return { text: `${kind} gap: —`, overlap: false };
  if (gapMs < 0) return { text: `${kind} overlap: ${compactDuration(-gapMs)}`, overlap: true };
  return { text: `${kind} gap: ${compactDuration(gapMs)}`, overlap: false };
}

// -- Time input -------------------------------------------------------------------

/** The backend stores timing with 10 ms precision. */
export const TIME_PRECISION_MS = 10;
export const DEFAULT_TIME_STEP_MS = 100;

export const roundToPrecision = (ms: number): number => Math.round(ms / TIME_PRECISION_MS) * TIME_PRECISION_MS;

export type TimeParse = { ok: true; ms: number } | { ok: false; reason: string };

/**
 * Typed time → ms. Never "fixes" input: a malformed string or one finer than the
 * backend's 10 ms precision is rejected instead of being rounded to another time.
 */
export function parseTimeInput(text: string): TimeParse {
  const ms = parseTimeField(text);
  if (ms === null) return { ok: false, reason: 'Use HH:MM:SS.mmm' };
  if (ms % TIME_PRECISION_MS !== 0) return { ok: false, reason: `Precision is ${TIME_PRECISION_MS} ms` };
  return { ok: true, ms };
}
