import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FIELD_WIDTH, MAX_EXPECTED_EPISODES, applyLatest, autoSeason, buildImportBody, candidateLabel,
  canProceedToMapping, defaultSearchQuery, draftForSource, effectiveExpected, emptyDraft, episodeMapSummary,
  expectedBasisText, groupLabel, importActions, importOutcomeMessage, mappingErrors, parseExpectedInput,
  shouldApplySuggestion, sourceKey, suggestExpected, suggestionNotice, suggestionTarget, validateExpected,
  withGroup, withOrder, withSeason, withStructure,
  type MappingDraft, type Suggestion, type SuggestionCandidate,
} from '../src/utils/tmdbImport.ts';
import { DescriptionRequestGuard } from '../src/utils/webPublishing.ts';
import type { WebConfig, WebSeason } from '../src/hooks/useWebPublishing.ts';
import type { SearchResult } from '../src/types/index.ts';

const season = (n: number, id = `s${n}`): WebSeason => ({ external_id: id, season_number: n, name_english: `Season ${n}`, year: null });
const config = (over: Partial<WebConfig> = {}): WebConfig => ({
  tmdb_title_id: '42', media_type: 'tv', episode_structure: 'SEASONED', order_kind: 'standard', group_id: null, season: null,
  metadata: { title_english: ' Title ', title_romaji: null, year: 2021, original_language: 'ja',
    original_description: 'Orig', description: 'Approved' },
  poster_path: '/p.jpg', ...over,
});
const candidate = (over: Partial<SuggestionCandidate> = {}): SuggestionCandidate => ({
  media_type: 'tv', tmdb_id: '42', season_number: 2, source_scope: null, via: null,
  episode_map: [{ source: '1-12', target: '13-24' }], source_episode_count: 12, ...over,
});
const suggestion = (over: Partial<Suggestion> = {}): Suggestion => ({
  status: 'matched', reason: null, title: { media_type: 'tv', tmdb_id: '42' }, candidates: [candidate()],
  dataset: { available: true }, ...over,
});
const result = (over: Partial<SearchResult> = {}): SearchResult => ({
  provider_id: '7', title: 'Romaji', title_english: 'English', title_native: 'Native', year: 2021, media_type: 'TV', episode_count: 12, ...over,
});
const draft = (over: Partial<MappingDraft> = {}): MappingDraft => ({ ...emptyDraft('anidb:7', 'q'), ...over });

// ── AniBridge suggestion handling ────────────────────────────────────────────

test('a single matched candidate is the only thing preselected with its season', () => {
  assert.deepEqual(suggestionTarget(suggestion()), { media_type: 'tv', tmdb_id: '42', season_number: 2 });
  assert.deepEqual(suggestionTarget(suggestion({ candidates: [candidate({ media_type: 'movie', season_number: null })],
    title: { media_type: 'movie', tmdb_id: '42' } })), { media_type: 'movie', tmdb_id: '42', season_number: null });
});

test('several seasons keep the title but never pick a season', () => {
  const s = suggestion({ status: 'ambiguous', reason: 'multiple_seasons', candidates: [candidate(), candidate({ season_number: 3 })] });
  assert.deepEqual(suggestionTarget(s), { media_type: 'tv', tmdb_id: '42', season_number: null });
});

test('ambiguous titles, conflicts, missing and unavailable mappings preselect nothing', () => {
  assert.equal(suggestionTarget(suggestion({ status: 'ambiguous', reason: 'multiple_titles', title: null })), null);
  assert.equal(suggestionTarget(suggestion({ status: 'conflict', reason: 'direct_vs_linked', title: null })), null);
  assert.equal(suggestionTarget(suggestion({ status: 'none', title: null, candidates: [] })), null);
  assert.equal(suggestionTarget(suggestion({ status: 'unavailable', title: null, candidates: [] })), null);
  assert.equal(suggestionTarget(undefined), null);
  assert.equal(suggestionTarget(suggestion({ candidates: [candidate(), candidate({ season_number: 3 })] })), null);   // never arbitrary
});

