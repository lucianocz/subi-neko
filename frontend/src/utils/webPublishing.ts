export interface EpisodeChoice { tmdb_episode_id: string; episode_number: number; name_english: string | null }

export function refreshDescription<T extends { original_description: string | null; description: string | null }>(before: T, fresh: T): T {
  const source = fresh.original_description?.trim() ? fresh.original_description : before.original_description;
  return { ...fresh, original_description: source,
    description: before.original_description !== null && before.description === before.original_description
      ? source : before.description };
}

/** An edit, title change, unmount or close invalidates a pending proposal. */
export class DescriptionRequestGuard {
  private revision = 0;
  invalidate() { this.revision++; }
  capture() { return this.revision; }
  isCurrent(revision: number) { return this.revision === revision; }
}
export interface WebMapping { tmdb_episode_id: string | null; episode_number: number; name_english: string | null }
export interface WebFile {
  id: number; filename: string; episode_number: number | null; status: string;
  mapping: WebMapping | null; locked: boolean; published_at: string | null;
  current_sha256: string | null; published_sha256: string | null; render_error: string | null;
}

export function suggestMappings(files: WebFile[], episodes: EpisodeChoice[]): Record<number, WebMapping> {
  const result: Record<number, WebMapping> = {};
  const assigned = new Set(files.flatMap(f => f.mapping?.tmdb_episode_id ? [f.mapping.tmdb_episode_id] : []));
  for (const file of files) {
    if (file.mapping) { result[file.id] = file.mapping; continue; }
    const matches = episodes.filter(e => e.episode_number === file.episode_number && !assigned.has(e.tmdb_episode_id));
    // Duplicate local numbers are ambiguous; do not arbitrarily assign the first.
    const localMatches = files.filter(f => !f.mapping && f.episode_number === file.episode_number);
    if (file.episode_number !== null && matches.length === 1 && localMatches.length === 1) {
      result[file.id] = matches[0];
      assigned.add(matches[0].tmdb_episode_id);
    }
  }
  return result;
}

export function inferRelease(filename: string): string {
  const value = /^\s*\[([^\u005b\u005d]+)\]/.exec(filename)?.[1].trim() ?? '';
  return value.length <= 200 ? value : '';
}

export function webAvailable(config?: { web_publishing_available: boolean }): boolean {
  return config?.web_publishing_available === true;
}

export const WEB_LABELS: Record<string, string> = {
  not_published: 'Not published', configured: 'Mapping configured', ready: 'Ready to publish',
  publishing: 'Publishing', published: 'Published', changes_available: 'Changes available',
  partial: 'Partially published', failed: 'Failed',
};

export function outcomeLabel(value: string): string {
  return ({ created: 'Created', updated: 'Updated', noop: 'Unchanged', failed: 'Failed', unavailable: 'Not available' })[value] ?? value;
}

export function seasonLabel(season: { season_number: number; name_english: string | null }): string {
  const label = `Season ${season.season_number}`;
  const name = season.name_english?.trim();
  return !name || name.toLocaleLowerCase() === label.toLocaleLowerCase() ? label : `${label} · ${name}`;
}
