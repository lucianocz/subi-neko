/**
 * The ONE place CPS thresholds turn into a visual severity, shared by the legacy
 * subtitle editor and Final QC (list row, detail panel). The backend owns the
 * numbers (`soft_cps_limit`, `cps_limit` = hard) and QA only ever uses the hard
 * limit; the soft limit is a display-only readability hint.
 *
 *   cps <= soft          -> normal
 *   soft < cps <= hard   -> warning (orange)
 *   cps > hard           -> error   (red)
 *
 * Comparisons are strict `>`, matching the backend's `cps > cps_limit`: a line
 * at exactly the hard limit is still only a warning.
 */
export type CpsSeverity = 'normal' | 'warning' | 'error';

export interface CpsLimits {
  /** Soft limit (orange above it). */
  soft: number;
  /** Hard limit (red above it; the QA `high_cps` limit). */
  hard: number;
}

/** No highlighting at all (limits not loaded yet). */
export const NO_CPS_LIMITS: CpsLimits = { soft: Number.POSITIVE_INFINITY, hard: Number.POSITIVE_INFINITY };

export function cpsSeverity(cps: number | null | undefined, limits: CpsLimits): CpsSeverity {
  if (cps == null || !Number.isFinite(cps)) return 'normal';
  if (cps > limits.hard) return 'error';
  if (cps > limits.soft) return 'warning';
  return 'normal';
}

/** Mantine colour per severity (`undefined` = keep the surrounding default). */
export const CPS_COLORS: Record<CpsSeverity, string | undefined> = {
  normal: undefined,
  warning: 'orange',
  error: 'red',
};

export function cpsLimitsFrom(
  source: { soft_cps_limit?: number; cps_limit?: number } | null | undefined,
): CpsLimits {
  if (!source || source.cps_limit == null) return NO_CPS_LIMITS;
  const hard = source.cps_limit;
  // Older servers don't send a soft limit: no warning band then.
  const soft = source.soft_cps_limit != null && source.soft_cps_limit < hard ? source.soft_cps_limit : hard;
  return { soft, hard };
}

/**
 * Live (client-side) CPS — a port of `compute_cps` in backend `app/subs/readability.py`,
 * used only to give instant feedback for an unsaved draft. The backend value stays
 * authoritative for persisted data and QA.
 *
 * Visible text = text split on literal `\N`, each row stripped of `{...}` blocks with
 * `\N`/`\n`/`\h` turned into spaces and trimmed, empty rows dropped, rows joined by one
 * space. Length counts code points (like Python `len`), not UTF-16 units.
 */
export function visibleLength(text: string | null | undefined): number {
  if (!text) return 0;
  const rows = text.split('\\N')
    .map((row) => row.replace(/\{[^}]*\}/g, '').replace(/\\[Nnh]/g, ' ').trim())
    .filter((row) => row !== '');
  return Array.from(rows.join(' ')).length;
}

/** Raw characters per second, or null when meaningless (empty text, duration <= 0). */
export function computeCps(text: string | null | undefined, startMs: number, endMs: number): number | null {
  const durationMs = endMs - startMs;
  if (!Number.isFinite(durationMs) || durationMs <= 0) return null;
  const length = visibleLength(text);
  if (length === 0) return null;
  return length / (durationMs / 1000);
}