test('every suggestion outcome has its own explicit notice', () => {
  const notices = (['matched', 'ambiguous', 'conflict', 'none', 'unavailable'] as const)
    .map(status => suggestionNotice(suggestion({ status, reason: null }), true)?.text);
  assert.equal(new Set(notices).size, 5);
  assert.match(notices[0]!, /review the title and season/i);
  assert.match(suggestionNotice(suggestion({ status: 'ambiguous', reason: 'multiple_seasons' }), false)!.text, /choose the season/);
  assert.match(suggestionNotice(suggestion({ status: 'ambiguous', reason: 'multiple_titles' }), false)!.text, /several possible TMDB titles/);
  assert.match(notices[2]!, /nothing was preselected/);
  assert.equal(suggestionNotice(undefined, false), null);
});

test('a late suggestion only applies to its own source and untouched drafts', () => {
  const fresh = draft();
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:7', currentKey: 'anidb:7', draft: fresh }), true);
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:6', currentKey: 'anidb:7', draft: fresh }), false);      // other source
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:7', currentKey: 'anidb:7', draft: draft({ sourceKey: 'anilist:7' }) }), false);
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:7', currentKey: 'anidb:7', draft: draft({ suggestionHandled: true }) }), false); // user chose first
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:7', currentKey: 'anidb:7', draft: draft({ config: config() }) }), false);
  assert.equal(shouldApplySuggestion({ suggestionKey: 'anidb:7', currentKey: 'anidb:7', draft: draft({ skipped: true }) }), false);
});

test('candidate and episode-map labels are readable', () => {
  assert.equal(candidateLabel(candidate()), 'TV · TMDB 42 · Season 2');
  assert.equal(candidateLabel(candidate({ season_number: 0 })), 'TV · TMDB 42 · Specials');
  assert.equal(candidateLabel(candidate({ media_type: 'movie', season_number: null })), 'Movie · TMDB 42');
  assert.equal(episodeMapSummary(candidate()), 'ep 1–12 → 13–24');
  assert.equal(episodeMapSummary(candidate({ episode_map: [{ source: '1', target: '1' }, { source: '2', target: '2' }, { source: '3', target: '3' }] })),
    'ep 1 → 1, 2 → 2, …');
  assert.equal(episodeMapSummary(candidate({ episode_map: [] })), null);
});

test('episode groups are labelled with type and size', () => {
  assert.equal(groupLabel({ name: 'Arcs', type: 5, episode_count: 25 }), 'Arcs · Story arc · 25 episodes');
  assert.equal(groupLabel({ name: 'One', episode_count: 1 }), 'One · 1 episode');
  assert.equal(groupLabel({ name: 'Plain', type: 99 }), 'Plain');
});

// ── Step navigation and draft ownership ──────────────────────────────────────

test('step 2 needs a directory and a chosen source title', () => {
  assert.equal(canProceedToMapping({ directory: null, result: result() }), false);
  assert.equal(canProceedToMapping({ directory: 'Show', result: null }), false);
  assert.equal(canProceedToMapping({ directory: 'Show', result: result() }), true);
});

test('Back and Next keep the entered mapping for the same source and drop it for another', () => {
  const key = sourceKey('anidb', '7');
  const edited = draft({ config: config(), expected: 24, expectedDirty: true, searchQuery: 'custom' });
  assert.equal(draftForSource(edited, key, 'ignored'), edited);                        // same identity → untouched
  const other = draftForSource(edited, sourceKey('anilist', '7'), 'New Title');        // same id, other provider
  assert.deepEqual(other, emptyDraft('anilist:7', 'New Title'));
  assert.deepEqual(draftForSource(edited, sourceKey('anidb', '8'), 'x'), emptyDraft('anidb:8', 'x'));
  assert.deepEqual(draftForSource(null, key, 'q'), emptyDraft(key, 'q'));
});

test('the TMDB search starts from the provider English title when there is one', () => {
  assert.equal(defaultSearchQuery({ title: 'Romaji', title_english: ' English ' }), 'English');
  assert.equal(defaultSearchQuery({ title: ' Romaji ', title_english: null }), 'Romaji');
});

// ── Placement: order, structure, season ──────────────────────────────────────

test('changing order, group or structure clears everything that depended on it', () => {
  const placed = config({ order_kind: 'episode_group', group_id: 'g', season: season(2) });
  assert.deepEqual([withOrder(placed, 'standard').order_kind, withOrder(placed, 'standard').group_id, withOrder(placed, 'standard').season], ['standard', null, null]);
  assert.equal(withGroup(placed, 'g2').season, null);
  assert.equal(withGroup(placed, 'g2').group_id, 'g2');
  assert.equal(withStructure(config({ season: season(1) }), 'FLAT').season, null);
  assert.equal(withSeason(config(), season(3)).season?.season_number, 3);
});

