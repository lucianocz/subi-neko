/** Persisted Final QC video volume (per browser, not per file). */
export const QC_PLAYER_KEY = 'subi-neko.qc.player.v1';

export interface PlayerPrefs {
  /** 0..1 */
  volume: number;
  muted: boolean;
}

/** Parse a stored value; anything malformed yields `null` (= leave the player alone). */
export function parsePlayerPrefs(raw: string | null | undefined): PlayerPrefs | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const { volume, muted } = value as Record<string, unknown>;
    if (typeof volume !== 'number' || !Number.isFinite(volume) || volume < 0 || volume > 1) return null;
    return { volume, muted: muted === true };
  } catch {
    return null;
  }
}

export function serializePlayerPrefs(prefs: PlayerPrefs): string {
  return JSON.stringify({ volume: prefs.volume, muted: prefs.muted });
}

export function readPlayerPrefs(): PlayerPrefs | null {
  try {
    return parsePlayerPrefs(localStorage.getItem(QC_PLAYER_KEY));
  } catch {
    return null;
  }
}

export function writePlayerPrefs(prefs: PlayerPrefs): void {
  try {
    localStorage.setItem(QC_PLAYER_KEY, serializePlayerPrefs(prefs));
  } catch {
    // Convenience only (private mode, blocked storage, ...).
  }
}
