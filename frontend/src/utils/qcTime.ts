/** Time-field helpers for the QC editor. Pure; the backend quantizes to 10 ms. */

/** `HH:MM:SS.mmm` — the editable form of a time. */
export function formatTimeField(ms: number): string {
  const abs = Math.max(0, Math.round(ms));
  const h = Math.floor(abs / 3_600_000);
  const m = Math.floor(abs / 60_000) % 60;
  const s = Math.floor(abs / 1000) % 60;
  const milli = abs % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(milli, 3)}`;
}

/**
 * Accepts `h:mm:ss.mmm`, `m:ss.mmm`, `s.mmm` and `s` (fraction optional, 1-3 digits
 * meaning a decimal fraction: `.5` = 500 ms). Returns whole milliseconds or null.
 */
export function parseTimeField(input: string): number | null {
  const m = /^\s*(?:(?:(\d+):)?(\d{1,2}):)?(\d{1,2})(?:[.,](\d{1,3}))?\s*$/.exec(input);
  if (!m) return null;
  const [, h, min, sec, frac] = m;
  const s = Number(sec);
  const mins = min === undefined ? 0 : Number(min);
  if (s > 59 || mins > 59) return null;
  const milli = frac === undefined ? 0 : Number(frac.padEnd(3, '0'));
  return (Number(h ?? 0) * 3600 + mins * 60 + s) * 1000 + milli;
}

export const MIN_DURATION_MS = 10;

export interface Timing {
  startMs: number;
  endMs: number;
}

export type TimingResult = { ok: true; timing: Timing } | { ok: false; reason: string };

const invalid = (reason: string): TimingResult => ({ ok: false, reason });

/** Whole-ms position of the player clock. */
export const videoTimeToMs = (seconds: number): number => Math.round(seconds * 1000);

export function setBoundary(timing: Timing, which: 'start' | 'end', ms: number): TimingResult {
  if (!Number.isFinite(ms) || ms < 0) return invalid('Time cannot be negative.');
  const next = which === 'start' ? { ...timing, startMs: ms } : { ...timing, endMs: ms };
  if (next.endMs - next.startMs < MIN_DURATION_MS) return invalid('Start must be before end.');
  return { ok: true, timing: next };
}

export function nudgeBoundary(timing: Timing, which: 'start' | 'end', deltaMs: number): TimingResult {
  const current = which === 'start' ? timing.startMs : timing.endMs;
  return setBoundary(timing, which, current + deltaMs);
}

/** Replay lead-in: playback starts this long before the event's Start. */
export const REPLAY_LEAD_IN_MS = 500;

export function replaySeekMs(startMs: number): number {
  return Math.max(0, startMs - REPLAY_LEAD_IN_MS);
}

/**
 * Seek the shared video to `Start − 500 ms` and play normally (no End boundary, no
 * timer, no state). Returns false when there is no video; a rejected `play()` is swallowed.
 */
export function replayFromStart(
  video: Pick<HTMLVideoElement, 'currentTime' | 'play'> | null | undefined,
  startMs: number,
): boolean {
  if (!video || !Number.isFinite(startMs)) return false;
  video.currentTime = replaySeekMs(startMs) / 1000;
  try {
    void Promise.resolve(video.play()).catch(() => { /* autoplay blocked / interrupted: nothing to do */ });
  } catch { /* play() threw synchronously */ }
  return true;
}