test('the AniBridge season is preselected only in the standard order', () => {
  const seasons = [season(0), season(1), season(2)];
  assert.equal(autoSeason(config(), seasons, 2)?.external_id, 's2');
  assert.equal(autoSeason(config(), seasons, 9), null);                                   // unknown season → user chooses
  assert.equal(autoSeason(config(), seasons, null), null);                                // several seasons, no hint
  assert.equal(autoSeason(config({ order_kind: 'episode_group', group_id: 'g' }), seasons, 2), null);  // groups use other numbering
});

test('the only available season is preselected; nothing else is ever guessed', () => {
  assert.equal(autoSeason(config(), [season(1)], null)?.external_id, 's1');
  assert.equal(autoSeason(config({ order_kind: 'episode_group', group_id: 'g' }), [season(1, 'sub')], null)?.external_id, 'sub');
  assert.equal(autoSeason(config({ order_kind: 'episode_group', group_id: null }), [season(1)], null), null);
  assert.equal(autoSeason(config({ season: season(2) }), [season(1)], null), null);       // already chosen
  assert.equal(autoSeason(config({ episode_structure: 'FLAT' }), [season(1)], null), null);
  assert.equal(autoSeason(config({ media_type: 'movie', episode_structure: 'FLAT' }), [season(1)], null), null);
});

// ── Expected episode count ───────────────────────────────────────────────────

test('expected-count suggestion prefers AniBridge, then the TMDB season, then the source', () => {
  const cfg = config({ season: season(2) });
  assert.deepEqual(suggestExpected({ config: cfg, candidate: candidate(), seasonEpisodeCount: 24, sourceEpisodeCount: 13 }), { value: 12, basis: 'anibridge' });
  assert.deepEqual(suggestExpected({ config: cfg, candidate: null, seasonEpisodeCount: 24, sourceEpisodeCount: 13 }), { value: 24, basis: 'tmdb-season' });
  assert.deepEqual(suggestExpected({ config: cfg, candidate: null, seasonEpisodeCount: null, sourceEpisodeCount: 13 }), { value: 13, basis: 'source' });
  assert.deepEqual(suggestExpected({ config: cfg, candidate: null, seasonEpisodeCount: null, sourceEpisodeCount: null }), { value: null, basis: null });
  assert.deepEqual(suggestExpected({ config: config({ media_type: 'movie', episode_structure: 'FLAT' }), candidate: null, seasonEpisodeCount: null, sourceEpisodeCount: 5 }), { value: 1, basis: 'movie' });
});

test('an AniBridge count is only trusted for the selected season of the same title', () => {
  const base = { candidate: candidate(), seasonEpisodeCount: 24, sourceEpisodeCount: null };
  assert.equal(suggestExpected({ ...base, config: config({ season: season(3) }) }).basis, 'tmdb-season');          // other season
  assert.equal(suggestExpected({ ...base, config: config({ tmdb_title_id: '99', season: season(2) }) }).basis, 'tmdb-season');
  assert.equal(suggestExpected({ ...base, config: config({ order_kind: 'episode_group', group_id: 'g', season: season(2) }) }).basis, 'tmdb-season');
  assert.equal(suggestExpected({ ...base, candidate: candidate({ via: 'anidb:1:R' }), config: config({ season: season(2) }) }).basis, 'tmdb-season'); // linked numbering
  assert.equal(suggestExpected({ ...base, candidate: candidate({ source_episode_count: null }), config: config({ season: season(2) }) }).basis, 'tmdb-season'); // open range
});

test('a user-entered count is never replaced by a later suggestion', () => {
  const s = { value: 12, basis: 'tmdb-season' as const };
  assert.equal(effectiveExpected({ expected: null, expectedDirty: false }, s), 12);
  assert.equal(effectiveExpected({ expected: 24, expectedDirty: true }, s), 24);
  assert.equal(effectiveExpected({ expected: null, expectedDirty: true }, s), null);       // cleared on purpose → invalid, not re-suggested
  assert.match(expectedBasisText(s)!, /TMDB lists 12/);
  assert.equal(expectedBasisText({ value: null, basis: null }), null);
});

