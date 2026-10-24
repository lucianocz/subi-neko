import test from 'node:test';
import assert from 'node:assert/strict';
import { inferRelease, suggestMappings, webAvailable, outcomeLabel, seasonLabel, type WebFile } from '../src/utils/webPublishing.ts';

const file = (id: number, number: number | null, extra: Partial<WebFile> = {}): WebFile => ({
  id, filename: `file ${id}.mkv`, episode_number: number, status: 'accepted', mapping: null,
  locked: false, published_at: null, current_sha256: null, published_sha256: null, render_error: null, ...extra,
});
const episodes = [1, 2, 3].map(n => ({ tmdb_episode_id: String(100 + n), episode_number: n, name_english: `Episode ${n}` }));

test('season summary does not repeat a default season name', () => {
  assert.equal(seasonLabel({ season_number: 1, name_english: 'Season 1' }), 'Season 1');
  assert.equal(seasonLabel({ season_number: 1, name_english: ' season 1 ' }), 'Season 1');
  assert.equal(seasonLabel({ season_number: 0, name_english: 'Specials' }), 'Season 0 · Specials');
  assert.equal(seasonLabel({ season_number: 2, name_english: null }), 'Season 2');
});

test('WEB remains completely hidden while loading and without configured availability', () => {
  assert.equal(webAvailable(), false);
  assert.equal(webAvailable({ web_publishing_available: false }), false);
  assert.equal(webAvailable({ web_publishing_available: true }), true);
});
test('release inference reads only a leading bracketed group', () => {
  assert.equal(inferRelease('[SubsPlease] Dragon - 01v2 (1080p) [5FFFA47F].mkv'), 'SubsPlease');
  assert.equal(inferRelease('  [ Group ] file [CRC].mkv'), 'Group');
  assert.equal(inferRelease('File [CRC].mkv'), '');
  assert.equal(inferRelease('[] file.mkv'), '');
  assert.equal(inferRelease(`[${'x'.repeat(201)}] file.mkv`), '');
});
test('suggestions use stored numbers, restore confirmations and reserve assigned IDs', () => {
  const confirmed = { tmdb_episode_id: '102', episode_number: 55, name_english: 'Approved edit' };
  const result = suggestMappings([file(1, 1, { mapping: confirmed, locked: true }), file(2, 2), file(3, 3)], episodes);
  assert.deepEqual(result[1], confirmed);
  assert.equal(result[2], undefined);
  assert.deepEqual(result[3], episodes[2]);
});
test('missing provider episode retains confirmed identity and approved metadata', () => {
  const mapping = { tmdb_episode_id: 'gone', episode_number: 9, name_english: 'Retained' };
  assert.deepEqual(suggestMappings([file(1, 1, { mapping, locked: true })], [])[1], mapping);
});
test('ambiguous, missing and duplicate local numbers do not get arbitrary suggestions', () => {
  assert.deepEqual(suggestMappings([file(1, 1), file(2, 1), file(3, null), file(4, 99)], episodes), {});
  assert.deepEqual(suggestMappings([file(1, 1)], [episodes[0], { ...episodes[0], tmdb_episode_id: 'other' }]), {});
});
test('actual outcomes render unchanged and failed without pretending success', () => {
  assert.equal(outcomeLabel('noop'), 'Unchanged');
  assert.equal(outcomeLabel('failed'), 'Failed');
});
