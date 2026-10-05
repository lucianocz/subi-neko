import type { QcEvent } from '../types/qc.ts';
import { MIN_DURATION_MS } from './qcTime.ts';

/** Editable fields of an existing event, as the user currently sees them. */
export interface EventDraft {
  id: number;
  text: string;
  startMs: number;
  endMs: number;
}

export interface NewEventDraft {
  text: string;
  startMs: number;
  endMs: number;
  style: string;
  speaker: string;
}

export interface EventPatch {
  translated_text?: string;
  start_ms?: number;
  end_ms?: number;
}

export const draftFromEvent = (e: QcEvent): EventDraft => ({
  id: e.id, text: e.translated_text ?? '', startMs: e.start_ms, endMs: e.end_ms,
});

/** Only the fields that differ from the persisted event (empty = clean). */
export function diffDraft(draft: EventDraft, event: QcEvent): EventPatch {
  const patch: EventPatch = {};
  if (draft.text !== (event.translated_text ?? '')) patch.translated_text = draft.text;
  if (draft.startMs !== event.start_ms) patch.start_ms = draft.startMs;
  if (draft.endMs !== event.end_ms) patch.end_ms = draft.endMs;
  return patch;
}

export const isDirty = (draft: EventDraft | null, event: QcEvent | undefined): boolean =>
  draft !== null && event !== undefined && draft.id === event.id
  && Object.keys(diffDraft(draft, event)).length > 0;

/**
 * After a save: take the server's value for each field the user has not changed
 * since the request left (a field edited meanwhile keeps the newer typing).
 */
export function mergeSaved(current: EventDraft, sent: EventPatch, saved: QcEvent): EventDraft {
  return {
    id: current.id,
    text: sent.translated_text !== undefined && current.text === sent.translated_text
      ? saved.translated_text ?? '' : current.text,
    startMs: sent.start_ms !== undefined && current.startMs === sent.start_ms ? saved.start_ms : current.startMs,
    endMs: sent.end_ms !== undefined && current.endMs === sent.end_ms ? saved.end_ms : current.endMs,
  };
}

/** Error messages (empty = valid) for a manual-event draft, before any POST. */
export function validateNewEvent(d: NewEventDraft, styles: readonly string[]): string[] {
  const errors: string[] = [];
  if (!Number.isFinite(d.startMs) || d.startMs < 0) errors.push('Start is required.');
  if (!Number.isFinite(d.endMs) || d.endMs < 0) errors.push('End is required.');
  if (errors.length === 0 && d.endMs - d.startMs < MIN_DURATION_MS) errors.push('Start must be before end.');
  if (d.text.trim() === '') errors.push('Text is required.');
  if (d.style === '' || !styles.includes(d.style)) errors.push('Choose a style.');
  return errors;
}

/** Nothing the user would lose by discarding the draft. */
export const isUntouchedNewEvent = (d: NewEventDraft, initial: NewEventDraft): boolean =>
  d.text.trim() === '' && d.speaker.trim() === ''
  && d.startMs === initial.startMs && d.endMs === initial.endMs && d.style === initial.style;

/** Default style: an obvious `Default` one, otherwise none (explicit choice). */
export function defaultStyle(styles: readonly string[]): string {
  return styles.find((s) => s.toLowerCase() === 'default') ?? '';
}
