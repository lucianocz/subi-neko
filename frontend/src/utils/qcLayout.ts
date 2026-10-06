/** Persisted Final QC splitter positions (both splitters share one JSON object). */
export const QC_LAYOUT_KEY = 'subi-neko.qc.layout.v1';

export type LayoutKey = 'h' | 'v';

/** One stored fraction (0 < f < 1) out of the raw layout JSON, or null. */
export function parseStoredFraction(raw: string | null | undefined, key: LayoutKey): number | null {
  if (!raw) return null;
  try {
    const value = (JSON.parse(raw) as Partial<Record<LayoutKey, unknown>> | null)?.[key];
    return typeof value === 'number' && value > 0 && value < 1 ? value : null;
  } catch {
    return null;
  }
}

/** The new raw layout JSON with `key` set, keeping the other splitter's value. */
export function mergeStoredFraction(raw: string | null | undefined, key: LayoutKey, value: number): string {
  let all: Record<string, unknown> = {};
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      all = parsed as Record<string, unknown>;
    }
  } catch {
    // corrupt value: start over
  }
  return JSON.stringify({ ...all, [key]: value });
}