test('expected episodes must be a positive whole number within the cap', () => {
  for (const ok of [1, 12, 500, MAX_EXPECTED_EPISODES]) assert.equal(validateExpected(ok), null, String(ok));
  for (const bad of [0, -1, 1.5, NaN, Infinity, '12', null, undefined, '', MAX_EXPECTED_EPISODES + 1]) {
    assert.notEqual(validateExpected(bad), null, String(bad));
  }
  assert.equal(parseExpectedInput(12), 12);
  assert.equal(parseExpectedInput(' 7 '), 7);
  assert.equal(parseExpectedInput(''), null);
  assert.equal(parseExpectedInput('abc'), null);
  assert.equal(parseExpectedInput(NaN), null);
});

// ── Validation, actions and the request ──────────────────────────────────────

test('mapping validation reports each missing piece', () => {
  assert.deepEqual(mappingErrors(null, null), {});
  const errors = mappingErrors(config({ metadata: { ...config().metadata, title_english: '  ', original_language: 'JA' } }), 0);
  assert.deepEqual(Object.keys(errors).sort(), ['expected', 'language', 'season', 'title']);
  assert.ok(mappingErrors(config({ order_kind: 'episode_group', group_id: null, season: season(1) }), 12).group);
  assert.deepEqual(mappingErrors(config({ season: season(1) }), 12), {});
  assert.deepEqual(mappingErrors(config({ episode_structure: 'FLAT' }), 12), {});
  assert.deepEqual(mappingErrors(config({ media_type: 'movie', episode_structure: 'FLAT' }), 1), {});
});

test('the two actions are enabled together, and only with a valid confirmed mapping', () => {
  const ready = { draft: { config: config({ season: season(1) }), skipped: false }, expected: 12, pending: false };
  assert.deepEqual(importActions(ready), { importOnly: { enabled: true, reason: null }, publishAndImport: { enabled: true, reason: null } });
  const noTitle = importActions({ ...ready, draft: { config: null, skipped: false } });
  assert.equal(noTitle.importOnly.enabled || noTitle.publishAndImport.enabled, false);
  assert.match(noTitle.importOnly.reason!, /Choose a TMDB title/);
  assert.match(importActions({ ...ready, expected: 0 }).importOnly.reason!, /at least 1/);
  assert.match(importActions({ ...ready, draft: { config: config(), skipped: false } }).importOnly.reason!, /Select a season/);
  assert.equal(importActions({ ...ready, pending: true }).importOnly.enabled, false);
  assert.equal(importActions({ ...ready, busy: true }).publishAndImport.enabled, false);
});

test('skipping the mapping allows only Import Only', () => {
  const skipped = importActions({ draft: { config: null, skipped: true }, expected: null, pending: false });
  assert.equal(skipped.importOnly.enabled, true);
  assert.equal(skipped.publishAndImport.enabled, false);
  assert.match(skipped.publishAndImport.reason!, /needs a TMDB mapping/);
});

test('source-only import omits all mapping and publishing intent, even after visiting settings', () => {
  const args = { directory: 'Show', provider: 'anilist' as const, result: result(),
    draft: { config: config({ season: season(2) }), skipped: false }, expected: 12 };
  const only = buildImportBody({ ...args, intent: 'import_only' })!;
  const publish = buildImportBody({ ...args, intent: 'publish_and_import' })!;
  assert.equal('tmdb' in only, false);
  assert.equal('intent' in only, false);
  assert.equal(publish.intent, 'publish_and_import');
  assert.equal(publish.tmdb!.expected_episode_count, 12);
  assert.equal(publish.tmdb!.config.season!.external_id, 's2');
  assert.equal(publish.tmdb!.config.metadata.title_english, 'Title');
  assert.equal(publish.tmdb!.config.metadata.original_description, 'Orig');
  assert.equal(publish.tmdb!.config.metadata.description, 'Approved');
});

test('the request keeps the existing source fields and sends nothing new for them', () => {
  const body = buildImportBody({ directory: 'Show', provider: 'anidb', result: result(),
    draft: { config: config({ season: season(1) }), skipped: false }, expected: 3, intent: 'import_only' })!;
  assert.deepEqual(
    { directory_name: body.directory_name, provider: body.provider, provider_id: body.provider_id, anime_title: body.anime_title,
      anime_title_native: body.anime_title_native, anime_year: body.anime_year },
    { directory_name: 'Show', provider: 'anidb', provider_id: '7', anime_title: 'Romaji', anime_title_native: 'Native', anime_year: 2021 });
  assert.equal('anime_title_english' in body, false);
});

