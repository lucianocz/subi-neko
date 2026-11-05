// Pure logic of the two-step project import wizard (source → TMDB mapping).
// Kept free of React/network so the rules are unit-tested without a DOM.
import type { WebConfig, WebSeason } from '../hooks/useWebPublishing.ts';
import type { SearchResult as SourceResult } from '../types/index.ts';
import { normalizeApprovedConfig } from './tmdbShared.ts';

export type SourceProvider = 'anidb' | 'anilist';
export type ImportIntent = 'import_only' | 'publish_and_import';
export type ImportStep = 'source' | 'mapping' | 'progress';

export const MAX_EXPECTED_EPISODES = 100_000;

/** getRandomValues also works on HTTP LAN installs where randomUUID is absent. */
export function newImportRequestId(): string {
  return [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

// ─── Stale async-response protection ────────────────────────────────────────

/** Structural type of `DescriptionRequestGuard` (utils/webPublishing). */
export interface LatestGuard { capture(): number; isCurrent(revision: number): boolean }

/** Run `load` and hand its outcome to the callbacks *only if no newer request,
 * edit, Back or unmount has invalidated `guard` in the meantime*. The revision is
 * captured synchronously, so call `guard.invalidate()` first to supersede earlier
 * work. `onSettled` is skipped for a superseded request (the newer one owns the
 * busy state). Resolves to whether the value was applied. */
export async function applyLatest<T>(
  guard: LatestGuard,
  load: () => Promise<T>,
  handlers: { onValue: (value: T) => void; onError?: (error: unknown) => void; onSettled?: () => void },
): Promise<boolean> {
  const revision = guard.capture();
  try {
    const value = await load();
    if (!guard.isCurrent(revision)) return false;
    handlers.onValue(value);
    return true;
  } catch (error) {
    if (guard.isCurrent(revision)) handlers.onError?.(error);
    return false;
  } finally {
    if (guard.isCurrent(revision)) handlers.onSettled?.();
  }
}

// ─── Layout contract (asserted by tests; used by the dialog) ────────────────

/** Wide enough for both steps; never wider than the viewport. */
export const IMPORT_MODAL_WIDTH = 'min(1240px, 96vw)';
/** Short-value fields keep a fixed width instead of stretching with the modal. */
export const FIELD_WIDTH = {
  order: 190,
  group: 250,
  structure: 150,
  season: 270,
  expected: 130,
} as const;

// ─── AniBridge suggestion (shape of GET /import/tmdb-suggestion) ────────────

export interface SuggestionCandidate {
  media_type: 'tv' | 'movie';
  tmdb_id: string;
  season_number: number | null;
  source_scope: string | null;
  via: string | null;
  episode_map: { source: string; target: string }[];
  source_episode_count: number | null;
}

export interface Suggestion {
  status: 'matched' | 'ambiguous' | 'conflict' | 'none' | 'unavailable';
  reason: string | null;
  title: { media_type: 'tv' | 'movie'; tmdb_id: string } | null;
  candidates: SuggestionCandidate[];
  dataset: { available: boolean; stale?: boolean; generated_on?: string | null };
}

export interface SuggestionTarget {
  media_type: 'tv' | 'movie';
  tmdb_id: string;
  /** Standard-order TMDB season to preselect; null = the user must choose. */
  season_number: number | null;
}

/** What may be preselected. Only an unambiguous title is ever applied; with
 * several seasons the title is kept and the season is left to the user. */
export function suggestionTarget(s: Suggestion | undefined | null): SuggestionTarget | null {
  if (!s) return null;
  if (s.status === 'matched' && s.candidates.length === 1) {
    const c = s.candidates[0];
    return { media_type: c.media_type, tmdb_id: c.tmdb_id, season_number: c.season_number };
  }
  if (s.status === 'ambiguous' && s.title) {
    return { ...s.title, season_number: null };
  }
  return null;
}

export interface Notice { color: 'blue' | 'yellow' | 'gray'; text: string }

export function suggestionNotice(s: Suggestion | undefined | null, applied: boolean): Notice | null {
  if (!s) return null;
  switch (s.status) {
    case 'matched':
      return { color: 'blue', text: applied
        ? 'Suggested by AniBridge — review the title and season, then import to confirm.'
        : 'AniBridge has a match for this title.' };
    case 'ambiguous':
      return s.reason === 'multiple_seasons'
        ? { color: 'yellow', text: 'AniBridge maps this entry to several seasons of the selected title — choose the season below.' }
        : { color: 'yellow', text: 'AniBridge found several possible TMDB titles — pick one below or search manually.' };
    case 'conflict':
      return { color: 'yellow', text: 'AniBridge entries disagree about the TMDB title, so nothing was preselected — pick one below or search manually.' };
    case 'none':
      return { color: 'gray', text: 'No AniBridge mapping for this title — search TMDB manually.' };
    case 'unavailable':
      return { color: 'gray', text: 'Automatic suggestions are unavailable right now — search TMDB manually.' };
  }
}

const GROUP_TYPES: Record<number, string> = {
  1: 'Original air date', 2: 'Absolute', 3: 'DVD', 4: 'Digital', 5: 'Story arc', 6: 'Production', 7: 'TV',
};

/** "Name · DVD · 25 episodes" for the episode-group picker. */
export function groupLabel(g: { name: string; type?: number | null; episode_count?: number | null }): string {
  const parts = [g.name];
  if (g.type != null && GROUP_TYPES[g.type]) parts.push(GROUP_TYPES[g.type]);
  if (g.episode_count != null) parts.push(`${g.episode_count} episode${g.episode_count === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

const dash = (range: string) => range.replace(/-/g, '–');

export function candidateLabel(c: SuggestionCandidate): string {
  const kind = c.media_type === 'tv' ? 'TV' : 'Movie';
  const season = c.media_type === 'tv' && c.season_number != null
    ? ` · ${c.season_number === 0 ? 'Specials' : `Season ${c.season_number}`}` : '';
  return `${kind} · TMDB ${c.tmdb_id}${season}`;
}

/** "ep 1–13 → 1–13" (first two segments); null when the mapping has no ranges. */
export function episodeMapSummary(c: SuggestionCandidate): string | null {
  if (!c.episode_map.length) return null;
  const shown = c.episode_map.slice(0, 2).map(m => `${dash(m.source)} → ${dash(m.target)}`);
  return `ep ${shown.join(', ')}${c.episode_map.length > 2 ? ', …' : ''}`;
}

// ─── Draft (Step 2 form state, owned by the dialog so Back never loses it) ──

export interface MappingDraft {
  /** Source identity this draft belongs to; a different source discards it. */
  sourceKey: string;
  config: WebConfig | null;
  expected: number | null;
  /** Once the user edits the count, suggestions never overwrite it. */
  expectedDirty: boolean;
  /** The automatic suggestion was applied or superseded by a manual choice. */
  suggestionHandled: boolean;
  /** "Import without TMDB mapping" (legacy path). */
  skipped: boolean;
  searchQuery: string;
}

export const sourceKey = (provider: SourceProvider, providerId: string) => `${provider}:${providerId}`;

/** Default TMDB search text: the English title when the provider has one. */
export function defaultSearchQuery(r: Pick<SourceResult, 'title' | 'title_english'>): string {
  return (r.title_english?.trim() || r.title).trim();
}

export function emptyDraft(key: string, query: string): MappingDraft {
  return { sourceKey: key, config: null, expected: null, expectedDirty: false,
    suggestionHandled: false, skipped: false, searchQuery: query };
}

/** Keep the draft when returning to the same source; start over for another. */
export function draftForSource(draft: MappingDraft | null, key: string, query: string): MappingDraft {
  return draft && draft.sourceKey === key ? draft : emptyDraft(key, query);
}

export function canProceedToMapping(s: { directory: string | null; result: SourceResult | null }): boolean {
  return !!s.directory && !!s.result;
}

/** A late suggestion response may only apply to the source it was fetched for,
 * and only while the user has not already chosen something themselves. */
export function shouldApplySuggestion(a: { suggestionKey: string; currentKey: string; draft: MappingDraft }): boolean {
  const { suggestionKey, currentKey, draft } = a;
  return suggestionKey === currentKey && draft.sourceKey === currentKey
    && !draft.suggestionHandled && draft.config === null && !draft.skipped;
}

// ─── Placement transitions (episode order / structure / season) ─────────────

export function withOrder(config: WebConfig, kind: WebConfig['order_kind']): WebConfig {
  return { ...config, order_kind: kind, group_id: null, season: null };
}

export function withGroup(config: WebConfig, groupId: string | null): WebConfig {
  return { ...config, group_id: groupId, season: null };
}

export function withStructure(config: WebConfig, structure: WebConfig['episode_structure']): WebConfig {
  return { ...config, episode_structure: structure, season: null };
}

export function withSeason(config: WebConfig, season: WebSeason | null): WebConfig {
  return { ...config, season };
}

/** Season to preselect once the catalog is known; null when the user must choose.
 * AniBridge seasons are TMDB *standard* seasons, so they only apply in that order. */
export function autoSeason(
  config: WebConfig, seasons: WebSeason[], suggestedSeasonNumber: number | null,
): WebSeason | null {
  if (config.media_type !== 'tv' || config.episode_structure !== 'SEASONED' || config.season) return null;
  if (config.order_kind === 'episode_group' && !config.group_id) return null;
  if (config.order_kind === 'standard' && suggestedSeasonNumber != null) {
    const hit = seasons.find(s => s.season_number === suggestedSeasonNumber);
    if (hit) return hit;
  }
  return seasons.length === 1 ? seasons[0] : null;
}

// ─── Expected episode count ──────────────────────────────────────────────────

export type ExpectedBasis = 'movie' | 'anibridge' | 'tmdb-season' | 'source';
export interface ExpectedSuggestion { value: number | null; basis: ExpectedBasis | null }

/** Initial suggestion only — the field stays editable and is never recomputed
 * after the user touches it (or after import). */
export function suggestExpected(a: {
  config: WebConfig;
  candidate: SuggestionCandidate | null;
  seasonEpisodeCount: number | null;
  sourceEpisodeCount: number | null;
}): ExpectedSuggestion {
  const { config, candidate, seasonEpisodeCount, sourceEpisodeCount } = a;
  if (config.media_type === 'movie') return { value: 1, basis: 'movie' };
  if (candidate && candidate.via === null && candidate.source_episode_count
    && candidate.media_type === config.media_type && candidate.tmdb_id === config.tmdb_title_id
    && config.order_kind === 'standard' && candidate.season_number === (config.season?.season_number ?? null)) {
    return { value: candidate.source_episode_count, basis: 'anibridge' };
  }
  if (config.episode_structure === 'SEASONED' && seasonEpisodeCount && seasonEpisodeCount > 0) {
    return { value: seasonEpisodeCount, basis: 'tmdb-season' };
  }
  if (sourceEpisodeCount && sourceEpisodeCount > 0) return { value: sourceEpisodeCount, basis: 'source' };
  return { value: null, basis: null };
}

export function effectiveExpected(draft: Pick<MappingDraft, 'expected' | 'expectedDirty'>, s: ExpectedSuggestion): number | null {
  return draft.expectedDirty ? draft.expected : s.value;
}

export function expectedBasisText(s: ExpectedSuggestion): string | null {
  switch (s.basis) {
    case 'movie': return 'A movie is one item.';
    case 'anibridge': return `AniBridge maps ${s.value} episodes to this season.`;
    case 'tmdb-season': return `TMDB lists ${s.value} episodes for this season.`;
    case 'source': return `The source entry lists ${s.value} episodes.`;
    default: return null;
  }
}

/** Error text, or null when `value` is a positive whole number within the cap. */
export function validateExpected(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return 'Enter the expected number of episodes.';
  if (typeof value !== 'number' || !Number.isFinite(value) || !Number.isInteger(value)) return 'Use a whole number.';
  if (value < 1) return 'Must be at least 1.';
  if (value > MAX_EXPECTED_EPISODES) return `Must be at most ${MAX_EXPECTED_EPISODES.toLocaleString('en-US')}.`;
  return null;
}

/** Mantine NumberInput yields number | '' | numeric string. */
export function parseExpectedInput(v: number | string): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  const t = v.trim();
  return t === '' || !Number.isFinite(Number(t)) ? null : Number(t);
}

// ─── Validation + actions ────────────────────────────────────────────────────

export interface MappingErrors { title?: string; language?: string; group?: string; season?: string; expected?: string }

export function mappingErrors(config: WebConfig | null, expected: number | null): MappingErrors {
  const errors: MappingErrors = {};
  if (!config) return errors;
  if (!config.metadata.title_english.trim()) errors.title = 'Enter an English title.';
  if (!/^[a-z]{2,3}$/.test(config.metadata.original_language)) errors.language = '2–3 lowercase letters.';
  if (config.order_kind === 'episode_group' && !config.group_id) errors.group = 'Select an episode group.';
  if (config.media_type === 'tv' && config.episode_structure === 'SEASONED' && !config.season) {
    errors.season = 'Select a season.';
  }
  const expectedError = validateExpected(expected);
  if (expectedError) errors.expected = expectedError;
  return errors;
}

export interface ActionState { enabled: boolean; reason: string | null }
export interface ImportActions { importOnly: ActionState; publishAndImport: ActionState }

export function importActions(a: {
  draft: Pick<MappingDraft, 'config' | 'skipped'>; expected: number | null; pending: boolean; busy?: boolean;
}): ImportActions {
  const { draft, expected, pending, busy } = a;
  const off = (reason: string): ImportActions => ({
    importOnly: { enabled: false, reason }, publishAndImport: { enabled: false, reason } });
  if (pending) return off('Importing…');
  if (busy) return off('Loading TMDB metadata…');
  if (draft.skipped) {
    return { importOnly: { enabled: true, reason: null },
      publishAndImport: { enabled: false, reason: 'Publish & Import needs a TMDB mapping.' } };
  }
  if (!draft.config) return off('Choose a TMDB title first.');
  const first = Object.values(mappingErrors(draft.config, expected))[0];
  if (first) return off(first);
  return { importOnly: { enabled: true, reason: null }, publishAndImport: { enabled: true, reason: null } };
}

export interface ImportBody {
  directory_name: string;
  provider: string;
  provider_id: string;
  anime_title: string;
  anime_title_native?: string | null;
  anime_year?: number | null;
  intent?: ImportIntent;
  request_id?: string;
  tmdb?: { config: WebConfig; expected_episode_count: number };
}

/** The request for either action; both carry the identical mapping. Returns
 * null when the action is not currently allowed (see `importActions`). */
export function buildImportBody(a: {
  directory: string; provider: SourceProvider; result: SourceResult;
  draft: Pick<MappingDraft, 'config' | 'skipped'>; expected: number | null; intent: ImportIntent;
}): ImportBody | null {
  const { directory, provider, result, draft, expected, intent } = a;
  const base: ImportBody = {
    directory_name: directory, provider, provider_id: result.provider_id, anime_title: result.title,
    anime_title_native: result.title_native, anime_year: result.year, intent,
  };
  if (intent === 'import_only') {
    const { intent: _intent, ...source } = base;
    void _intent;
    return source;
  }
  if (draft.skipped) return null;
  if (!draft.config || expected === null || Object.keys(mappingErrors(draft.config, expected)).length) return null;
  return {
    ...base,
    tmdb: {
      expected_episode_count: expected,
      config: normalizeApprovedConfig(draft.config),
    },
  };
}

/** Result copy that keeps "imported" and "published" apart. */
export function importOutcomeMessage(intent: ImportIntent, name: string): { title: string; message: string } {
  return intent === 'publish_and_import'
    ? { title: 'Project imported', message: `“${name}” was imported and Discover has started. Initial metadata publication is tracked separately.` }
    : { title: 'Project imported', message: `“${name}” was imported and Discover has started.` };
}