test('publication normalises approved metadata and refuses invalid settings; plain import ignores settings', () => {
  const messy = config({ season: season(1), metadata: { ...config().metadata, title_romaji: '  ', description: '   ' } });
  const base = { directory: 'Show', provider: 'anidb' as const, result: result() };
  const body = buildImportBody({ ...base, draft: { config: messy, skipped: false }, expected: 3, intent: 'publish_and_import' })!;
  assert.equal(body.tmdb!.config.metadata.title_romaji, null);
  assert.equal(body.tmdb!.config.metadata.description, null);
  for (const draft of [{ config: config(), skipped: false }, { config: null, skipped: false }, { config: messy, skipped: true }]) {
    assert.equal(buildImportBody({ ...base, draft, expected: 3, intent: 'publish_and_import' }), null);
    const plain = buildImportBody({ ...base, draft, expected: null, intent: 'import_only' })!;
    assert.equal('tmdb' in plain, false);
    assert.equal('intent' in plain, false);
  }
});

test('import outcome keeps publication separate', () => {
  assert.doesNotMatch(importOutcomeMessage('import_only', 'Show').message, /publish/i);
  assert.match(importOutcomeMessage('publish_and_import', 'Show').message, /publication is tracked separately/);
});

// ── Stale async responses ────────────────────────────────────────────────────

const deferred = <T,>() => {
  let resolve!: (v: T) => void; let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

test('only the latest TMDB title selection is applied, whatever order responses arrive in', async () => {
  const guard = new DescriptionRequestGuard();
  const slow = deferred<string>(); const fast = deferred<string>();
  const applied: string[] = []; const settled: string[] = [];

  guard.invalidate();
  const first = applyLatest(guard, () => slow.promise, { onValue: v => applied.push(v), onSettled: () => settled.push('first') });
  guard.invalidate();                                   // the user picked another title
  const second = applyLatest(guard, () => fast.promise, { onValue: v => applied.push(v), onSettled: () => settled.push('second') });

  fast.resolve('B'); assert.equal(await second, true);
  slow.resolve('A'); assert.equal(await first, false);  // late response from the superseded request
  assert.deepEqual(applied, ['B']);
  assert.deepEqual(settled, ['second']);                // the newer request owns the busy flag
});

test('Back, an edit or closing the dialog drops a pending response and its error', async () => {
  const guard = new DescriptionRequestGuard();
  const pending = deferred<string>(); const failing = deferred<string>();
  const seen: string[] = [];
  const ok = applyLatest(guard, () => pending.promise, { onValue: v => seen.push(v), onError: () => seen.push('err'), onSettled: () => seen.push('settled') });
  guard.invalidate();                                   // e.g. unmount / user edit
  pending.resolve('late'); assert.equal(await ok, false);

  const bad = applyLatest(guard, () => failing.promise, { onValue: v => seen.push(v), onError: () => seen.push('err'), onSettled: () => seen.push('settled') });
  guard.invalidate();
  failing.reject(new Error('boom')); assert.equal(await bad, false);
  assert.deepEqual(seen, []);                           // nothing leaked into the UI
});

test('a current request reports its value, error and settled state', async () => {
  const guard = new DescriptionRequestGuard();
  const seen: string[] = [];
  assert.equal(await applyLatest(guard, async () => 'v', { onValue: v => seen.push(v), onSettled: () => seen.push('done') }), true);
  assert.equal(await applyLatest(guard, async () => { throw new Error('x'); }, { onValue: () => seen.push('no'), onError: () => seen.push('err'), onSettled: () => seen.push('done') }), false);
  assert.deepEqual(seen, ['v', 'done', 'err', 'done']);
});

test('field widths are fixed for short values', () => {
  for (const w of Object.values(FIELD_WIDTH)) assert.ok(w >= 120 && w <= 300);
  assert.ok(FIELD_WIDTH.expected <= 140);
  assert.ok(FIELD_WIDTH.structure < FIELD_WIDTH.season);
});
